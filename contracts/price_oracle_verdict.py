# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

import json
from dataclasses import dataclass


def _addr_str(a: Address) -> str:
    try:
        return a.as_hex
    except Exception:
        return str(a)


def _to_address(val) -> Address:
    if isinstance(val, Address):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str.startswith("0x"):
            val_str = "0x" + val_str
        return Address(val_str)
    return Address(val)


def _safe_price_str(val) -> str:
    try:
        s = str(val).replace(",", "").replace("$", "").strip()
        if s.startswith("-"):
            return "0"
        if s == "" or s == ".":
            return "0"
        dot_count = 0
        for ch in s:
            if ch == ".":
                dot_count += 1
            elif ch < "0" or ch > "9":
                return "0"
        if dot_count > 1:
            return "0"
        if "." in s:
            whole, frac = s.split(".")
            frac = frac[:6].rstrip("0")
            whole = whole.lstrip("0") or "0"
            if frac:
                return whole + "." + frac
            return whole
        return s.lstrip("0") or "0"
    except Exception:
        return "0"


def _price_scaled(price_str) -> int:
    s = _safe_price_str(price_str)
    if "." in s:
        whole, frac = s.split(".")
        frac = (frac + "000000")[:6]
        return int(whole) * 1000000 + int(frac)
    return int(s) * 1000000


def _enforce_price_range(price_low: str, consensus_price: str, price_high: str):
    lo = _price_scaled(price_low)
    mid = _price_scaled(consensus_price)
    hi = _price_scaled(price_high)
    if lo > hi:
        lo, hi = hi, lo
        price_low, price_high = price_high, price_low
    if mid < lo:
        price_low = consensus_price
    if mid > hi:
        price_high = consensus_price
    return price_low, consensus_price, price_high


@allow_storage
@dataclass
class PriceDispute:
    submitter: Address
    asset_pair: str
    disputed_timestamp: str
    context_note: str
    source_urls: DynArray[str]
    status: str
    consensus_price: str
    price_low: str
    price_high: str
    confidence: bigint
    sources_used: bigint
    reasoning: str
    verdict: str


