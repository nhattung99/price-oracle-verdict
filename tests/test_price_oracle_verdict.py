import json


STANDARD_URLS = [
    "https://coingecko.com/btc-jan-15",
    "https://coinmarketcap.com/btc-history",
    "https://kraken.com/prices/btc-jan-2026"
]


def _submit(contract, direct_vm, caller, urls=None, asset_pair="BTC/USD", timestamp="2026-01-15T14:30:00Z", note="Disputed liquidation price"):
    if urls is None:
        urls = list(STANDARD_URLS)
    with direct_vm.prank(caller):
        return contract.submit_dispute(asset_pair, timestamp, note, urls)


def _mock_happy_sources(direct_vm):
    direct_vm.mock_web(r".*coingecko.*", "BTC price on Jan 15 2026: $98,450 USD")
    direct_vm.mock_web(r".*coinmarketcap.*", "Bitcoin $98,300 January 15 2026")
    direct_vm.mock_web(r".*kraken.*", "XBT/USD 98520 2026-01-15 14:29")


def _mock_consensus_llm(direct_vm, extra=None):
    payload = {
        "verdict": "CONSENSUS_REACHED",
        "consensus_price": "98423.33",
        "price_low": "98300",
        "price_high": "98520",
        "confidence": 92,
        "sources_used": 3,
        "reasoning": "Three sources agree BTC was ~$98,400 on Jan 15."
    }
    if extra:
        payload.update(extra)
    direct_vm.mock_llm(r".*price oracle validator.*", json.dumps(payload))


