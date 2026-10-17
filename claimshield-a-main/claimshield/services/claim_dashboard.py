"""Claim journey summaries assembled from existing case and timeline records."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DisputeStatus(str, Enum):
    NOT_RECORDED = "not_recorded"
    NONE = "no_dispute"
    DRAFTING = "drafting"
    SUBMITTED = "submitted"
    PENDING = "pending"
    RESOLVED = "resolved"


class ClaimOutcomeRecord(BaseModel):
    actual_settled_amount: float | None = Field(default=None, ge=0, le=100_000_000)
    confirmed_recovery: float | None = Field(default=None, ge=0, le=100_000_000)
    dispute_status: DisputeStatus | None = None


class ClaimSummary(BaseModel):
    claim_id: str
    claim_date: str | None = None
    provider: str = ""
    status: str
    dispute_status: str
    claimed_amount: float
    assessed_payable_amount: float | None = None
    assessed_payable_basis: str = "ClaimShield policy replay estimate"
    actual_settled_amount: float | None = None
    actual_settlement_source: str = "Not recorded"
    deductions: list[dict[str, Any]] = Field(default_factory=list)
    estimated_recovery: float = 0
    estimated_recovery_upper: float = 0
    confirmed_recovery: float | None = None
    outstanding_documents: list[str] = Field(default_factory=list)
    pending_actions: list[str] = Field(default_factory=list)
    simulated_demo_data: bool = False
    sample_id: str | None = None


def owner_matches(owner: dict[str, Any], subject: str, tenant: str, issuer: str) -> bool:
    """Case records are visible only to the exact original owner identity."""
    return (owner.get("version") == 1 and owner.get("owner_id") == subject
            and owner.get("tenant_id") == tenant and owner.get("issuer") == issuer)


def make_claim_summary(case_id: str, case: dict[str, Any], timeline: dict[str, Any],
                       outcome: dict[str, Any] | None = None,
                       sample_id: str | None = None) -> ClaimSummary:
    bill = case.get("bill") or {}
    rec = case.get("reconciliation") or {}
    events = sorted(timeline.get("events", []), key=lambda row: (row.get("date", ""), row.get("created_at", 0)))
    latest = events[-1] if events else None
    status = latest.get("claim_status", "not_recorded") if latest else "not_recorded"
    pending_actions: list[str] = []
    outstanding_documents: list[str] = []
    if latest and status in {"query_pending", "rejected", "grievance_open", "submitted", "under_review"}:
        if status == "query_pending":
            pending_actions.append("Respond to insurer query")
        elif status == "rejected":
            pending_actions.append("Review rejection and consider a grievance")
        elif status == "grievance_open":
            pending_actions.append("Await or request grievance response")
        else:
            pending_actions.append("Check for an insurer update")
        outstanding_documents.extend(latest.get("required_documents", []))
    outcome = outcome or {}
    actual_settled = outcome.get("actual_settled_amount")
    settlement_source = "Manually recorded; not verified by ClaimShield" if actual_settled is not None else "Not recorded"
    if actual_settled is None and rec.get("insurer_paid") not in (None, 0, 0.0):
        actual_settled = round(float(rec["insurer_paid"]), 2)
        settlement_source = "Extracted from bill settlement; not independently verified"
    dispute_status = outcome.get("dispute_status")
    if not dispute_status:
        dispute_status = "pending" if status == "grievance_open" else "resolved" if status == "grievance_resolved" else "no_dispute" if events and status != "rejected" else "not_recorded"
    return ClaimSummary(
        claim_id=case_id, claim_date=bill.get("admission_date") or None,
        provider=bill.get("hospital", ""), status=status, dispute_status=dispute_status,
        claimed_amount=round(float(bill.get("stated_gross_total") or 0), 2),
        assessed_payable_amount=round(float((rec.get("entitlement_as_billed") or {}).get("insurer_payable") or 0), 2) if rec else None,
        actual_settled_amount=actual_settled,
        actual_settlement_source=settlement_source,
        deductions=[dict(row) for row in bill.get("deductions", [])],
        estimated_recovery=round(float(rec.get("recovery_likely") or 0), 2),
        estimated_recovery_upper=round(float(rec.get("recovery_upper") or 0), 2),
        confirmed_recovery=outcome.get("confirmed_recovery"),
        outstanding_documents=outstanding_documents, pending_actions=pending_actions,
        simulated_demo_data=bool(sample_id), sample_id=sample_id,
    )


def aggregate_summaries(summaries: list[ClaimSummary]) -> dict[str, Any]:
    known_settlements = [s.actual_settled_amount for s in summaries if s.actual_settled_amount is not None]
    known_confirmed = [s.confirmed_recovery for s in summaries if s.confirmed_recovery is not None]
    return {"claim_count": len(summaries),
            "claimed_amount": round(sum(s.claimed_amount for s in summaries), 2),
            "assessed_payable_amount": round(sum(s.assessed_payable_amount or 0 for s in summaries), 2),
            "actual_settled_amount": round(sum(known_settlements), 2),
            "settlements_recorded": len(known_settlements),
            "estimated_recovery": round(sum(s.estimated_recovery for s in summaries), 2),
            "estimated_recovery_upper": round(sum(s.estimated_recovery_upper for s in summaries), 2),
            "confirmed_recovery": round(sum(known_confirmed), 2),
            "confirmed_recovery_records": len(known_confirmed),
            "deductions": round(sum(float(row.get("amount", 0)) for s in summaries for row in s.deductions), 2),
            "pending_actions": sum(len(s.pending_actions) for s in summaries),
            "outstanding_documents": sum(len(s.outstanding_documents) for s in summaries)}