class Contract(gl.Contract):
    disputes: TreeMap[str, PriceDispute]
    next_id: bigint
    owner: Address

    def __init__(self):
        self.next_id = bigint(1)
        self.owner = _to_address(gl.message.sender_address)

    @gl.public.write
    def submit_dispute(
        self,
        asset_pair: str,
        disputed_timestamp: str,
        context_note: str,
        source_urls: DynArray[str]
    ) -> str:
        if not asset_pair or len(asset_pair.strip()) == 0 or len(asset_pair) > 20:
            raise gl.vm.UserError("Invalid asset_pair: cannot be empty or exceed 20 characters")
        if "/" not in asset_pair:
            raise gl.vm.UserError("asset_pair must contain '/'")

        if not disputed_timestamp or len(disputed_timestamp.strip()) == 0 or len(disputed_timestamp) > 30:
            raise gl.vm.UserError("Invalid disputed_timestamp: cannot be empty or exceed 30 characters")

        if len(context_note) > 300:
            raise gl.vm.UserError("Invalid context_note: cannot exceed 300 characters")

        num_urls = len(source_urls)
        if num_urls < 3:
            raise gl.vm.UserError("At least 3 source URLs required")
        if num_urls > 5:
            raise gl.vm.UserError("Maximum 5 source URLs")

        urls_list = []
        seen = []
        for i in range(num_urls):
            url = source_urls[i]
            if not (url.startswith("http://") or url.startswith("https://")):
                raise gl.vm.UserError("Invalid URL: must start with http:// or https://")
            if url in seen:
                raise gl.vm.UserError("Duplicate URL")
            seen.append(url)
            urls_list.append(url)

        dispute_id = str(self.next_id)
        self.next_id += bigint(1)

        new_dispute = PriceDispute(
            submitter=_to_address(gl.message.sender_address),
            asset_pair=asset_pair,
            disputed_timestamp=disputed_timestamp,
            context_note=context_note,
            source_urls=urls_list,
            status="PENDING",
            consensus_price="",
            price_low="",
            price_high="",
            confidence=bigint(0),
            sources_used=bigint(0),
            reasoning="",
            verdict=""
        )
        self.disputes[dispute_id] = new_dispute
        return dispute_id

    @gl.public.write
    def resolve_dispute(self, dispute_id: str) -> None:
        if dispute_id not in self.disputes:
            raise gl.vm.UserError("Dispute not found")

        dispute = self.disputes[dispute_id]
        if dispute.status != "PENDING":
            raise gl.vm.UserError("Dispute already resolved")

        source_urls_list = [dispute.source_urls[i] for i in range(len(dispute.source_urls))]
        asset_pair_cap = dispute.asset_pair
        timestamp_cap = dispute.disputed_timestamp
        context_cap = dispute.context_note

        def leader_fn():
            source_results = []
            readable_count = 0
            for url in source_urls_list:
                try:
                    res = gl.nondet.web.render(url)
                    body = res.body if hasattr(res, "body") else str(res)
                    if body and len(body.strip()) > 30:
                        source_results.append(
                            "Source [" + url + "]:\n" + body[:3000]
                        )
                        readable_count += 1
                    else:
                        source_results.append(
                            "Source [" + url + "]: (empty or too short)"
                        )
                except Exception as e:
                    source_results.append(
                        "Source [" + url + "]: (fetch failed: " + str(e) + ")"
                    )

            prompt = f"""You are an expert financial data analyst working as an
AI price oracle validator on GenLayer.

Asset Pair: {asset_pair_cap}
Disputed Timestamp: {timestamp_cap}
Context (why this price is disputed): {context_cap}

The following price sources were fetched on-chain:

{chr(10).join(source_results)}

Your task:
1. Extract the price of {asset_pair_cap} at or nearest to {timestamp_cap}
   from each source that returned readable data.
2. Compute an aggregate consensus price from the readable sources.
   If prices differ significantly, weight them equally unless one is
   clearly an outlier (more than 15% away from the median).
3. Determine a price range [low, high] representing the variance
   across sources (exclude outliers from range calculation).
4. Assess whether consensus was reached:
   - "CONSENSUS_REACHED": at least 2 readable sources, variance < 5%
   - "HIGH_VARIANCE": at least 2 readable sources, variance >= 5%
   - "INSUFFICIENT_DATA": fewer than 2 readable sources returned price data

Return confidence 0-100 (how certain you are of the consensus price).

CRITICAL RULES for your response:
- consensus_price, price_low, price_high must be numeric strings
  WITHOUT currency symbols or commas. Examples: "98432.50", "0.0012"
- If INSUFFICIENT_DATA, set all three to "0"
- sources_used = integer count of sources with readable price data

Return ONLY raw JSON, no markdown, no backticks:
{{"verdict": "CONSENSUS_REACHED"|"HIGH_VARIANCE"|"INSUFFICIENT_DATA",
  "consensus_price": "<number string>",
  "price_low": "<number string>",
  "price_high": "<number string>",
  "confidence": <0-100>,
  "sources_used": <integer>,
  "reasoning": "<2-3 sentence explanation citing specific source prices>"}}"""

            raw = gl.nondet.exec_prompt(prompt, response_format="json")

            try:
                if isinstance(raw, dict):
                    parsed = raw
                else:
                    cleaned = str(raw).strip()
                    if cleaned.startswith("```json"):
                        cleaned = cleaned[7:]
                    if cleaned.startswith("```"):
                        cleaned = cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]
                    parsed = json.loads(cleaned.strip())

                verdict = parsed.get("verdict", "INSUFFICIENT_DATA")
                if verdict not in ["CONSENSUS_REACHED", "HIGH_VARIANCE", "INSUFFICIENT_DATA"]:
                    verdict = "INSUFFICIENT_DATA"

                try:
                    conf = int(parsed.get("confidence", 0))
                    conf = max(0, min(100, conf))
                except Exception:
                    conf = 0

                try:
                    src_used = max(0, int(parsed.get("sources_used", readable_count)))
                except Exception:
                    src_used = readable_count

                consensus_price = _safe_price_str(parsed.get("consensus_price", "0"))
                price_low = _safe_price_str(parsed.get("price_low", "0"))
                price_high = _safe_price_str(parsed.get("price_high", "0"))

                if verdict == "INSUFFICIENT_DATA":
                    consensus_price = "0"
                    price_low = "0"
                    price_high = "0"
                else:
                    price_low, consensus_price, price_high = _enforce_price_range(
                        price_low, consensus_price, price_high
                    )

                return {
                    "verdict": verdict,
                    "consensus_price": consensus_price,
                    "price_low": price_low,
                    "price_high": price_high,
                    "confidence": conf,
                    "sources_used": src_used,
                    "reasoning": str(parsed.get("reasoning", ""))
                }
            except Exception as e:
                return {
                    "verdict": "INSUFFICIENT_DATA",
                    "consensus_price": "0",
                    "price_low": "0",
                    "price_high": "0",
                    "confidence": 0,
                    "sources_used": readable_count,
                    "reasoning": "Parse error: " + str(e)
                }

        def validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            lp = leader_res.calldata
            if not isinstance(lp, dict):
                return False

            leader_verdict = lp.get("verdict")
            leader_conf = lp.get("confidence")
            leader_price = lp.get("consensus_price", "0")

            if leader_verdict not in ["CONSENSUS_REACHED", "HIGH_VARIANCE", "INSUFFICIENT_DATA"]:
                return False
            try:
                lc = int(leader_conf)
                if not (0 <= lc <= 100):
                    return False
            except Exception:
                return False

            try:
                my_result = leader_fn()
            except Exception:
                return False

            if my_result.get("verdict") != leader_verdict:
                return False

            try:
                lp_scaled = _price_scaled(leader_price)
                my_scaled = _price_scaled(my_result.get("consensus_price", "0"))
                if lp_scaled > 0:
                    diff = my_scaled - lp_scaled
                    if diff < 0:
                        diff = -diff
                    if diff * 50 > lp_scaled:
                        return False
            except Exception:
                return False

            try:
                mc = int(my_result.get("confidence", 0))
                if not (0 <= mc <= 100):
                    return False
            except Exception:
                return False

            def _band(c):
                if c < 35:
                    return 1
                elif c < 80:
                    return 2
                else:
                    return 3

            return _band(mc) == _band(lc)

        ruling = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        result_data = ruling.calldata if hasattr(ruling, "calldata") else ruling

        dispute.verdict = result_data.get("verdict", "INSUFFICIENT_DATA")
        dispute.consensus_price = str(result_data.get("consensus_price", "0"))
        dispute.price_low = str(result_data.get("price_low", "0"))
        dispute.price_high = str(result_data.get("price_high", "0"))
        dispute.confidence = bigint(int(result_data.get("confidence", 0)))
        dispute.sources_used = bigint(int(result_data.get("sources_used", 0)))
        dispute.reasoning = str(result_data.get("reasoning", ""))
        dispute.status = "RESOLVED"
        self.disputes[dispute_id] = dispute

    @gl.public.write
    def invalidate_dispute(self, dispute_id: str) -> None:
        if dispute_id not in self.disputes:
            raise gl.vm.UserError("Dispute not found")

        sender = _to_address(gl.message.sender_address)
        if sender != self.owner:
            raise gl.vm.UserError("Only owner can invalidate disputes")

        dispute = self.disputes[dispute_id]
        if dispute.status != "PENDING":
            raise gl.vm.UserError("Dispute already resolved")

        dispute.status = "INCONCLUSIVE"
        dispute.reasoning = "Invalidated by admin"
        self.disputes[dispute_id] = dispute

    @gl.public.view
    def get_dispute(self, dispute_id: str) -> str:
        if dispute_id not in self.disputes:
            raise gl.vm.UserError("Dispute not found")

        dispute = self.disputes[dispute_id]
        urls = [dispute.source_urls[i] for i in range(len(dispute.source_urls))]
        res = {
            "submitter": _addr_str(dispute.submitter),
            "asset_pair": dispute.asset_pair,
            "disputed_timestamp": dispute.disputed_timestamp,
            "context_note": dispute.context_note,
            "source_urls": urls,
            "status": dispute.status,
            "consensus_price": dispute.consensus_price,
            "price_low": dispute.price_low,
            "price_high": dispute.price_high,
            "confidence": int(dispute.confidence),
            "sources_used": int(dispute.sources_used),
            "reasoning": dispute.reasoning,
            "verdict": dispute.verdict
        }
        return json.dumps(res)

    @gl.public.view
    def get_price_result(self, dispute_id: str) -> str:
        if dispute_id not in self.disputes:
            raise gl.vm.UserError("Dispute not found")
        dispute = self.disputes[dispute_id]
        if dispute.status == "PENDING":
            raise gl.vm.UserError("Dispute not yet resolved")
        res = {
            "asset_pair": dispute.asset_pair,
            "disputed_timestamp": dispute.disputed_timestamp,
            "consensus_price": dispute.consensus_price,
            "price_low": dispute.price_low,
            "price_high": dispute.price_high,
            "confidence": int(dispute.confidence),
            "verdict": dispute.verdict,
            "sources_used": int(dispute.sources_used),
            "reasoning": dispute.reasoning
        }
        return json.dumps(res)

    @gl.public.view
    def list_disputes(self, status_filter: str) -> str:
        if status_filter not in ["", "PENDING", "RESOLVED", "INCONCLUSIVE"]:
            raise gl.vm.UserError("Invalid status filter")

        results = []
        limit = int(self.next_id)
        for i in range(1, limit):
            did = str(i)
            if did in self.disputes:
                d = self.disputes[did]
                if status_filter == "" or d.status == status_filter:
                    results.append({
                        "dispute_id": did,
                        "asset_pair": d.asset_pair,
                        "disputed_timestamp": d.disputed_timestamp,
                        "status": d.status,
                        "verdict": d.verdict,
                        "consensus_price": d.consensus_price
                    })
        return json.dumps(results)

    @gl.public.view
    def get_count(self) -> int:
        return int(self.next_id) - 1
