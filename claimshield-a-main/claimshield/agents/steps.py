"""The six ClaimShield agents' work, as plain functions over a JSON-able state dict.

Each step reads what earlier steps wrote to `state`, writes its own keys into the returned
delta, and narrates everything it does to the case trace. `adk_agents.py` wraps each step in an
ADK agent; `runner.py` can also call them directly when ADK isn't installed.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from datetime import date
from typing import Any, Callable

from ..domain.coverage import coverage_findings, entitlement
from ..domain.policy_terms import extract_terms, match_sub_limit
from ..models import (Bill, Clause, Finding, FindingType, Letter, PolicyCitation, PolicyDoc, PolicyTerms,
                      Reconciliation)
from ..money import inr
from ..services import Services
from ..services.llm import merge_terms
from ..services.parsing import ParseError, categorize, charge_unit, normalize_bill, normalize_policy, policy_document_identity
from ..services.finding_evidence import attach_finding_evidence
from ..services.healthcare_audit import audit_from_state
from ..trace import Trace

Step = Callable[[dict[str, Any], Trace, Services], dict[str, Any]]

SOFT_DUPLICATE_CATEGORIES = {"PHARMACY", "THERAPY", "CONSUMABLE", "OTHER"}


def _bill(state: dict[str, Any]) -> Bill:
    return Bill.model_validate(state["bill"])


def _policy(state: dict[str, Any]) -> PolicyDoc:
    return PolicyDoc.model_validate(state["policy_doc"])


def _terms(state: dict[str, Any]) -> PolicyTerms:
    return PolicyTerms.model_validate(state["policy_terms"])


def _cite_fn(state: dict[str, Any]) -> Callable[[str], PolicyCitation | None]:
    clauses = {k: Clause.model_validate(v) for k, v in state.get("clauses", {}).items() if v}

    def cite(topic: str) -> PolicyCitation | None:
        c = clauses.get(topic)
        return PolicyCitation(clause_id=c.clause_id, title=c.title, quote=c.text, source=c.source,
                              page=c.page, document_id=c.document_id, version_id=c.version_id) if c else None

    return cite


def _uncertain_policy_topics(found: dict[str, Clause | None]) -> list[str]:
    markers = re.compile(r"\b(?:may be considered|at the discretion|as determined by the insurer|unclear|ambiguous)\b", re.I)
    return [topic for topic, clause in found.items() if clause and markers.search(clause.text)]


def _ids(prefix: str) -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"{prefix}{next(counter)}"


# ---------------------------------------------------------------------------------------
# 1. Intake
# ---------------------------------------------------------------------------------------


def intake_step(state: dict[str, Any], trace: Trace, svc: Services) -> dict[str, Any]:
    agent = "intake_agent"
    trace.emit(agent, "start", "Reading the hospital bill and the insurance policy")
    known = svc.store.categories()

    def parse(key: str, label: str):
        data = svc.storage.get(key)
        trace.emit(agent, "tool", f"{svc.parser.name}: processing {label} ({len(data) // 1024} KB)")
        try:
            return svc.parser.parse(data), data
        except Exception as exc:  # noqa: BLE001 - runtime cloud failure, fall back to local parsing
            if svc.parser is svc.local_parser:
                raise
            trace.emit(agent, "warn", f"{svc.parser.name} failed ({type(exc).__name__}); parsing locally instead")
            return svc.local_parser.parse(data), data

    raw_bill, bill_bytes = parse(state["bill_key"], state.get("bill_name", "bill"))
    try:
        bill = normalize_bill(raw_bill, known)
    except ParseError as exc:
        if not svc.llm.available:
            raise
        trace.emit(agent, "tool", f"Layout not recognised ({exc}); asking {svc.llm.name} to transcribe the bill")
        data = svc.llm.extract_bill(bill_bytes)
        bill = Bill.model_validate({**data, "parser": svc.llm.name})
        for li in bill.line_items:
            li.category = categorize(li.dept, li.description, known, li.code)  # type: ignore[assignment]
            if 'unit' not in li.model_fields_set:
                li.unit = charge_unit(li.category, li.description)
    trace.emit(
        agent, "result",
        f"Bill: {bill.hospital} - {len(bill.line_items)} line items, {len(bill.deductions)} insurer deductions, "
        f"gross {inr(bill.stated_gross_total)}, insurer approved {inr(bill.insurer_approved)}",
        hospital=bill.hospital, procedure=bill.procedure, lines=len(bill.line_items),
    )

    raw_policy, policy_bytes = parse(state["policy_key"], state.get("policy_name", "policy"))
    document_id = policy_document_identity(policy_bytes)
    policy = normalize_policy(raw_policy, state.get("policy_name", "policy.pdf"),
                              document_id=document_id, version_id=document_id)
    trace.emit(agent, "result", f"Policy: {policy.plan_name} ({policy.insurer}) - {len(policy.clauses)} numbered clauses")

    if bill.policy_number and policy.policy_number and bill.policy_number != policy.policy_number:
        trace.emit(agent, "warn", f"Bill quotes policy {bill.policy_number} but the uploaded policy is {policy.policy_number}")

    n = svc.store.load_case(state.get('analysis_case_id', state['case_id']), bill)
    trace.emit(agent, "tool", f"{svc.store.name}: loaded {n} rows into line_items for case {state['case_id']}")
    return {"bill": bill.model_dump(), "policy_doc": policy.model_dump()}


# ---------------------------------------------------------------------------------------
# 2. Policy
# ---------------------------------------------------------------------------------------


def policy_queries(bill: Bill) -> dict[str, str]:
    return {
        "sum_insured": "sum insured per policy year",
        "room_rent": "room rent ICU charges per day limit",
        "copay": "co-payment admissible claim network hospital",
        "sub_limit": f"sub-limits {bill.procedure}",
        "non_medical": "non-medical items consumables admission kit gloves masks payable",
        "personal_items": "personal comfort items attendant food toiletries",
        "actually_incurred": "duplicate entries arithmetical errors expenses actually incurred",
        "reasonable_customary": "reasonable and customary charges prevailing charges geographical area",
        "grievance": "grievance claim decision ombudsman",
    }


def policy_step(state: dict[str, Any], trace: Trace, svc: Services) -> dict[str, Any]:
    agent = "policy_agent"
    bill, policy = _bill(state), _policy(state)
    trace.emit(agent, "start", "Finding the policy clauses that govern this claim")
    found: dict[str, Clause | None] = {}
    for topic, query in policy_queries(bill).items():
        try:
            hits = svc.retriever.search(policy, query, top_k=1)
        except Exception as exc:  # noqa: BLE001 - search outage: fall back to the local index
            trace.emit(agent, "warn", f"{svc.retriever.name} failed ({type(exc).__name__}); using local retrieval")
            from ..services.retrieval import LocalRetriever

            hits = LocalRetriever().search(policy, query, top_k=1)
        found[topic] = hits[0] if hits else None
        shown = f"{hits[0].cite()} (p.{hits[0].page})" if hits else "no matching clause"
        backend = getattr(svc.retriever, "last_backend", svc.retriever.name)
        trace.emit(agent, "tool", f"{backend}: \"{query}\" -> {shown}", topic=topic)

    terms = extract_terms(found)
    uncertain_topics = _uncertain_policy_topics(found)
    terms.uncertain_topics = uncertain_topics
    for topic in uncertain_topics:
        trace.emit(agent, "warn", f"Policy wording for {topic} is ambiguous; its extracted value is marked uncertain and requires review")
    if svc.llm.available:
        try:
            joined = "\n\n".join(f"{c.clause_id} {c.title}. {c.text}" for c in found.values() if c)
            terms, disagreements = merge_terms(terms, svc.llm.extract_policy_terms(joined))
            trace.emit(agent, "tool", f"{svc.llm.name}: cross-checked extracted terms against the clause text")
            for d in disagreements:
                trace.emit(agent, "warn", f"Rules and Gemini disagree on {d}; keeping the rule-based value")
        except Exception as exc:  # noqa: BLE001
            trace.emit(agent, "warn", f"Gemini term extraction failed ({type(exc).__name__}); using rule-based terms")

    sl = match_sub_limit(terms, bill.procedure)
    parts = []
    if terms.room_rent_limit:
        parts.append(f"room rent up to {inr(terms.room_rent_limit)}/day")
    parts.append(f"co-pay {terms.copay_network_pct:g}% network / {terms.copay_non_network_pct:g}% non-network"
                 + (f" (+{terms.copay_senior_pct:g}% if aged {terms.senior_age}+)" if terms.copay_senior_pct else ""))
    parts.append(f"sub-limit for this procedure: {inr(sl.amount)} per {sl.basis}" if sl else "no sub-limit for this procedure")
    parts.append("non-medical items covered" if terms.non_medical_covered else "non-medical items not covered")
    trace.emit(agent, "result", "Policy terms: " + "; ".join(parts), terms=terms.model_dump())
    return {
        "policy_terms": terms.model_dump(),
        "clauses": {k: (v.model_dump() if v else None) for k, v in found.items()},
    }


# ---------------------------------------------------------------------------------------
# 3. Audit
# ---------------------------------------------------------------------------------------


def audit_step(state: dict[str, Any], trace: Trace, svc: Services) -> dict[str, Any]:
    agent = "audit_agent"
    bill, terms, cite = _bill(state), _terms(state), _cite_fn(state)
    case_id = state.get('analysis_case_id', state['case_id'])
    next_id = _ids("A")
    findings: list[Finding] = []
    by_line = {li.line_no: li for li in bill.line_items}
    trace.emit(agent, "start", "Checking the bill for duplicates, arithmetic errors and settlement mismatches")

    # Duplicates
    rows = svc.store.query("duplicates", case_id=case_id)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["service_date"], r["code"] or r["description"].lower(), round(r["qty"], 2), round(r["unit_price"], 2))].append(r)
    trace.emit(agent, "tool", f"{svc.store.name}: duplicates.sql -> {len(groups)} repeated charge group(s)")
    for (svc_date, _, _, _), grp in groups.items():
        lines = sorted(r["line_no"] for r in grp)
        first = by_line[lines[0]]
        soft = first.category in SOFT_DUPLICATE_CATEGORIES
        extra = first.amount * (len(lines) - 1)
        why = (" Two sessions or doses on the same day can be legitimate, so ask the hospital for the treatment record."
               if soft else " Unless the test or service was genuinely performed twice, one entry is a duplicate.")
        findings.append(Finding(
            finding_id=next_id(), type=FindingType.DUPLICATE_CHARGE, title=f"{first.description} charged {len(lines)} times on {svc_date}",
            bill_lines=lines,
            bill_evidence="; ".join(f"Line {n}: {svc_date} {by_line[n].code} {by_line[n].description} x{by_line[n].qty:g} @ {inr(by_line[n].unit_price)} = {inr(by_line[n].amount)}" for n in lines),
            policy=cite("actually_incurred"),
            explanation=f"Lines {', '.join(map(str, lines))} charge the same service on the same day at the same price.{why}",
            confidence="Worth reviewing" if soft else "High", impact=extra, counterparty="hospital",
            bill_correction=extra, correction_line=lines[-1], detected_by=agent, method="BigQuery SQL (duplicates.sql)",
        ))

    # Quantity x rate
    rows = svc.store.query("arithmetic", case_id=case_id)
    trace.emit(agent, "tool", f"{svc.store.name}: arithmetic.sql -> {len(rows)} line(s) where qty x rate != amount")
    for r in rows:
        over = round(r["amount"] - r["expected_amount"], 2)
        if over <= 0:
            continue
        findings.append(Finding(
            finding_id=next_id(), type=FindingType.ARITHMETIC_ERROR, title=f"{r['description']}: amount doesn't match quantity x rate",
            bill_lines=[r["line_no"]],
            bill_evidence=f"Line {r['line_no']}: {r['qty']:g} x {inr(r['unit_price'])} = {inr(r['expected_amount'])}, but billed {inr(r['amount'])}",
            policy=cite("actually_incurred"),
            explanation=f"{r['qty']:g} units at {inr(r['unit_price'])} come to {inr(r['expected_amount'])}, but the bill charges {inr(r['amount'])}, {inr(over)} more.",
            confidence="High", impact=over, counterparty="hospital", bill_correction=over, correction_line=r["line_no"],
            detected_by=agent, method="BigQuery SQL (arithmetic.sql)",
        ))

    # Room days vs length of stay
    allowed = max(bill.nights, 1)
    room_lines = [li for li in bill.line_items if li.category in ("ROOM", "ICU")]
    billed_days = sum(li.qty for li in room_lines)
    trace.emit(agent, "tool", f"Length-of-stay check: {bill.admission_date} to {bill.discharge_date} = {allowed} day(s); room billed for {billed_days:g}")
    if room_lines and billed_days > allowed + 0.01:
        extra_days = billed_days - allowed
        li = max(room_lines, key=lambda x: x.unit_price)
        over = round(extra_days * li.unit_price, 2)
        findings.append(Finding(
            finding_id=next_id(), type=FindingType.QUANTITY_MISMATCH, title="Room charged for more days than the stay",
            bill_lines=[x.line_no for x in room_lines],
            bill_evidence=f"Line {li.line_no}: {li.description} x{li.qty:g} days; admitted {bill.admission_date}, discharged {bill.discharge_date} ({allowed} nights)",
            policy=cite("actually_incurred"),
            explanation=f"The stay was {allowed} nights but the room is billed for {billed_days:g} days. The extra {extra_days:g} day(s) come to {inr(over)}.",
            confidence="High", impact=over, counterparty="hospital", bill_correction=over, correction_line=li.line_no,
            detected_by=agent, method="length-of-stay rule",
        ))

    # Gross total vs sum of lines
    line_sum = round(sum(li.amount for li in bill.line_items), 2)
    trace.emit(agent, "tool", f"Totals check: lines sum to {inr(line_sum)}; bill states gross {inr(bill.stated_gross_total)}")
    if bill.stated_gross_total > line_sum + 0.5:
        gap = round(bill.stated_gross_total - line_sum, 2)
        findings.append(Finding(
            finding_id=next_id(), type=FindingType.TOTAL_MISMATCH, title="Gross total is more than the sum of the line items",
            bill_lines=[], bill_evidence=f"Sum of {len(bill.line_items)} itemised lines = {inr(line_sum)}; 'Gross Total' printed = {inr(bill.stated_gross_total)}",
            policy=cite("actually_incurred"),
            explanation=f"The itemised charges add up to {inr(line_sum)}, but the bill's gross total is {inr(bill.stated_gross_total)}. The {inr(gap)} difference isn't backed by any line item.",
            confidence="High", impact=gap, counterparty="hospital", bill_correction=gap, correction_line=None,
            detected_by=agent, method="totals reconciliation",
        ))

    # Settlement vs policy
    cov, notes = coverage_findings(bill, terms, cite, next_id)
    trace.emit(agent, "tool", f"Policy replay: re-computed the insurer settlement under the policy terms -> {len(cov)} mismatch(es)")
    for note in notes:
        trace.emit(agent, "warn", note)
    findings += cov

    # Optional clinical consistency pass
    if svc.llm.available:
        try:
            concerns = svc.llm.review_lines(bill)
            trace.emit(agent, "tool", f"{svc.llm.name}: clinical consistency review -> {len(concerns)} question(s)")
            for c in concerns[:3]:
                li = by_line.get(int(c.get("line_no", -1)))
                if li is None or any(li.line_no in f.bill_lines for f in findings):
                    continue
                findings.append(Finding(
                    finding_id=next_id(), type=FindingType.CLINICAL_REVIEW, title=f"Ask why {li.description} was needed",
                    bill_lines=[li.line_no], bill_evidence=f"Line {li.line_no}: {li.description} = {inr(li.amount)}",
                    policy=cite("reasonable_customary"), explanation=str(c.get("concern", "")).strip(),
                    confidence="Worth reviewing", impact=li.amount, counterparty="hospital", bill_correction=li.amount,
                    correction_line=li.line_no, detected_by=agent, method=f"{svc.llm.name} review",
                ))
        except Exception as exc:  # noqa: BLE001
            trace.emit(agent, "warn", f"Gemini clinical review skipped ({type(exc).__name__})")

    high = sum(1 for f in findings if f.confidence == "High")
    trace.emit(agent, "result", f"{len(findings)} finding(s): {high} high-confidence, {len(findings) - high} worth reviewing")
    return {"audit_findings": [f.model_dump(mode="json") for f in findings], "audit_notes": notes}


# ---------------------------------------------------------------------------------------
# 4. Benchmark
# ---------------------------------------------------------------------------------------


def benchmark_step(state: dict[str, Any], trace: Trace, svc: Services) -> dict[str, Any]:
    agent = "benchmark_agent"
    bill, cite = _bill(state), _cite_fn(state)
    case_id = state.get('analysis_case_id', state['case_id'])
    next_id = _ids("B")
    findings: list[Finding] = []
    trace.emit(agent, "start", "Comparing charges against reference pricing and typical coding")
    known = svc.store.categories()
    covered = sum(1 for li in bill.line_items if li.code in known)
    trace.emit(agent, "tool", f"{svc.store.name}: {covered} of {len(bill.line_items)} lines have a reference price")
    benchmark_metadata = svc.store.benchmark_metadata
    region = bill.region or benchmark_metadata.get("default_region", "")
    dataset_version = benchmark_metadata.get("version", "legacy-unversioned")
    region_basis = "hospital region extracted from bill" if bill.region else "dataset default region; hospital region unavailable"
    comparison_rows = svc.store.query("benchmark_comparisons", case_id=case_id, region=region,
                                      dataset_version=dataset_version)
    comparisons = []
    for row in comparison_rows:
        comparable = row.get("p90_unit_price") is not None
        comparisons.append({
            "line_no": row["line_no"], "code": row.get("code") or None,
            "description": row["description"], "procedure": row.get("procedure") or None,
            "region": row.get("benchmark_region") or region, "region_basis": region_basis,
            "unit": row["unit"], "quantity": row["qty"], "billed_unit_rate": row["billed_unit_rate"],
            "comparison_range": ({"p50": row["p50_unit_price"], "p90": row["p90_unit_price"]} if comparable else None),
            "price_source": row.get("price_source"), "price_type": row.get("price_type"),
            "provider_id": row.get("provider_id"),
            "reference_date": row.get("reference_date"), "dataset_version": row.get("dataset_version"),
            "dataset_label": benchmark_metadata.get("description", "Reference benchmark"),
            "comparable": comparable,
            "status": "comparable" if comparable else "missing_comparable_data",
            "missing_reason": (None if comparable else
                               "Comparable code and unit exist in another region, but no reference is available for the selected region." if row.get("other_region_matches") else
                               "The code exists in this region under a different unit." if row.get("other_unit_matches") else
                               "No comparable procedure code was found in this region and unit."),
            "confidence_label": "Worth reviewing" if comparable and row["billed_unit_rate"] > row["p90_unit_price"] * float(svc.settings.outlier_ratio) else None,
        })

    flagged: set[int] = set()
    rows = svc.store.query("upcoding", case_id=case_id, procedure=bill.procedure)
    trace.emit(agent, "tool", f"{svc.store.name}: upcoding.sql (procedure '{bill.procedure}') -> {len(rows)} line(s) above the typical tier")
    for r in rows:
        over = round((r["unit_price"] - r["expected_p90"]) * r["qty"], 2)
        if over <= 0:
            continue
        flagged.add(r["line_no"])
        findings.append(Finding(
            finding_id=next_id(), type=FindingType.UPCODING_SIGNAL, title=f"{r['description']} looks higher-grade than usual",
            bill_lines=[r["line_no"]],
            bill_evidence=f"Line {r['line_no']}: {r['code']} {r['description']} = {inr(r['amount'])} (tier {r['tier']}; typical for this procedure: {r['expected_code']}, up to tier {r['max_expected_tier']}, 90th percentile {inr(r['expected_p90'])})",
            policy=cite("reasonable_customary"),
            explanation=(f"{r['rationale']}. This bill uses {r['description']}. That can be justified by complications, "
                         f"so ask the hospital for the anaesthesia/procedure record. If it was a standard case, the typical charge is up to {inr(r['expected_p90'])}."),
            confidence="Worth reviewing", impact=over, counterparty="hospital", bill_correction=over, correction_line=r["line_no"],
            detected_by=agent, method="BigQuery SQL (upcoding.sql) vs procedure_benchmarks",
        ))

    rows = svc.store.query("benchmark_outliers", case_id=case_id, region=region,
                           dataset_version=dataset_version, outlier_ratio=float(svc.settings.outlier_ratio))
    trace.emit(agent, "tool", f"{svc.store.name}: benchmark_outliers.sql (>{svc.settings.outlier_ratio:g}x p90) -> {len(rows)} line(s)")
    for r in rows:
        if r["line_no"] in flagged:
            continue
        over = round((r["unit_price"] - r["p90_unit_price"]) * r["qty"], 2)
        findings.append(Finding(
            finding_id=next_id(), type=FindingType.PRICE_OUTLIER, title=f"{r['description']} priced {r['ratio']:.1f}x the area benchmark",
            bill_lines=[r["line_no"]],
            bill_evidence=f"Line {r['line_no']}: {r['description']} @ {inr(r['unit_price'])}; reference median {inr(r['p50_unit_price'])}, 90th percentile {inr(r['p90_unit_price'])} ({r['source']})",
            policy=cite("reasonable_customary"),
            explanation=(f"This charge is {r['ratio']:.1f} times the 90th-percentile price for the same service in comparable hospitals. "
                         f"Policies pay 'reasonable and customary' charges, so the insurer may also question it. Ask the hospital to justify the rate."),
            confidence="Worth reviewing", impact=over, counterparty="hospital", bill_correction=over, correction_line=r["line_no"],
            detected_by=agent, method="BigQuery SQL (benchmark_outliers.sql)",
            benchmark={"region": r.get("region") or region, "unit": r.get("unit"),
                       "billed_unit_rate": r["unit_price"], "comparison_range": {"p50": r["p50_unit_price"], "p90": r["p90_unit_price"]},
                       "price_source": r.get("price_source") or r.get("source"), "price_type": r.get("price_type", "informational"),
                       "provider_id": r.get("provider_id"),
                       "reference_date": str(r.get("reference_date") or benchmark_metadata.get("effective_date", "unknown")),
                       "dataset_version": r.get("dataset_version") or dataset_version,
                       "dataset_label": benchmark_metadata.get("description", "Reference benchmark"),
                       "confidence_label": "Worth reviewing"},
        ))
    trace.emit(agent, "result", f"{len(findings)} pricing signal(s), all marked 'worth reviewing'")
    return {"benchmark_findings": [f.model_dump(mode="json") for f in findings],
            "benchmark_comparisons": comparisons}


# ---------------------------------------------------------------------------------------
# 5. Reconciliation
# ---------------------------------------------------------------------------------------


def _corrections(findings: list[Finding]) -> tuple[dict[int, float], float]:
    per_line: dict[int, float] = defaultdict(float)
    total = 0.0
    for f in findings:
        if f.counterparty != "hospital" or f.bill_correction <= 0:
            continue
        total += f.bill_correction
        if f.correction_line is not None:
            per_line[f.correction_line] += f.bill_correction
    return dict(per_line), round(total, 2)


def reconciliation_step(state: dict[str, Any], trace: Trace, svc: Services) -> dict[str, Any]:
    agent = "reconciliation_agent"
    bill, terms = _bill(state), _terms(state)
    trace.emit(agent, "start", "Reconciling what was billed, what the policy covers and what you should owe")
    raw = [Finding.model_validate(f) for f in state.get("audit_findings", []) + state.get("benchmark_findings", [])]
    raw.sort(key=lambda f: (f.confidence != "High", -f.impact))
    policy = _policy(state)
    findings = [f.model_copy(update={"finding_id": f"F{i}"}) for i, f in enumerate(raw, start=1)]
    findings = [attach_finding_evidence(f, bill, policy, state.get("bill_key"), state.get("policy_key")) for f in findings]

    high = [f for f in findings if f.confidence == "High"]
    line_hi, gross_hi = _corrections(high)
    line_all, gross_all = _corrections(findings)

    as_billed = entitlement(bill, terms)
    fair_hi = entitlement(bill, terms, line_hi, gross_hi)
    fair_all = entitlement(bill, terms, line_all, gross_all)
    paid_by_patient = bill.patient_payable or round(bill.stated_gross_total - bill.insurer_approved, 2)
    trace.emit(agent, "tool", f"Entitlement as billed: insurer should pay {inr(as_billed.insurer_payable)} (paid {inr(bill.insurer_approved)})")
    trace.emit(agent, "tool", f"After high-confidence bill corrections of {inr(gross_hi)}: fair bill {inr(fair_hi.gross)}, insurer share {inr(fair_hi.insurer_payable)}")

    notes = list(state.get("audit_notes", []))
    likely = round(max(paid_by_patient - fair_hi.patient_payable, 0.0), 2)
    upper = round(max(paid_by_patient - fair_all.patient_payable, likely), 2)
    preserved = round(as_billed.insurer_payable - fair_hi.insurer_payable, 2)
    if preserved > 0.5:
        notes.append(f"Correcting the hospital overcharges also lowers the insurer's share by {inr(preserved)}, which stays available in your sum insured.")
    if as_billed.insurer_payable + 0.5 < bill.insurer_approved:
        notes.append("The insurer paid more than this policy reading suggests on some component; ClaimShield does not dispute amounts in your favour.")

    rec = Reconciliation(
        billed_gross=bill.stated_gross_total, insurer_paid=bill.insurer_approved, patient_paid=paid_by_patient,
        hospital_overcharge_high=gross_hi, hospital_overcharge_all=gross_all,
        insurer_shortfall=round(sum(f.impact for f in high if f.counterparty == "insurer"), 2),
        fair_bill_high=fair_hi.gross, fair_insurer_payable_high=fair_hi.insurer_payable,
        fair_patient_payable_high=fair_hi.patient_payable, fair_patient_payable_all=fair_all.patient_payable,
        recovery_likely=likely, recovery_upper=upper, entitlement_as_billed=as_billed, entitlement_fair=fair_hi, notes=notes,
    )
    try:
        svc.store.save_findings(state.get('analysis_case_id', state['case_id']), findings)
        trace.emit(agent, "tool", f"{svc.store.name}: wrote {len(findings)} finding(s) to the findings table")
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort; the case record still has them
        trace.emit(agent, "warn", f"Could not persist findings ({type(exc).__name__})")
    trace.emit(
        agent, "result",
        f"You paid {inr(paid_by_patient)}; under the policy you should owe {inr(fair_hi.patient_payable)}. "
        f"Likely recovery {inr(likely)}" + (f", up to {inr(upper)} if the 'worth reviewing' items hold" if upper > likely + 0.5 else ""),
    )
    healthcare = audit_from_state(state, svc)
    trace.emit(agent, 'info', 'Healthcare source checks completed; benchmark variances remain separate from settlement recovery',
               finding_counts=healthcare['finding_counts'])
    return {"findings": [f.model_dump(mode="json") for f in findings], "reconciliation": rec.model_dump(mode="json"),
            'healthcare_audit': healthcare}


# ---------------------------------------------------------------------------------------
# 6. Dispute
# ---------------------------------------------------------------------------------------


def _when(bill: Bill) -> str:
    def fmt(d: str) -> str:
        try:
            return date.fromisoformat(d).strftime("%d %b %Y")
        except ValueError:
            return d
    if bill.admission_date and bill.admission_date == bill.discharge_date:
        return f"on {fmt(bill.admission_date)}"
    return f"from {fmt(bill.admission_date)} to {fmt(bill.discharge_date)}"


def _lines(f: Finding) -> str:
    if not f.bill_lines:
        return ""
    return f" (bill line{'s' if len(f.bill_lines) > 1 else ''} {', '.join(map(str, f.bill_lines))})"


def _short(text: str, limit: int = 220) -> str:
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "..."


def insurer_letter(bill: Bill, policy: PolicyDoc, rec: Reconciliation, items: list[Finding], grievance: PolicyCitation | None) -> Letter:
    high = [f for f in items if f.confidence == "High"]
    review = [f for f in items if f.confidence != "High"]
    lines = [
        "Dear Sir/Madam,",
        "",
        f"I am writing about the cashless settlement of my claim for treatment at {bill.hospital} {_when(bill)} "
        f"({bill.procedure}), bill no. {bill.bill_number}. "
        f"Against a final bill of {inr(rec.billed_gross)}, {inr(rec.insurer_paid)} was approved and I was asked to pay {inr(rec.patient_paid)}.",
        "",
        "I have compared the settlement with my policy wording, and the following deductions do not appear to be in line with the policy:",
        "",
    ]
    for i, f in enumerate(high, start=1):
        ref = f" (Policy clause {f.policy.clause_id}, {f.policy.title}: \"{_short(f.policy.quote)}\")" if f.policy else ""
        lines += [f"{i}. {f.title}{_lines(f)}. {f.explanation}{ref} Amount: {inr(f.impact)}.", ""]
    if review:
        lines += ["I would also be grateful for an explanation of the following:", ""]
        for f in review:
            lines += [f"- {f.title}: {f.explanation}", ""]
    if high:
        lines += [f"I request that you re-assess the claim under the policy terms and release the additional {inr(rec.insurer_shortfall)}, "
                  "or explain in writing the policy basis for each deduction above.", ""]
    if grievance:
        lines += [f"This letter is a grievance under clause {grievance.clause_id} ({grievance.title}) of my policy. "
                  "If it isn't resolved, I will escalate it as that clause provides.", ""]
    lines += [
        "Enclosed: final itemised hospital bill and settlement, policy schedule and wording, ClaimShield reconciliation report.",
        "",
        "Yours faithfully,",
        bill.patient_name or "[Policyholder name]",
        f"Policy no. {bill.policy_number or policy.policy_number}",
        "[Phone] | [Email]",
    ]
    return Letter(
        recipient="insurer",
        to=f"The Grievance Redressal Officer, {policy.insurer or bill.insurer_tpa}",
        subject=f"Request to re-assess cashless claim settlement - Policy {bill.policy_number or policy.policy_number}, {bill.patient_name}, bill {bill.bill_number}",
        body="\n".join(lines), finding_ids=[f.finding_id for f in items],
    )


def hospital_letter(bill: Bill, rec: Reconciliation, items: list[Finding]) -> Letter:
    high = [f for f in items if f.confidence == "High"]
    review = [f for f in items if f.confidence != "High"]
    lines = [
        "Dear Billing Team,",
        "",
        f"I am writing about my final bill no. {bill.bill_number} for treatment {_when(bill)}. On going through the "
        f"itemised bill, I found entries that I believe need correcting.",
        "",
    ]
    if high:
        lines += ["Please correct the following:", ""]
        for i, f in enumerate(high, start=1):
            lines += [f"{i}. {f.title}{_lines(f)}. {f.explanation} Amount: {inr(f.impact)}.", ""]
    if review:
        lines += ["Please also share the supporting records or justification for:", ""]
        for f in review:
            lines += [f"- {f.title}{_lines(f)}. {f.explanation} Amount in question: {inr(f.impact)}.", ""]
    lines += [
        "Please issue a revised final bill, send it to my insurer's TPA so the settlement can be revised, and refund any "
        "excess I have paid.",
        "",
        "Yours faithfully,",
        bill.patient_name or "[Patient name]",
        "[Phone] | [Email]",
    ]
    return Letter(
        recipient="hospital", to=f"The Billing Department, {bill.hospital}" + (f", {bill.hospital_address}" if bill.hospital_address else ""),
        subject=f"Request for a corrected final bill - bill no. {bill.bill_number}, patient {bill.patient_name}",
        body="\n".join(lines), finding_ids=[f.finding_id for f in items],
    )


def dispute_step(state: dict[str, Any], trace: Trace, svc: Services) -> dict[str, Any]:
    agent = "dispute_agent"
    from ..reports.pdf import letter_pdf, report_pdf

    bill, policy = _bill(state), _policy(state)
    findings = [Finding.model_validate(f) for f in state.get("findings", [])]
    rec = Reconciliation.model_validate(state["reconciliation"])
    trace.emit(agent, "start", "Writing explanations, the dispute letter(s) and the reconciliation report")

    if svc.llm.available and findings:
        for f in findings:
            try:
                f.explanation = svc.llm.explain(f, bill)
            except Exception as exc:  # noqa: BLE001
                trace.emit(agent, "warn", f"Gemini explanation for {f.finding_id} failed ({type(exc).__name__}); keeping the template")
        trace.emit(agent, "tool", f"{svc.llm.name}: rewrote {len(findings)} explanation(s) in plain language")

    letters: list[Letter] = []
    to_insurer = [f for f in findings if f.counterparty == "insurer"]
    to_hospital = [f for f in findings if f.counterparty == "hospital"]
    if to_insurer:
        letters.append(insurer_letter(bill, policy, rec, to_insurer, _cite_fn(state)("grievance")))
    if to_hospital:
        letters.append(hospital_letter(bill, rec, to_hospital))
    for letter in letters:
        facts = {"bill": {k: getattr(bill, k) for k in ("hospital", "bill_number", "patient_name", "policy_number")},
                 "findings": [f.model_dump(mode="json", include={"finding_id", "title", "impact", "confidence"}) for f in findings
                              if f.finding_id in letter.finding_ids],
                 "reconciliation": rec.model_dump(mode="json", include={"billed_gross", "insurer_paid", "patient_paid", "insurer_shortfall"})}
        try:
            letter.body = svc.llm.draft_letter(facts, letter.body)
        except Exception as exc:  # noqa: BLE001
            trace.emit(agent, "warn", f"Gemini letter drafting failed ({type(exc).__name__}); using the template letter")
        trace.emit(agent, "tool", f"{svc.llm.name}: drafted letter to the {letter.recipient} ({len(letter.finding_ids)} item(s))")

    case_id = state["case_id"]
    artifacts: dict[str, str] = {}
    artifacts["report"] = svc.storage.put(f"cases/{case_id}/reconciliation_report.pdf",
                                          report_pdf(case_id, bill, policy, findings, rec, svc.describe(), _terms(state)), "application/pdf")
    for letter in letters:
        artifacts[f"letter_{letter.recipient}"] = svc.storage.put(
            f"cases/{case_id}/dispute_letter_{letter.recipient}.pdf", letter_pdf(letter, bill), "application/pdf")
    trace.emit(agent, "tool", f"{svc.storage.name}: saved {len(artifacts)} document(s)")

    if letters:
        trace.emit(agent, "result", f"Ready to send: {', '.join('letter to the ' + l.recipient for l in letters)}; reconciliation report generated")
    else:
        trace.emit(agent, "result", "Nothing worth disputing: the bill adds up and the settlement matches the policy. Report generated.")
    return {
        "findings": [f.model_dump(mode="json") for f in findings],
        "letters": [l.model_dump(mode="json") for l in letters],
        "artifacts": artifacts,
        "completed_on": date.today().isoformat(),
    }


STEPS: dict[str, Step] = {
    "intake_agent": intake_step,
    "policy_agent": policy_step,
    "audit_agent": audit_step,
    "benchmark_agent": benchmark_step,
    "reconciliation_agent": reconciliation_step,
    "dispute_agent": dispute_step,
}