def test_consensus_reached_happy_path(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")

    with direct_vm.prank(direct_alice):
        dispute_id = contract.submit_dispute(
            "BTC/USD",
            "2026-01-15T14:30:00Z",
            "Disputed liquidation price",
            [
                "https://coingecko.com/btc-jan-15",
                "https://coinmarketcap.com/btc-history",
                "https://kraken.com/prices/btc-jan-2026"
            ]
        )
    assert dispute_id == "1"
    assert contract.get_count() == 1

    direct_vm.mock_web(r".*coingecko.*", "BTC price on Jan 15 2026: $98,450 USD")
    direct_vm.mock_web(r".*coinmarketcap.*", "Bitcoin $98,300 January 15 2026")
    direct_vm.mock_web(r".*kraken.*", "XBT/USD 98520 2026-01-15 14:29")

    direct_vm.mock_llm(r".*price oracle validator.*", json.dumps({
        "verdict": "CONSENSUS_REACHED",
        "consensus_price": "98423.33",
        "price_low": "98300",
        "price_high": "98520",
        "confidence": 92,
        "sources_used": 3,
        "reasoning": "Three sources agree BTC was ~$98,400 on Jan 15."
    }))

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "CONSENSUS_REACHED"
    assert result["consensus_price"] == "98423.33"
    assert result["confidence"] == 92
    assert result["sources_used"] == 3

    dispute = json.loads(contract.get_dispute(dispute_id))
    assert dispute["status"] == "RESOLVED"


def test_high_variance_verdict(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)

    direct_vm.mock_web(r".*coingecko.*", "BTC price reported at $70,000 USD on the snapshot page")
    direct_vm.mock_web(r".*coinmarketcap.*", "Bitcoin historical close $120,400 on January 15 2026")
    direct_vm.mock_web(r".*kraken.*", "XBT/USD last trade 98500 far from the other two prints")

    direct_vm.mock_llm(r".*price oracle validator.*", json.dumps({
        "verdict": "HIGH_VARIANCE",
        "consensus_price": "96300",
        "price_low": "70000",
        "price_high": "120400",
        "confidence": 41,
        "sources_used": 3,
        "reasoning": "Readable sources disagree by more than 5 percent so variance is high."
    }))

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "HIGH_VARIANCE"
    dispute = json.loads(contract.get_dispute(dispute_id))
    assert dispute["status"] == "RESOLVED"


def test_all_sources_fail_graceful(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(
        contract,
        direct_vm,
        direct_alice,
        urls=[
            "https://down-a.example.com/price",
            "https://down-b.example.com/price",
            "https://down-c.example.com/price"
        ]
    )

    def _raise(_url=None):
        raise RuntimeError("source unreachable")

    direct_vm.mock_web(r".*down-a.*", _raise)
    direct_vm.mock_web(r".*down-b.*", _raise)
    direct_vm.mock_web(r".*down-c.*", _raise)

    direct_vm.mock_llm(r".*price oracle validator.*", json.dumps({
        "verdict": "INSUFFICIENT_DATA",
        "consensus_price": "0",
        "price_low": "0",
        "price_high": "0",
        "confidence": 10,
        "sources_used": 0,
        "reasoning": "No readable price data could be extracted from the submitted sources."
    }))

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "INSUFFICIENT_DATA"
    assert result["consensus_price"] == "0"
    dispute = json.loads(contract.get_dispute(dispute_id))
    assert dispute["status"] == "RESOLVED"


def test_partial_source_failure(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)

    def _raise(_url=None):
        raise RuntimeError("coinmarketcap timeout")

    direct_vm.mock_web(r".*coingecko.*", "BTC price on Jan 15 2026: $98,450 USD from the archive page")
    direct_vm.mock_web(r".*coinmarketcap.*", _raise)
    direct_vm.mock_web(r".*kraken.*", "XBT/USD 98520 2026-01-15 14:29 printed on the public tape")

    direct_vm.mock_llm(r".*price oracle validator.*", json.dumps({
        "verdict": "CONSENSUS_REACHED",
        "consensus_price": "98485",
        "price_low": "98450",
        "price_high": "98520",
        "confidence": 81,
        "sources_used": 2,
        "reasoning": "Two working sources agree near 98485 after one fetch failed."
    }))

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "CONSENSUS_REACHED"
    assert result["sources_used"] == 2
    assert result["consensus_price"] == "98485"


def test_too_few_sources_rejected(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("At least 3 source URLs required"):
            contract.submit_dispute(
                "BTC/USD",
                "2026-01-15",
                "",
                ["https://a.com", "https://b.com"]
            )


def test_too_many_sources_rejected(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("Maximum 5 source URLs"):
            contract.submit_dispute(
                "BTC/USD",
                "2026-01-15",
                "",
                [
                    "https://a.com",
                    "https://b.com",
                    "https://c.com",
                    "https://d.com",
                    "https://e.com",
                    "https://f.com"
                ]
            )


def test_invalid_url_format(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("Invalid URL"):
            contract.submit_dispute(
                "BTC/USD",
                "2026-01-15",
                "",
                ["not-a-url", "https://b.com", "https://c.com"]
            )


def test_duplicate_urls_rejected(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("Duplicate"):
            contract.submit_dispute(
                "BTC/USD",
                "2026-01-15",
                "",
                ["https://a.com", "https://a.com", "https://c.com"]
            )


def test_invalid_asset_pair(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("asset_pair must contain"):
            contract.submit_dispute(
                "BTCUSD",
                "2026-01-15",
                "",
                ["https://a.com", "https://b.com", "https://c.com"]
            )


def test_resolve_already_resolved(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)
    _mock_happy_sources(direct_vm)
    _mock_consensus_llm(direct_vm)

    contract.resolve_dispute(dispute_id)

    with direct_vm.expect_revert("already resolved"):
        contract.resolve_dispute(dispute_id)


def test_get_price_result_pending(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    with direct_vm.prank(direct_alice):
        dispute_id = contract.submit_dispute(
            "BTC/USD",
            "2026-01-15T14:30:00Z",
            "Disputed liquidation price",
            [
                "https://coingecko.com/btc-jan-15",
                "https://coinmarketcap.com/btc-history",
                "https://kraken.com/prices/btc-jan-2026"
            ]
        )
    with direct_vm.expect_revert("not yet resolved"):
        contract.get_price_result(dispute_id)


def test_malformed_llm_json_fallback(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)
    _mock_happy_sources(direct_vm)
    direct_vm.mock_llm(r".*price oracle validator.*", "This is not JSON at all!!")

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "INSUFFICIENT_DATA"
    assert result["consensus_price"] == "0"
    dispute = json.loads(contract.get_dispute(dispute_id))
    assert dispute["status"] == "RESOLVED"


def test_invalid_verdict_string_normalized(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)
    _mock_happy_sources(direct_vm)
    direct_vm.mock_llm(r".*price oracle validator.*", json.dumps({
        "verdict": "MAYBE",
        "consensus_price": "100",
        "price_low": "90",
        "price_high": "110",
        "confidence": 50,
        "sources_used": 3,
        "reasoning": "Model returned an unknown verdict label."
    }))

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "INSUFFICIENT_DATA"
    assert result["consensus_price"] == "0"


def test_list_disputes_filter(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/price_oracle_verdict.py")

    with direct_vm.prank(direct_alice):
        id1 = contract.submit_dispute(
            "BTC/USD",
            "2026-01-15T14:30:00Z",
            "First dispute",
            [
                "https://coingecko.com/one",
                "https://coinmarketcap.com/one",
                "https://kraken.com/one"
            ]
        )
        id2 = contract.submit_dispute(
            "ETH/USD",
            "2026-02-01T09:00:00Z",
            "Second dispute",
            [
                "https://coingecko.com/two",
                "https://coinmarketcap.com/two",
                "https://kraken.com/two"
            ]
        )

    _mock_happy_sources(direct_vm)
    _mock_consensus_llm(direct_vm)
    contract.resolve_dispute(id1)

    pending = json.loads(contract.list_disputes("PENDING"))
    assert len(pending) == 1
    assert pending[0]["dispute_id"] == id2

    resolved = json.loads(contract.list_disputes("RESOLVED"))
    assert len(resolved) == 1
    assert resolved[0]["dispute_id"] == id1

    all_rows = json.loads(contract.list_disputes(""))
    assert len(all_rows) == 2


def test_invalidate_dispute_owner_only(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)

    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("Only owner"):
            contract.invalidate_dispute(dispute_id)

    contract.invalidate_dispute(dispute_id)

    dispute = json.loads(contract.get_dispute(dispute_id))
    assert dispute["status"] == "INCONCLUSIVE"
    assert dispute["reasoning"] == "Invalidated by admin"


def test_price_range_consistency(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/price_oracle_verdict.py")
    dispute_id = _submit(contract, direct_vm, direct_alice)
    _mock_happy_sources(direct_vm)
    _mock_consensus_llm(direct_vm)

    contract.resolve_dispute(dispute_id)

    result = json.loads(contract.get_price_result(dispute_id))
    assert result["verdict"] == "CONSENSUS_REACHED"
    assert float(result["price_low"]) <= float(result["consensus_price"])
    assert float(result["consensus_price"]) <= float(result["price_high"])
