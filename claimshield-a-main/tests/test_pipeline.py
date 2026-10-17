"""End-to-end checks against the synthetic answer key (data/samples/expected.json)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from claimshield.agents import runner
from claimshield.config import PROJECT_ROOT
from claimshield.domain.coverage import entitlement, infer_insurer
from claimshield.services import get_services
from claimshield.services.parsing import LocalPdfParser, normalize_bill, normalize_policy

SAMPLES = PROJECT_ROOT / "data" / "samples"
CATALOG = json.loads((SAMPLES / "catalog.json").read_text())
EXPECTED = json.loads((SAMPLES / "expected.json").read_text())


def _run(bill_id: str, use_adk: bool, monkeypatch) -> dict:
    svc = get_services()
    monkeypatch.setattr(svc, "settings", svc.settings.__class__(**{**svc.settings.__dict__, "use_adk": use_adk}))
    bill = CATALOG["bills"][bill_id]
    policy = CATALOG["policies"][bill["policy"]]
    case_id = f"test-{bill_id.lower()}-{uuid.uuid4().hex[:6]}"
    svc.storage.put(f"cases/{case_id}/bill.pdf", (SAMPLES / bill["file"]).read_bytes())
    svc.storage.put(f"cases/{case_id}/policy.pdf", (SAMPLES / policy["file"]).read_bytes())
    return asyncio.run(runner.run_audit(case_id, f"cases/{case_id}/bill.pdf", f"cases/{case_id}/policy.pdf",
                                        Path(bill["file"]).name, Path(policy["file"]).name))


def _signature(findings: list[dict]) -> set[tuple]:
    return {(f["type"], tuple(sorted(f["bill_lines"])), f["confidence"], f.get("topic") or "") for f in findings}


@pytest.mark.parametrize("use_adk", [True, False], ids=["adk", "plain"])
@pytest.mark.parametrize("bill_id", sorted(EXPECTED))
def test_findings_match_answer_key(bill_id: str, use_adk: bool, monkeypatch) -> None:
    state = _run(bill_id, use_adk, monkeypatch)
    assert state["status"] == "complete", state.get("error")
    expected = {(f["type"], tuple(sorted(f["lines"])), f["confidence"], f.get("topic", "")) for f in EXPECTED[bill_id]["findings"]}
    assert _signature(state["findings"]) == expected

    for f in state["findings"]:
        assert f["policy"] and f["policy"]["clause_id"] and f["policy"]["quote"], f"{f['finding_id']} has no policy citation"
        assert f["impact"] > 0
        assert f["confidence"] in ("High", "Worth reviewing")
        assert "fraud" not in (f["explanation"] + f["title"]).lower()

    agents = {e["agent"] for e in state["trace"]}
    assert {"intake_agent", "policy_agent", "audit_agent", "benchmark_agent", "reconciliation_agent", "dispute_agent"} <= agents
    assert state["artifacts"]["report"]
    # An ADK-labelled test must not silently pass through the plain fallback.
    expected_runner = "ADK claimshield_orchestrator" if use_adk else "Plain runner:"
    assert any(expected_runner in e["message"] for e in state["trace"])
    paid, fair, likely, upper = EXPECTED_MONEY[bill_id]
    rec = state["reconciliation"]
    assert (rec["patient_paid"], rec["fair_patient_payable_high"],
            rec["recovery_likely"], rec["recovery_upper"]) == pytest.approx((paid, fair, likely, upper))


EXPECTED_MONEY = {
    # bill: (patient paid, fair patient payable, likely recovery, upper bound)
    "BILL-001": (25746, 900, 24846, 24846),
    "BILL-002": (14850, 8400, 6450, 6450),
    "BILL-003": (154500, 111700, 42800, 49100),
    "BILL-004": (3216, 3216, 0, 0),
}


@pytest.mark.parametrize("bill_id", sorted(EXPECTED_MONEY))
def test_reconciliation_amounts(bill_id: str, monkeypatch) -> None:
    rec = _run(bill_id, False, monkeypatch)["reconciliation"]
    paid, fair, likely, upper = EXPECTED_MONEY[bill_id]
    assert rec["patient_paid"] == pytest.approx(paid)
    assert rec["fair_patient_payable_high"] == pytest.approx(fair)
    assert rec["recovery_likely"] == pytest.approx(likely)
    assert rec["recovery_upper"] == pytest.approx(upper)


def test_clean_bill_has_no_letters(monkeypatch) -> None:
    state = _run("BILL-004", False, monkeypatch)
    assert state["findings"] == []
    assert state["letters"] == []


def test_letters_cite_the_insurers_actual_numbers(monkeypatch) -> None:
    letters = {l["recipient"]: l for l in _run("BILL-002", False, monkeypatch)["letters"]}
    assert "capped this claim (Cataract surgery (phacoemulsification with IOL), right eye) at ₹35,000" in letters["insurer"]["body"]
    assert "₹5,000" in letters["insurer"]["body"]
    assert "clause 3.3" in letters["insurer"]["body"]
    assert "₹1,000" in letters["hospital"]["body"] and "₹450" in letters["hospital"]["body"]


def test_insurer_settlement_is_reproduced_from_printed_deductions() -> None:
    parser = LocalPdfParser()
    for bill_id, entry in CATALOG["bills"].items():
        bill = normalize_bill(parser.parse((SAMPLES / entry["file"]).read_bytes()))
        assert infer_insurer(bill).reproduced == pytest.approx(bill.insurer_approved), bill_id


def test_entitlement_applies_copay_after_sub_limit() -> None:
    from claimshield.agents.steps import policy_queries
    from claimshield.domain.policy_terms import extract_terms
    from claimshield.services.retrieval import LocalRetriever

    parser = LocalPdfParser()
    bill = normalize_bill(parser.parse((SAMPLES / CATALOG["bills"]["BILL-003"]["file"]).read_bytes()))
    policy = normalize_policy(parser.parse((SAMPLES / "policies" / "carefirst_family_silver.pdf").read_bytes()), "silver.pdf")
    r = LocalRetriever()
    terms = extract_terms({t: (r.search(policy, q, 1) or [None])[0] for t, q in policy_queries(bill).items()})
    e = entitlement(bill, terms)
    assert e.non_payable == 1200 and e.sub_limit_applied == 150000 and e.copay_pct == 20
    assert e.insurer_payable == pytest.approx(120000)
    assert terms.room_rent_limit == 3000 and terms.icu_rent_limit == 6000 and terms.sum_insured == 300000
