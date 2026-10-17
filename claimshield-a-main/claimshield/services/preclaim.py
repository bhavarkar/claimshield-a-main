"""Conservative estimates of policy coverage before treatment.

This service deliberately keeps evidence provenance alongside extracted values. A missing
clause is not treated as a confirmed zero co-pay or an unlimited benefit.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from ..domain.coverage import entitlement
from ..domain.policy_terms import extract_terms, match_sub_limit
from ..models import Bill, Clause, LineItem, PolicyDoc, PolicyTerms
from ..money import parse_amount


class CostItem(BaseModel):
    description: str = Field(min_length=1, max_length=160)
    amount: float = Field(gt=0, le=100_000_000)
    category: str = "OTHER"

    @field_validator("category")
    @classmethod
    def known_category(cls, value: str) -> str:
        allowed = {"ROOM", "ICU", "CONSULT", "LAB", "RADIOLOGY", "PROCEDURE", "IMPLANT", "ANAESTHESIA",
                   "PHARMACY", "CONSUMABLE", "NON_MEDICAL", "PERSONAL", "THERAPY", "OTHER"}
        value = value.strip().upper().replace("NON-MEDICAL", "NON_MEDICAL")
        if value not in allowed:
            raise ValueError("Unknown cost category")
        return value


class PreClaimInput(BaseModel):
    treatment: str = Field(min_length=2, max_length=160)
    estimated_total: float = Field(gt=0, le=100_000_000)
    room_category: str = Field(min_length=1, max_length=80)
    room_rate_per_day: float | None = Field(default=None, ge=0, le=10_000_000)
    hospitalization_date: date
    nights: int = Field(default=1, ge=1, le=365)
    network_hospital: bool | None = None
    patient_age: int | None = Field(default=None, ge=0, le=120)
    deductible: float | None = Field(default=None, ge=0, le=100_000_000)
    cost_items: list[CostItem] = Field(default_factory=list, max_length=100)


class WhatIfScenario(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    estimated_total: float | None = Field(default=None, gt=0, le=100_000_000)
    room_category: str | None = Field(default=None, min_length=1, max_length=80)
    room_rate_per_day: float | None = Field(default=None, ge=0, le=10_000_000)
    hypothetical_sub_limit: float | None = Field(default=None, ge=0, le=100_000_000)
    hypothetical_copay_network_pct: float | None = Field(default=None, ge=0, le=100)
    hypothetical_copay_non_network_pct: float | None = Field(default=None, ge=0, le=100)


class WhatIfInput(BaseModel):
    base: PreClaimInput
    scenarios: list[WhatIfScenario] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_names(self):
        names = [scenario.name.strip().casefold() for scenario in self.scenarios]
        if len(set(names)) != len(names):
            raise ValueError("Scenario names must be unique")
        return self


def simulate_what_if(data: WhatIfInput, policy: PolicyDoc, retriever: Any | None = None) -> dict[str, Any]:
    """Evaluate isolated hypothetical scenarios through the deterministic pre-claim engine."""
    found = _clauses(policy, retriever)
    original_terms = extract_terms(found)
    baseline_result = assess_preclaim(data.base, policy, retriever, supplied_terms=original_terms.model_copy(deep=True))
    baseline = {"name": "Current policy estimate", "hypothetical": False, **baseline_result}
    scenarios = [baseline]
    for scenario in data.scenarios:
        terms = original_terms.model_copy(deep=True)
        overridden = []
        if scenario.hypothetical_sub_limit is not None:
            match = match_sub_limit(terms, data.base.treatment)
            if match:
                match.amount = scenario.hypothetical_sub_limit
            else:
                from ..models import SubLimit
                terms.sub_limits.append(SubLimit(procedure=data.base.treatment, amount=scenario.hypothetical_sub_limit))
            terms.clause_refs.setdefault("sub_limit", "hypothetical")
            overridden.append("procedure sublimit")
        if scenario.hypothetical_copay_network_pct is not None:
            terms.copay_network_pct = scenario.hypothetical_copay_network_pct
            overridden.append("network co-payment")
        if scenario.hypothetical_copay_non_network_pct is not None:
            terms.copay_non_network_pct = scenario.hypothetical_copay_non_network_pct
            overridden.append("non-network co-payment")
        scenario_data = data.base.model_copy(update={
            "estimated_total": scenario.estimated_total if scenario.estimated_total is not None else data.base.estimated_total,
            "room_category": scenario.room_category if scenario.room_category is not None else data.base.room_category,
            "room_rate_per_day": scenario.room_rate_per_day if scenario.room_rate_per_day is not None else data.base.room_rate_per_day,
        }, deep=True)
        outcome = assess_preclaim(scenario_data, policy, retriever, supplied_terms=terms)
        base_breakdown, next_breakdown = baseline_result["breakdown"], outcome["breakdown"]
        outcome.update({
            "name": scenario.name, "hypothetical": True,
            "scenario_inputs": {"estimated_total": scenario_data.estimated_total,
                                "room_category": scenario_data.room_category,
                                "room_rate_per_day": scenario_data.room_rate_per_day,
                                "hypothetical_sub_limit": scenario.hypothetical_sub_limit,
                                "hypothetical_copay_network_pct": scenario.hypothetical_copay_network_pct,
                                "hypothetical_copay_non_network_pct": scenario.hypothetical_copay_non_network_pct},
            "differences": {
                "insurer_payment": _difference(outcome["estimated_insurer_payment"], baseline_result["estimated_insurer_payment"]),
                "patient_payment": _difference(outcome["estimated_patient_payment"], baseline_result["estimated_patient_payment"]),
                "estimated_total": _difference(next_breakdown["estimated_total"], base_breakdown["estimated_total"]),
                "room_excess": _difference(next_breakdown["room_excess"], base_breakdown["room_excess"]),
                "after_sublimit": _difference(next_breakdown["after_sublimit"], base_breakdown["after_sublimit"]),
                "copay": _difference(next_breakdown["copay"], base_breakdown["copay"]),
            },
            "hypothetical_policy_values": {"sub_limit": scenario.hypothetical_sub_limit,
                                           "copay_network_pct": scenario.hypothetical_copay_network_pct,
                                           "copay_non_network_pct": scenario.hypothetical_copay_non_network_pct},
            "overridden_policy_rules": overridden,
        })
        scenarios.append(outcome)
    fixed = ["sum insured", "room rent limits", "ICU rent limit", "deductible", "senior co-payment", "non-medical coverage", "personal-item exclusion"]
    fixed_values = {"sum_insured": original_terms.sum_insured,
                    "room_rent_limit_per_day": original_terms.room_rent_limit,
                    "icu_rent_limit_per_day": original_terms.icu_rent_limit,
                    "deductible": _deductible(found.get("deductible")),
                    "senior_age": original_terms.senior_age,
                    "senior_copay_pct": original_terms.copay_senior_pct,
                    "non_medical_covered": original_terms.non_medical_covered,
                    "personal_items_excluded": original_terms.personal_items_excluded}
    return {"scenarios": scenarios, "real_policy_terms": original_terms.model_dump(mode="json"),
            "fixed_policy_rules": fixed, "fixed_policy_values": fixed_values,
            "notice": "Scenario overrides are hypothetical comparisons only and do not change the supplied policy or represent insurer approval."}


def _difference(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None:
        return None
    return round(value - baseline, 2)


def _clauses(policy: PolicyDoc, retriever: Any | None) -> dict[str, Clause | None]:
    queries = {
        "sum_insured": "sum insured policy maximum coverage",
        "room_rent": "normal room rent ICU per day limit",
        "copay": "co-payment network non-network hospital percentage",
        "sub_limit": "sub-limit treatment procedure",
        "non_medical": "non-medical consumables payable covered",
        "personal_items": "personal items excluded not payable",
        "deductible": "deductible per claim amount",
    }
    found: dict[str, Clause | None] = {}
    for topic, query in queries.items():
        hits = retriever.search(policy, query, top_k=1) if retriever else []
        if hits:
            found[topic] = hits[0]
            continue
        # Uploaded policies are not necessarily indexed. Never rely on cloud search alone.
        from .retrieval import LocalRetriever
        local_hits = LocalRetriever().search(policy, query, top_k=1)
        found[topic] = local_hits[0] if local_hits else None
    return found


def _deductible(clause: Clause | None) -> float | None:
    if not clause:
        return None
    text = clause.text
    match = re.search(r"deductible[^.]{0,100}?Rs\.?\s*([\d,]+(?:\.\d+)?)", text, re.I)
    if not match:
        match = re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)[^.]{0,60}?deductible", text, re.I)
    return parse_amount(match.group(1)) if match else None


def _restriction(policy: PolicyDoc, treatment: str) -> tuple[str, list[Clause]]:
    """Return a restriction state only when clause wording names this treatment."""
    generic = {"treatment", "procedure", "surgery", "planned", "total"}
    words = [w for w in re.findall(r"[a-z0-9]+", treatment.lower()) if len(w) > 3 and w not in generic]
    named = [c for c in policy.clauses if any(w in c.text.lower() for w in words)]
    covered = [c for c in named if re.search(r"\b(covered|payable|included)\b", c.text, re.I)]
    excluded = [c for c in named if re.search(r"\b(excluded|not covered|not payable|waiting period)\b", c.text, re.I)]
    if covered and excluded:
        by_id = {c.clause_id: c for c in covered + excluded}
        return "conflict", list(by_id.values())
    if excluded:
        return "restricted", excluded
    if covered:
        return "covered", covered
    broad = [c for c in policy.clauses if re.search(r"(will pay|are payable|covered hospitalisation|covered hospitalization)", c.text, re.I)]
    return ("covered", broad[:1]) if broad else ("unknown", [])


def assess_preclaim(data: PreClaimInput, policy: PolicyDoc, retriever: Any | None = None,
                    supplied_terms: PolicyTerms | None = None) -> dict[str, Any]:
    found = _clauses(policy, retriever)
    extracted = extract_terms(found)
    terms = supplied_terms or extracted
    deductible = data.deductible if data.deductible is not None else _deductible(found.get("deductible"))
    missing: list[str] = []
    uncertainty: list[str] = ["This is an estimate from the supplied information, not a guarantee of claim approval or settlement."]
    applied: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    def cite(topic: str) -> Clause | None:
        clause = found.get(topic)
        if clause:
            evidence.append({"topic": topic, "clause_id": clause.clause_id, "title": clause.title,
                             "quote": clause.text, "source": clause.source, "page": clause.page})
        return clause

    for topic, label, value in (
        ("sum_insured", "Available sum insured", terms.sum_insured),
        ("room_rent", "Room-rent limit", terms.room_rent_limit),
        ("copay", "Co-payment", None),
        ("sub_limit", "Procedure sublimit", match_sub_limit(terms, data.treatment)),
        ("non_medical", "Non-medical coverage", terms.non_medical_covered if "non_medical" in terms.clause_refs else None),
    ):
        if value is None:
            missing.append(label)
        cite(topic)

    if "copay" not in terms.clause_refs:
        missing.append("Policy co-payment terms")
    if deductible is None:
        missing.append("Policy deductible terms")

    if data.network_hospital is None:
        if abs(terms.copay_network_pct - terms.copay_non_network_pct) > 0.001:
            missing.append("Whether the hospital is in the insurer's network")
        else:
            pct = terms.copay_network_pct
    else:
        pct = terms.copay_network_pct if data.network_hospital else terms.copay_non_network_pct
    if data.patient_age is None and terms.senior_age:
        missing.append("Patient age (policy has an age-based co-payment)")
    elif data.patient_age is not None and terms.senior_age and data.patient_age >= terms.senior_age:
        pct = (terms.copay_network_pct if data.network_hospital is not False else terms.copay_non_network_pct) + terms.copay_senior_pct
    else:
        pct = terms.copay_network_pct if data.network_hospital is not False else terms.copay_non_network_pct

    categories = {"general": "ROOM", "shared": "ROOM", "semi-private": "ROOM", "private": "ROOM", "icu": "ICU"}
    room_kind = categories.get(data.room_category.strip().lower())
    room_limit = terms.icu_rent_limit if room_kind == "ICU" else terms.room_rent_limit
    room_excess = 0.0
    if data.room_rate_per_day is not None and room_limit is not None:
        room_excess = max(data.room_rate_per_day - room_limit, 0) * data.nights
    elif data.room_rate_per_day is None:
        missing.append("Estimated room rate per day")
    if room_kind is None:
        uncertainty.append(f"Room category '{data.room_category}' could not be mapped to a policy room category; no room cap was applied.")

    status, restriction_clauses = _restriction(policy, data.treatment)
    excluded_treatment = status == "restricted"
    if status == "conflict":
        uncertainty.append("Policy clauses contain both coverage and restriction wording relevant to this treatment; ask the insurer to confirm before relying on the estimate.")
    elif status == "restricted":
        uncertainty.append("A policy clause appears to restrict this treatment. The estimate conservatively treats the estimated costs as not payable pending insurer confirmation.")
    elif status == "unknown":
        missing.append("A policy clause that clearly confirms eligibility for this treatment")
    for clause in restriction_clauses:
        evidence.append({"topic": "treatment_restriction", "clause_id": clause.clause_id, "title": clause.title,
                         "quote": clause.text, "source": clause.source, "page": clause.page})

    if data.cost_items and abs(sum(i.amount for i in data.cost_items) - data.estimated_total) > 0.5:
        raise ValueError("Cost item amounts must add up to the estimated total.")
    unresolved_exclusion = any(i.category in {"NON_MEDICAL", "PERSONAL"} for i in data.cost_items) and not (
        "non_medical" in terms.clause_refs and "personal_items" in terms.clause_refs
    )
    if unresolved_exclusion:
        missing.append("Policy treatment of the listed non-medical or personal expense(s)")
    if data.cost_items:
        lines = []
        for i, item in enumerate(data.cost_items):
            is_room = item.category in {"ROOM", "ICU"}
            rate = (data.room_rate_per_day if is_room and data.room_rate_per_day is not None
                    else item.amount / data.nights if is_room else item.amount)
            lines.append(LineItem(line_no=i + 1, service_date=data.hospitalization_date.isoformat(), description=item.description,
                                  dept=item.category, category=item.category, qty=item.amount / rate if is_room and rate else 1,
                                  unit_price=rate, amount=item.amount))
    else:
        lines = [LineItem(line_no=1, service_date=data.hospitalization_date.isoformat(), description=data.treatment,
                          category="PROCEDURE", dept="Procedure", qty=1, unit_price=data.estimated_total, amount=data.estimated_total)]
        if data.room_rate_per_day is not None and data.room_rate_per_day * data.nights <= data.estimated_total:
            # Separate room expense so the existing entitlement engine applies its policy cap.
            lines[0].amount -= data.room_rate_per_day * data.nights
            lines[0].unit_price = lines[0].amount
            lines.append(LineItem(line_no=2, service_date=data.hospitalization_date.isoformat(), description=f"{data.room_category} room",
                                  category=room_kind or "ROOM", dept="Room", qty=data.nights,
                                  unit_price=data.room_rate_per_day, amount=data.room_rate_per_day * data.nights))

    unknown_network_share = data.network_hospital is None and abs(terms.copay_network_pct - terms.copay_non_network_pct) > 0.001
    unknown_room_cost = room_limit is not None and (data.room_rate_per_day is None or room_kind is None)
    unknown_room_rule = "room_rent" not in terms.clause_refs
    unknown_sublimit_rule = "sub_limit" not in terms.clause_refs
    if (status in {"unknown", "conflict"} or "copay" not in terms.clause_refs or unknown_network_share
            or unresolved_exclusion or unknown_room_cost or unknown_room_rule or unknown_sublimit_rule):
        estimated_insurer = None
        patient_payment = None
        breakdown = {"estimated_total": round(data.estimated_total, 2), "excluded_estimate": None,
                     "room_excess": round(room_excess, 2), "after_sublimit": None, "copay": None,
                     "deductible": deductible, "sum_insured_cap": terms.sum_insured,
                     "estimated_insurer_payment": None, "estimated_patient_payment": None}
        uncertainty.append("The supplied policy and estimate do not establish enough treatment eligibility or cost-sharing terms for a reliable payment estimate.")
    elif excluded_treatment:
        estimated_insurer = 0.0
        patient_payment = data.estimated_total
        breakdown = {"estimated_total": round(data.estimated_total, 2), "excluded_estimate": round(data.estimated_total, 2),
                     "room_excess": 0.0, "after_sublimit": 0.0, "copay": 0.0, "deductible": 0.0,
                     "sum_insured_cap": terms.sum_insured, "estimated_insurer_payment": 0.0,
                     "estimated_patient_payment": round(data.estimated_total, 2)}
    else:
        bill = Bill(procedure=data.treatment, patient_age=data.patient_age,
                    network_hospital=data.network_hospital is not False, admission_date=data.hospitalization_date.isoformat(),
                    discharge_date=data.hospitalization_date.isoformat(), line_items=lines, stated_gross_total=data.estimated_total)
        result = entitlement(bill, terms)
        room_excess = result.room_excess
        if room_limit is not None and room_excess:
            applied.append({"rule": "room_rent_cap", "limit_per_day": room_limit, "estimated_excess": round(room_excess, 2)})
        deductible_used = min(deductible or 0.0, result.insurer_payable)
        estimated_insurer = round(max(result.insurer_payable - deductible_used, 0.0), 2)
        patient_payment = round(data.estimated_total - estimated_insurer, 2)
        if deductible is not None:
            applied.append({"rule": "deductible", "amount": deductible, "application_order": "after co-payment and sublimit"})
        if result.copay_pct:
            applied.append({"rule": "co_payment", "percentage": result.copay_pct, "amount": result.copay})
        if result.sub_limit_applied is not None:
            applied.append({"rule": "procedure_sublimit", "amount": result.sub_limit_applied})
        if result.non_payable:
            applied.append({"rule": "excluded_items", "amount": result.non_payable})
        breakdown = {"estimated_total": result.gross, "excluded_estimate": result.non_payable,
                     "room_excess": round(room_excess, 2), "admissible_estimate": result.admissible,
                     "sub_limit": result.sub_limit_applied, "after_sublimit": result.after_sub_limit,
                     "copay_percentage": result.copay_pct, "copay": result.copay,
                     "deductible": deductible_used, "sum_insured_cap": terms.sum_insured,
                     "estimated_insurer_payment": estimated_insurer, "estimated_patient_payment": patient_payment}

    if data.hospitalization_date:
        uncertainty.append("Waiting periods, policy start date, pre-existing-condition terms, remaining sum insured, network status, medical necessity, and final admissible expenses may change the result.")
    return {"estimated_insurer_payment": estimated_insurer, "estimated_patient_payment": patient_payment,
            "breakdown": breakdown, "applied_rules": applied, "missing_inputs": sorted(set(missing)),
            "uncertainty": uncertainty, "policy_evidence": evidence, "treatment_status": status,
            "hospitalization_date": data.hospitalization_date.isoformat()}
