# PriceOracleVerdict: Multi-Source Price Consensus on GenLayer

PriceOracleVerdict dies without GenLayer: no EVM contract can fetch CoinGecko, CoinMarketCap, and Kraken on-chain simultaneously and reason about whether a disputed liquidation price was fair -- only GenLayer's AI validator consensus produces a multi-node verified price attestation.

---

## Problem & Solution

DeFi protocols depend on price oracles, but when a feed is manipulated or stale there is no neutral on-chain way to answer: "what was the real price at this timestamp?" Liquidations, settlements, and RWA valuations that are disputed on price have no decentralized adjudication layer.

PriceOracleVerdict lets anyone open a price dispute with:

- The asset pair (for example `BTC/USD`)
- The timestamp of the disputed price event
- 3 to 5 public source URLs (CoinGecko, CoinMarketCap, DEX pages, exchange APIs, or articles that cite the price)

The contract fetches every source on-chain, extracts price data, and has AI validators independently compute a consensus price and confidence band. The stored result (`consensus_price`, `price_low`, `price_high`, `confidence`, `verdict`) is tamper-proof evidence for dispute resolution.

Remove `web.render` and the LLM and this contract is only a registry of whatever the submitter typed in. The AI aggregation is the product.

---

## How it works

```
submit_dispute  ->  PENDING
       |
       +--> resolve_dispute (permissionless, nondet consensus)  ->  RESOLVED
       |
       +--> invalidate_dispute (owner only)                     ->  INCONCLUSIVE
```

1. **Submit** -- `submit_dispute(asset_pair, disputed_timestamp, context_note, source_urls)` stores a `PENDING` dispute and returns a `dispute_id` (`"1"`, `"2"`, ...).
2. **Resolve** -- anyone may call `resolve_dispute(dispute_id)`. Validators fetch each URL, prompt the LLM, and agree on the *meaning* of the full attestation: verdict, consensus price, low/high band, supporting-source count, and confidence band.
3. **Read** -- `get_price_result(dispute_id)` returns the settlement fields. `get_dispute` returns the full record. `list_disputes` filters by status.

Owner-only `invalidate_dispute` marks spam or clearly invalid `PENDING` submissions as `INCONCLUSIVE`.

---

## Consensus design (meaning, not format)

`resolve_dispute` runs inside `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`. The validator is not a JSON-shape check. Each validator independently re-fetches the source URLs and re-runs the LLM, then binds **every core attestation field** against the leader under explicit invariants. Two nodes that reach different decisions cannot both accept the block.

1. **Self-consistency (both sides).** Leader payload and validator payload must each be internally valid before they are compared: allowed verdict, confidence in `0..100`, `price_low <= consensus_price <= price_high`, `INSUFFICIENT_DATA` prices all `"0"`, and non-insufficient verdicts require `sources_used >= 2` with a positive midpoint.
2. **Verdict identity (exact).** `CONSENSUS_REACHED` / `HIGH_VARIANCE` / `INSUFFICIENT_DATA` must match. A different decision is a different attestation.
3. **Supporting-source count (exact).** `sources_used` is recomputed independently and must match. A 2-source vs 3-source report cannot pass.
4. **Full price band (2% per field).** `consensus_price`, `price_low`, and `price_high` are each compared within 2% (or exact `0` if either side is zero). Matching only the midpoint while the low/high range diverges is rejected.
5. **Confidence banding.** Scores are grouped into three bands (0-34, 35-79, 80-100). Nodes must land in the same band. Reasoning text is intentionally not compared.

`validator_fn` first checks `isinstance(leader_res, gl.vm.Return)` and reads `leader_res.calldata`. Each URL fetch is wrapped in its own try/except. `leader_fn` never throws; parse failures fall back to `INSUFFICIENT_DATA` and `"0"` prices. `price_low <= consensus_price <= price_high` is enforced before the result is stored.

---

## Public API

### Write methods

- `submit_dispute(asset_pair, disputed_timestamp, context_note, source_urls) -> str`
- `resolve_dispute(dispute_id) -> None` (permissionless)
- `invalidate_dispute(dispute_id) -> None` (owner only, `PENDING` only)

### View methods (JSON strings except `get_count`)

