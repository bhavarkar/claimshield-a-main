from claimshield.services.claim_dashboard import (aggregate_summaries, make_claim_summary)
from claimshield.services.claim_dashboard import owner_matches


def claim(case_id="case-a", gross=1000, assessed=700, settled=None, recovery=None, findings_recovery=50):
    case = {"case_id": case_id, "status": "complete", "bill_name": "bill.pdf",
            "bill": {"admission_date": "2026-01-01", "hospital": "Synthetic Hospital",
                     "stated_gross_total": gross,
                     "deductions": [{"label": "Co-pay", "basis": "percentage", "amount": 100}]},
            "reconciliation": {"entitlement_as_billed": {"insurer_payable": assessed},
                               "recovery_likely": findings_recovery, "recovery_upper": 90}}
    outcome = {"actual_settled_amount": settled, "confirmed_recovery": recovery}
    return make_claim_summary(case_id, case, {}, outcome)


def test_multiple_claims_aggregate_amounts_and_recoveries():
    summary = aggregate_summaries([claim("case-a", 1000, 700, 650, 100), claim("case-b", 2000, 1500, 1200, 0)])
    assert summary["claim_count"] == 2
    assert summary["claimed_amount"] == 3000
    assert summary["assessed_payable_amount"] == 2200
    assert summary["actual_settled_amount"] == 1850
    assert summary["deductions"] == 200
    assert summary["estimated_recovery"] == 100
    assert summary["confirmed_recovery"] == 100


def test_zero_claims_has_zero_aggregates():
    assert aggregate_summaries([]) == {"claim_count": 0, "claimed_amount": 0, "assessed_payable_amount": 0,
        "actual_settled_amount": 0, "settlements_recorded": 0, "estimated_recovery": 0,
        "estimated_recovery_upper": 0, "confirmed_recovery": 0, "confirmed_recovery_records": 0,
        "deductions": 0, "pending_actions": 0, "outstanding_documents": 0}


def test_partial_settlement_is_distinct_from_assessment_and_estimate():
    item = claim(settled=350)
    assert item.actual_settled_amount == 350
    assert item.assessed_payable_amount == 700
    assert item.estimated_recovery == 50
    assert item.confirmed_recovery is None
    assert item.actual_settlement_source.startswith("Manually recorded")


def test_missing_outcome_does_not_become_zero_or_confirmed_recovery():
    item = claim()
    totals = aggregate_summaries([item])
    assert item.actual_settled_amount is None
    assert item.confirmed_recovery is None
    assert totals["actual_settled_amount"] == 0 and totals["settlements_recorded"] == 0
    assert totals["confirmed_recovery"] == 0 and totals["confirmed_recovery_records"] == 0


def test_timeline_actions_and_required_documents_are_included():
    case_data = {"status": "complete", "bill": {"stated_gross_total": 500}}
    timeline = {"events": [{"date": "2026-02-01", "claim_status": "query_pending",
                             "required_documents": ["Discharge summary"]}]}
    item = make_claim_summary("case-q", case_data, timeline)
    assert item.pending_actions == ["Respond to insurer query"]
    assert item.outstanding_documents == ["Discharge summary"]


def test_access_isolation_requires_original_owner_and_matching_tenant_issuer():
    owner = {"version": 1, "owner_id": "alice", "tenant_id": "tenant-a", "issuer": "issuer"}
    assert owner_matches(owner, "alice", "tenant-a", "issuer")
    assert not owner_matches(owner, "bob", "tenant-a", "issuer")
    assert not owner_matches(owner, "alice", "tenant-b", "issuer")
    assert not owner_matches(owner, "alice", "tenant-a", "other-issuer")
