"""Claim timeline records and cautious draft generation."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ClaimEventType(str, Enum):
    SUBMISSION = "submission"
    INSURER_QUERY = "insurer_query"
    DOCUMENT_RESPONSE = "document_response"
    SETTLEMENT = "settlement"
    REJECTION = "rejection"
    GRIEVANCE = "grievance"
    CLOSURE = "closure"


class ClaimStatus(str, Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    QUERY_PENDING = "query_pending"
    DOCUMENTS_RESPONDED = "documents_responded"
    SETTLED = "settled"
    REJECTED = "rejected"
    GRIEVANCE_OPEN = "grievance_open"
    GRIEVANCE_RESOLVED = "grievance_resolved"
    CLOSED = "closed"


class ClaimEvent(BaseModel):
    event_type: ClaimEventType
    date: date
    source: str = Field(min_length=1, max_length=160)
    claim_status: ClaimStatus
    notes: str = Field(default="", max_length=4000)
    required_documents: list[str] = Field(default_factory=list, max_length=30)
    attachment_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_event(self):
        expected = {
            ClaimEventType.SUBMISSION: {ClaimStatus.SUBMITTED},
            ClaimEventType.INSURER_QUERY: {ClaimStatus.QUERY_PENDING},
            ClaimEventType.DOCUMENT_RESPONSE: {ClaimStatus.DOCUMENTS_RESPONDED},
            ClaimEventType.SETTLEMENT: {ClaimStatus.SETTLED},
            ClaimEventType.REJECTION: {ClaimStatus.REJECTED},
            ClaimEventType.GRIEVANCE: {ClaimStatus.GRIEVANCE_OPEN, ClaimStatus.GRIEVANCE_RESOLVED},
            ClaimEventType.CLOSURE: {ClaimStatus.CLOSED},
        }
        if self.claim_status not in expected[self.event_type]:
            raise ValueError("Claim status is not supported for this event type")
        return self


def chronological(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda e: (e["date"], e.get("created_at", ""), e.get("id", "")))


def draft_follow_up(events: list[dict], grievance: bool = False) -> dict[str, str]:
    if not events:
        raise ValueError("Add a claim event before drafting a follow-up")
    latest = chronological(events)[-1]
    status = latest["claim_status"].replace("_", " ")
    recipient = "insurer grievance officer" if grievance else "insurer claims team"
    subject = "Grievance regarding my health insurance claim" if grievance else "Request for an update on my health insurance claim"
    body = (f"To the {recipient},\n\nI am writing about my claim. The latest recorded status is {status} "
            f"as of {latest['date']}. Please review the claim and provide a written update. "
            "I have included the relevant claim records and supporting documents for your review.\n\n"
            "Please let me know if further information is needed.\n\nSincerely,\n[Your name]\n[Claim / policy number]")
    return {"recipient": recipient, "subject": subject, "body": body, "external_submission": "not_submitted"}