- `get_dispute(dispute_id) -> str`
- `get_price_result(dispute_id) -> str`
- `list_disputes(status_filter) -> str` (`""` | `PENDING` | `RESOLVED` | `INCONCLUSIVE`)
- `get_count() -> int`

---

## Verdict types

| Verdict | Meaning |
| --- | --- |
| `CONSENSUS_REACHED` | At least 2 readable sources and variance under 5% |
| `HIGH_VARIANCE` | At least 2 readable sources and variance of 5% or more |
| `INSUFFICIENT_DATA` | Fewer than 2 sources returned usable price data, or the LLM output could not be parsed |

---

## Use cases

- **DeFi liquidation disputes** -- attest whether a liquidation price was inside a multi-source band at that timestamp
- **RWA valuation** -- archive a consensus mark for an off-chain asset using public market pages
- **Options and settlement** -- produce a multi-node verified expiry price when a single oracle is contested

---

## Edge cases handled

- Individual source fetch failures are captured as prompt text; one dead URL does not crash resolution
- All sources failing still resolves to `INSUFFICIENT_DATA`
- Malformed LLM JSON and unknown verdict strings normalize to `INSUFFICIENT_DATA` with price `"0"`
- Duplicate URLs, invalid URL schemes, too few/too many sources, and asset pairs missing `/` revert on submit
- Already-resolved disputes cannot be resolved again; `get_price_result` reverts while status is `PENDING`
- Outliers more than 15% from the median are described in the prompt so validators can exclude them from the range

---

## Deployment

- **CONTRACT_ADDRESS**: `0x31182AabBa3b9B338a972Fd231165376ba2A7180`
- **NETWORK**: `studionet`
- **Explorer**: https://genlayer-explorer.vercel.app/address/0x31182AabBa3b9B338a972Fd231165376ba2A7180
- **Revision**: validator binds every core attestation field (`verdict`, `consensus_price`, `price_low`, `price_high`, `sources_used`, confidence band). Previous studionet address `0xe875eDbdF72d531FD58f2D680553263d7e015d79` is superseded by this deploy.

### Live read (real result, 2026-09-21)

A read-only call against the redeployed contract on studionet returned:

- `get_count()` -> `0`
- `list_disputes("")` -> `[]`
- schema methods: `submit_dispute`, `resolve_dispute`, `invalidate_dispute`, `get_dispute`, `get_price_result`, `list_disputes`, `get_count`

No disputes have been submitted on this deployment yet, so there is no on-chain `resolve_dispute` receipt to quote. The call sequence below is an **illustrative expected example** taken from the passing unit tests (same public API the live schema exposes).

### Worked example (illustrative expected output)

```
submit_dispute(
  asset_pair           = "BTC/USD",
  disputed_timestamp   = "2026-01-15T14:30:00Z",
  context_note         = "Disputed liquidation price",
  source_urls          = [
    "https://coingecko.com/btc-jan-15",
    "https://coinmarketcap.com/btc-history",
    "https://kraken.com/prices/btc-jan-2026"
  ]
)
-> dispute_id "1"

resolve_dispute("1")   # validators fetch the three pages and agree on meaning

get_price_result("1") expected:
{
  "asset_pair": "BTC/USD",
  "disputed_timestamp": "2026-01-15T14:30:00Z",
  "consensus_price": "98423.33",
  "price_low": "98300",
  "price_high": "98520",
  "confidence": 92,
  "verdict": "CONSENSUS_REACHED",
  "sources_used": 3,
  "reasoning": "Three sources agree BTC was ~$98,400 on Jan 15."
}
```

When those three sources print ~98450 / ~98300 / ~98520, variance is under 5%, so validators should store `CONSENSUS_REACHED` with `price_low <= consensus_price <= price_high`.

### Deploy again from GenLayer Studio

1. Open [GenLayer Studio](https://studio.genlayer.com) and connect a wallet.
2. Create a new Intelligent Contract and paste `contracts/price_oracle_verdict.py`.
3. Confirm the header dependency is `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`.
4. Deploy to **studionet**.
5. Call `submit_dispute` with 3-5 `https://` source URLs, then `resolve_dispute`.
6. Read the attestation with `get_price_result`.

---

## Run tests

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
gltest tests/
```

All 16 cases cover happy-path consensus, high variance, source failures, submit validation, resolve guards, LLM fallbacks, listing, admin invalidate, and price-range consistency.
