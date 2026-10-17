from datetime import date

from fastapi.testclient import TestClient

from claimshield.api.main import app
from claimshield.models import Clause, PolicyDoc, PolicyTerms, SubLimit
from claimshield.services.preclaim import PreClaimInput, assess_preclaim


def _policy(conflict: bool = False) -> tuple[PolicyDoc, PolicyTerms]:
    clauses = [Clause(clause_id="1.1", title="Hospitalisation", text="The company will pay medical expenses for covered hospitalisation."),
               Clause(clause_id="3.1", title="Room Rent", text="Room rent for a normal room is payable up to Rs. 5,000 per day."),
               Clause(clause_id="3.2", title="Co-payment", text="No co-payment applies to Network Hospitals."),
               Clause(clause_id="3.3", title="Sublimits", text="(a) Knee replacement: Rs. 60,000 per claim")]
    if conflict:
        clauses += [Clause(clause_id="4.1", title="Knee Exclusion", text="Knee replacement treatment is excluded and not payable."),
                    Clause(clause_id="4.2", title="Knee Coverage", text="Knee replacement is covered and payable under this policy.")]
    doc = PolicyDoc(plan_name="Synthetic Test Plan", clauses=clauses)
    terms = PolicyTerms(sum_insured=500_000, room_rent_limit=5_000, copay_network_pct=0,
                        copay_non_network_pct=20, sub_limits=[SubLimit(procedure="Knee replacement", amount=60_000)],
                        clause_refs={"sum_insured": "1.1", "room_rent": "3.1", "copay": "3.2", "sub_limit": "3.3"})
    return doc, terms


def _input(**changes) -> PreClaimInput:
    values = dict(treatment="Knee replacement", estimated_total=100_000, room_category="General",
                  room_rate_per_day=4_000, hospitalization_date=date(2026, 11, 1), nights=5,
                  network_hospital=True)
    values.update(changes)
    return PreClaimInput(**values)


def test_preclaim_full_coverage_scenario():
    policy, terms = _policy()
    result = assess_preclaim(_input(treatment="Appendectomy"), policy, supplied_terms=terms)
    assert result["estimated_insurer_payment"] == 100_000
    assert result["estimated_patient_payment"] == 0
    assert result["policy_evidence"]


def test_preclaim_copay_scenario():
    policy, terms = _policy()
    result = assess_preclaim(_input(treatment="Appendectomy", network_hospital=False), policy, supplied_terms=terms)
    assert result["estimated_insurer_payment"] == 80_000
    assert result["breakdown"]["copay"] == 20_000


def test_preclaim_deductible_scenario():
    policy, terms = _policy()
    result = assess_preclaim(_input(treatment="Appendectomy", deductible=10_000), policy, supplied_terms=terms)
    assert result["estimated_insurer_payment"] == 90_000
    assert result["estimated_patient_payment"] == 10_000


def test_preclaim_sublimit_scenario():
    policy, terms = _policy()
    result = assess_preclaim(_input(network_hospital=False), policy, supplied_terms=terms)
    assert result["breakdown"]["after_sublimit"] == 60_000
    assert result["estimated_insurer_payment"] == 48_000


def test_preclaim_applies_explicit_nonmedical_exclusion():
    policy, terms = _policy()
    policy.clauses.extend([
        Clause(clause_id="3.4", title="Non-medical items", text="Non-medical items are not payable."),
        Clause(clause_id="4.4", title="Personal items", text="Personal items are not payable."),
    ])
    terms.non_medical_covered = False
    terms.clause_refs.update({"non_medical": "3.4", "personal_items": "4.4"})
    data = _input(treatment="Appendectomy", estimated_total=100_000, cost_items=[
        {"description": "Surgery and hospital care", "amount": 90_000, "category": "PROCEDURE"},
        {"description": "Non-medical kit", "amount": 10_000, "category": "NON_MEDICAL"},
    ])
    result = assess_preclaim(data, policy, supplied_terms=terms)
    assert result["breakdown"]["excluded_estimate"] == 10_000
    assert result["estimated_insurer_payment"] == 90_000


def test_preclaim_missing_policy_data_does_not_assume_full_coverage():
    result = assess_preclaim(_input(), PolicyDoc(plan_name="Unclear plan"))
    assert result["estimated_insurer_payment"] is None
    assert result["estimated_patient_payment"] is None
    assert result["missing_inputs"]


def test_preclaim_conflicting_coverage_restrictions_require_confirmation():
    policy, terms = _policy(conflict=True)
    result = assess_preclaim(_input(), policy, supplied_terms=terms)
    assert result["treatment_status"] == "conflict"
    assert result["estimated_insurer_payment"] is None
    assert any("both coverage and restriction" in item for item in result["uncertainty"])
    assert {e["clause_id"] for e in result["policy_evidence"]} >= {"4.1", "4.2"}


def test_preclaim_api_accepts_policy_pdf_and_returns_citations():
    from pathlib import Path
    sample = Path(__file__).resolve().parents[1] / "data/samples/policies/suraksha_health_gold.pdf"
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        response = client.post("/api/preclaim", data={"treatment": "Appendectomy", "estimated_total": "50000",
                                  "room_category": "General", "room_rate_per_day": "3000", "nights": "2",
                                  "hospitalization_date": "2026-11-01", "network_hospital": "true"},
                               files={"policy": ("policy.pdf", sample.read_bytes(), "application/pdf")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimated_insurer_payment"] == 50_000
    assert body["policy_evidence"]
