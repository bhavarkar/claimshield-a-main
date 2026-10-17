"""Pre-authorization preparation intake; no insurer or hospital submission is performed."""
from __future__ import annotations

from datetime import date
from enum import Enum
import re

from pydantic import BaseModel, Field, model_validator

from ..models import PolicyDoc
from .preclaim import _restriction


class NetworkStatus(str, Enum):
    NETWORK = "confirmed_network"
    NON_NETWORK = "confirmed_non_network"
    UNKNOWN = "unknown"


class AuthorizationStatus(str, Enum):
    NOT_RECORDED = "not_recorded"
    PENDING = "pending"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    DENIED = "denied"
    MORE_INFORMATION = "more_information_requested"


DOCUMENT_LABELS = {
    "hospital_estimate": "Hospital cost estimate",
    "doctor_advice": "Doctor advice / admission recommendation",
    "policy_schedule": "Policy schedule or e-card",
    "patient_id": "Patient identity document",
    "investigation_reports": "Relevant investigation reports",
}


class PreAuthorizationIntake(BaseModel):
    patient_name: str = Field(min_length=1, max_length=160)
    patient_consent: bool
    treatment: str = Field(min_length=2, max_length=160)
    estimated_cost: float = Field(gt=0, le=100_000_000)
    cost_notes: str = Field(default="", max_length=1000)
    hospital_name: str = Field(min_length=1, max_length=180)
    hospital_address: str = Field(min_length=1, max_length=500)
    hospital_contact: str = Field(default="", max_length=120)
    network_status: NetworkStatus = NetworkStatus.UNKNOWN
    insurer_name: str = Field(min_length=1, max_length=160)
    tpa_name: str = Field(default="", max_length=160)
    policy_number: str = Field(min_length=1, max_length=120)
    plan_name: str = Field(default="", max_length=160)
    policy_start: date | None = None
    policy_end: date | None = None
    document_types: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_consent_and_policy_dates(self):
        if not self.patient_consent:
            raise ValueError("Patient consent is required to prepare a pre-authorization request")
        if self.policy_start and self.policy_end and self.policy_end < self.policy_start:
            raise ValueError("Policy end date is earlier than its start date")
        unknown = set(self.document_types) - set(DOCUMENT_LABELS)
        if unknown:
            raise ValueError("Unsupported document type: " + ", ".join(sorted(unknown)))
        if len(set(self.document_types)) != len(self.document_types):
            raise ValueError("Document types must be unique")
        return self


def prepare_request(intake: PreAuthorizationIntake, policy: PolicyDoc | None,
                    attachment_names: list[str]) -> dict:
    checklist = [{"type": key, "label": label, "provided": key in intake.document_types,
                  "required": key in {"hospital_estimate", "doctor_advice", "policy_schedule"}}
                 for key, label in DOCUMENT_LABELS.items()]
    warnings: list[dict] = []
    if intake.network_status == NetworkStatus.UNKNOWN:
        warnings.append({"kind": "network_status", "message": "Network hospital status is unconfirmed. Ask the insurer or hospital to verify it."})
    if not policy:
        warnings.append({"kind": "policy", "message": "No policy wording was supplied, so treatment-specific restrictions could not be checked."})
        restriction_status, restriction_evidence = "unknown", []
    else:
        restriction_status, clauses = _restriction(policy, intake.treatment)
        restriction_evidence = [{"clause_id": clause.clause_id, "title": clause.title,
                                 "text": clause.text, "page": clause.page} for clause in clauses]
        if restriction_status in {"restricted", "conflict"}:
            warnings.append({"kind": "coverage_restriction", "message": (
                "The supplied policy text contains restriction wording relevant to this treatment; request insurer confirmation."
                if restriction_status == "restricted" else
                "The supplied policy text contains conflicting coverage and restriction wording for this treatment; request insurer confirmation."),
                "status": restriction_status})
        elif restriction_status == "unknown":
            warnings.append({"kind": "coverage_uncertain", "message": "No clear treatment-specific coverage clause was found. This does not mean the treatment is excluded."})
    policy_conflicts = []
    if policy:
        if policy.insurer and _normal(policy.insurer) != _normal(intake.insurer_name):
            policy_conflicts.append(f"Entered insurer '{intake.insurer_name}' differs from supplied policy insurer '{policy.insurer}'.")
        if policy.policy_number and intake.policy_number and _normal(policy.policy_number) != _normal(intake.policy_number):
            policy_conflicts.append("Entered policy number differs from the supplied policy document.")
    for conflict in policy_conflicts:
        warnings.append({"kind": "policy_conflict", "message": conflict})
    missing = [item["label"] for item in checklist if item["required"] and not item["provided"]]
    complete = not missing and not policy_conflicts
    summary = _summary(intake, checklist, warnings, restriction_status, attachment_names)
    return {"intake": intake.model_dump(mode="json"), "document_checklist": checklist,
            "missing_required_documents": missing, "documents_complete": complete,
            "coverage_restriction_status": restriction_status,
            "coverage_evidence": restriction_evidence, "warnings": warnings,
            "authorization_status": AuthorizationStatus.NOT_RECORDED.value,
            "status_source": "manual entry only", "summary": summary,
            "approval_disclaimer": "Preparation only. ClaimShield cannot grant pre-authorization or guarantee coverage. This request has not been submitted to an insurer or hospital.",
            "attachment_names": attachment_names}


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _summary(intake, checklist, warnings, restriction_status, attachment_names) -> str:
    documents = "\n".join(f"- [{'x' if item['provided'] else ' '}] {item['label']}" for item in checklist)
    concerns = "\n".join(f"- {warning['message']}" for warning in warnings) or "- No additional warning detected from the supplied information."
    attachments = ", ".join(attachment_names) if attachment_names else "None"
    return ("PRE-AUTHORIZATION PREPARATION SUMMARY\n\n"
            f"Patient: {intake.patient_name}\nTreatment: {intake.treatment}\nEstimated cost: {intake.estimated_cost:.2f}\n"
            f"Hospital: {intake.hospital_name}\nHospital address: {intake.hospital_address}\n"
            f"Hospital network status: {intake.network_status.value}\nInsurer: {intake.insurer_name}\nTPA: {intake.tpa_name or 'Not provided'}\n"
            f"Policy number: {intake.policy_number}\nPlan: {intake.plan_name or 'Not provided'}\n"
            f"Policy period: {intake.policy_start or 'Not provided'} to {intake.policy_end or 'Not provided'}\n"
            f"Treatment restriction review: {restriction_status}\n\nDOCUMENT CHECKLIST\n{documents}\n"
            f"\nUploaded supporting files: {attachments}\n\nITEMS FOR REVIEW\n{concerns}\n\n"
            "Authorization status: manually entered status only; no insurer response was retrieved.\n"
            "ClaimShield cannot grant pre-authorization. This preparation summary has not been submitted to an insurer or hospital.\n")
