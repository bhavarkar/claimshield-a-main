"""Version-aware policy dates and waiting-period review with explicit continuity gaps."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..models import PolicyDoc


class PolicyDocumentType(str, Enum):
    CURRENT_POLICY = "current_policy"
    RENEWAL = "renewal"
    ENDORSEMENT = "endorsement"
    PRIOR_POLICY = "prior_policy"
    PORTABILITY = "portability_certificate"


class ContinuityStatus(str, Enum):
    CONFIRMED = "confirmed_with_documents"
    NOT_CONFIRMED = "not_confirmed"
    UNKNOWN = "unknown"


class PolicyDocumentDescriptor(BaseModel):
    document_type: PolicyDocumentType


class PolicyHistoryRequest(BaseModel):
    treatment: str = Field(min_length=2, max_length=160)
    treatment_date: date
    continuity_status: ContinuityStatus = ContinuityStatus.UNKNOWN
    documents: list[PolicyDocumentDescriptor] = Field(min_length=1, max_length=10)


def extract_policy_dates(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start, end, renewal = None, None, None
    endorsements: list[str] = []
    endorsement_effective = None
    for line in lines:
        low = line.casefold()
        dates = _dates(line)
        if re.search(r"endorsement", low):
            endorsements.append(line[:300])
            if dates and re.search(r"effective|dated", low):
                endorsement_effective = dates[-1]
        if dates and re.search(r"(policy )?(start|inception|commencement|effective from|from date|period of insurance from|period of insurance|policy period)", low):
            start = start or dates[0]
        if dates and re.search(r"(policy )?(expiry|expir|end date|valid till|valid until|period of insurance.*to|policy period)", low):
            end = end or dates[-1]
        if dates and re.search(r"renewal", low):
            renewal = dates[-1]
            start = start or dates[0]
    return {"policy_start_date": start.isoformat() if start else None,
            "policy_expiry_date": end.isoformat() if end else None,
            "renewal_date": renewal.isoformat() if renewal else None,
            "endorsement_effective_date": endorsement_effective.isoformat() if endorsement_effective else None,
            "endorsements": endorsements}


def _dates(text: str) -> list[date]:
    patterns = [r"\b\d{4}-\d{1,2}-\d{1,2}\b", r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b"]
    found: list[date] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            raw = match.group()
            try:
                parsed = date.fromisoformat(raw) if "-" in raw and raw[:4].isdigit() else None
                if parsed is None:
                    separator = "/" if "/" in raw else "-"
                    parsed = datetime.strptime(raw, f"%d{separator}%m{separator}%Y").date()
                found.append(parsed)
            except ValueError:
                continue
    return sorted(set(found))


def check_policy_history(request: PolicyHistoryRequest, documents: list[dict[str, Any]], retriever) -> dict[str, Any]:
    versions = []
    all_relevant_waiting: list[dict] = []
    for descriptor, item in zip(request.documents, documents):
        policy: PolicyDoc = item["policy"]
        text = item["text"]
        dates = extract_policy_dates(text)
        digest = hashlib.sha256(item["bytes"]).hexdigest()
        version_id = f"{descriptor.document_type.value}-sha256-{digest[:12]}"
        continuity_evidence = bool(re.search(r"(portability|continuity)[^.;]{0,100}(credit|benefit|accepted|confirmed|transferred|maintained)|"
                                             r"(credit|benefit|accepted|confirmed|transferred|maintained)[^.;]{0,100}(portability|continuity)",
                                             text, re.I))
        versions.append({"version_id": version_id, "document_type": descriptor.document_type.value,
                         "filename": item["filename"], "sha256": digest,
                         "policy_number": policy.policy_number or None, "insurer": policy.insurer or None,
                         "plan_name": policy.plan_name or None, "continuity_evidence_found": continuity_evidence, **dates})
        for clause in _relevant_waiting_clauses(policy, request.treatment, retriever):
            periods = _waiting_periods(clause.text)
            all_relevant_waiting.append({"version_id": version_id, "document_type": descriptor.document_type.value,
                                         "clause_id": clause.clause_id, "title": clause.title,
                                         "text": clause.text, "page": clause.page,
                                         "waiting_periods": periods})

    conflicts = _conflicts(versions)
    current_candidates = [v for v in versions if v["document_type"] in {"current_policy", "renewal"}]
    applicable = _applicable_version(current_candidates, request.treatment_date)
    relevant_version_ids = {applicable["version_id"]} if applicable else set()
    endorsement_effective_date_missing = False
    for version in versions:
        if version["document_type"] == "endorsement":
            effective = version.get("endorsement_effective_date") or version.get("policy_start_date")
            if effective:
                if date.fromisoformat(effective) <= request.treatment_date:
                    relevant_version_ids.add(version["version_id"])
            else:
                endorsement_effective_date_missing = True
    relevant_waiting = [clause for clause in all_relevant_waiting if clause["version_id"] in relevant_version_ids]
    effective_start = date.fromisoformat(applicable["policy_start_date"]) if applicable and applicable.get("policy_start_date") else None
    months_active = _months_between(effective_start, request.treatment_date) if effective_start else None
    has_history_document = any(v.get("document_type") in {"prior_policy", "portability_certificate"} for v in versions)
    continuity_evidence_available = any(v.get("document_type") == "portability_certificate" and v.get("continuity_evidence_found") for v in versions)
    continuity_known = request.continuity_status == ContinuityStatus.CONFIRMED and continuity_evidence_available
    waiting_results = []
    for clause in relevant_waiting:
        for period in clause["waiting_periods"]:
            if request.continuity_status == ContinuityStatus.UNKNOWN or (request.continuity_status == ContinuityStatus.CONFIRMED and not continuity_evidence_available):
                status = "continuity_unknown"
            elif request.continuity_status == ContinuityStatus.NOT_CONFIRMED:
                status = "continuity_not_confirmed"
            elif months_active is None:
                status = "effective_start_unknown"
            elif period.get("days") is not None:
                elapsed_days = (request.treatment_date - effective_start).days if effective_start else None
                status = "period_elapsed" if elapsed_days is not None and elapsed_days >= period["days"] else "period_may_apply"
            else:
                status = "period_elapsed" if months_active >= period["months"] else "period_may_apply"
            elapsed_days = (request.treatment_date - effective_start).days if effective_start else None
            waiting_results.append({**clause, **period, "months_since_current_start": months_active,
                                    "days_since_current_start": elapsed_days, "status": status})
    missing = []
    if not any(v.get("document_type") == "prior_policy" for v in versions):
        missing.append("Prior policy schedule or certificate showing prior coverage dates")
    if not any(v.get("document_type") == "portability_certificate" for v in versions):
        missing.append("Portability or continuity confirmation, if applicable")
    if request.continuity_status != ContinuityStatus.CONFIRMED or not continuity_evidence_available:
        missing.append("Portability / continuity certificate explicitly confirming continuity credits")
    if not has_history_document:
        missing.append("Prior policy schedule or portability document showing historical coverage")
    if not applicable or not applicable.get("policy_start_date"):
        missing.append("Current policy effective/start date")
    warnings = ["Renewal dates by themselves do not establish uninterrupted coverage or portability credits."]
    if endorsement_effective_date_missing:
        warnings.append("An uploaded endorsement has no readable effective date, so its applicability to the treatment date is uncertain.")
    if conflicts:
        warnings.append("The uploaded documents contain conflicting policy identifiers or effective dates; resolve these with the insurer.")
    if applicable and applicable.get("policy_expiry_date"):
        expiry = date.fromisoformat(applicable["policy_expiry_date"])
        if not (effective_start and effective_start <= request.treatment_date <= expiry):
            warnings.append("The treatment date is outside the effective dates extracted from the selected policy document.")
    elif current_candidates and not applicable:
        warnings.append("No current or renewal document has extracted effective dates covering the treatment date.")
    if not waiting_results:
        warnings.append("No clearly relevant waiting-period clause was found in the parsed documents; this is not confirmation that no waiting period applies.")
    return {"treatment": request.treatment, "treatment_date": request.treatment_date.isoformat(),
            "continuity_status": request.continuity_status.value,
            "continuity_evidence_available": continuity_evidence_available,
            "continuity_used_for_waiting_check": continuity_known, "policy_versions": versions,
            "applicable_policy_version": applicable["version_id"] if applicable else None,
            "applicable_policy_dates": ({"start": applicable.get("policy_start_date"), "expiry": applicable.get("policy_expiry_date"),
                                         "renewal": applicable.get("renewal_date")} if applicable else None),
            "waiting_periods": waiting_results, "missing_historical_documents": missing,
            "conflicts": conflicts, "warnings": warnings,
            "notice": "This document review does not decide coverage. Continuity is not inferred from renewal dates."}


def _relevant_waiting_clauses(policy: PolicyDoc, treatment: str, retriever) -> list:
    hits = retriever.search(policy, f"waiting period {treatment} pre-existing specified disease", top_k=8) if retriever else []
    tokens = [w for w in re.findall(r"[a-z0-9]+", treatment.casefold()) if len(w) > 3]
    out, seen = [], set()
    for clause in hits + policy.clauses:
        text = f"{clause.title} {clause.text}".casefold()
        if "wait" not in text or not _waiting_periods(clause.text):
            continue
        if tokens and not any(token in text for token in tokens) and not re.search(r"pre-existing|specified disease|initial waiting", text):
            continue
        if clause.clause_id not in seen:
            seen.add(clause.clause_id)
            out.append(clause)
    return out


def _waiting_periods(text: str) -> list[dict]:
    out = []
    for match in re.finditer(r"(?:(pre-existing|specified disease|initial|waiting)[^.;]{0,100}?)\b(\d+)\s*(days?|months?|years?)\b|\b(\d+)\s*(days?|months?|years?)\s*(?:waiting period|waiting)", text, re.I):
        label = (match.group(1) or "waiting").strip().casefold()
        count = int(match.group(2) or match.group(4))
        unit = (match.group(3) or match.group(5)).casefold()
        months = count * 12 if unit.startswith("year") else count if unit.startswith("month") else None
        days = count if unit.startswith("day") else None
        if count > 0:
            out.append({"condition": label, "duration": f"{count} {unit}", "months": months, "days": days})
    return out


def _applicable_version(candidates: list[dict], treatment_date: date) -> dict | None:
    dated = []
    for version in candidates:
        start = version.get("policy_start_date")
        if start:
            try:
                parsed = date.fromisoformat(start)
                expiry = date.fromisoformat(version["policy_expiry_date"]) if version.get("policy_expiry_date") else None
                if parsed <= treatment_date and (expiry is None or treatment_date <= expiry):
                    dated.append((parsed, version))
            except ValueError:
                pass
    return max(dated, key=lambda pair: pair[0])[1] if dated else None


def _months_between(start: date | None, end: date) -> int | None:
    if not start or end < start:
        return None
    return (end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0)


def _conflicts(versions: list[dict]) -> list[dict]:
    conflicts = []
    identifiers: dict[str, set[str]] = {"policy_number": set(), "insurer": set()}
    for version in versions:
        for field in identifiers:
            value = version.get(field)
            if value:
                identifiers[field].add(re.sub(r"\W", "", value.casefold()))
    for field, values in identifiers.items():
        if len(values) > 1:
            conflicts.append({"kind": field, "message": f"Uploaded documents contain different {field.replace('_', ' ')} values."})
    by_type: dict[str, list[dict]] = {}
    for version in versions:
        by_type.setdefault(version["document_type"], []).append(version)
    for kind, items in by_type.items():
        starts = {i["policy_start_date"] for i in items if i.get("policy_start_date")}
        if len(starts) > 1:
            conflicts.append({"kind": "effective_date", "document_type": kind,
                              "message": f"Documents labeled {kind} have conflicting policy start dates."})
    return conflicts
