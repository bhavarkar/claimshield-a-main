"""Deterministic healthcare reference checks over the existing normalized models.

Informational benchmark differences never enter the insurer-payment/recovery calculation.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import Bill, Finding, FindingType, PolicyCitation, PolicyDoc, PolicyTerms
from ..money import inr
from ..services.healthcare_references import is_active
from ..services.regulatory import ApplicabilityRequest
from .medical_matching import match_description, normalize_description


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


class HealthcareAuditInput(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    claim_id: str = Field(min_length=1, max_length=100)
    bill: Bill
    policy_terms: PolicyTerms = Field(default_factory=PolicyTerms)
    policy_doc: PolicyDoc = Field(default_factory=PolicyDoc)
    context: ApplicabilityRequest = Field(default_factory=ApplicabilityRequest)

    @model_validator(mode='after')
    def normalized_values(self):
        lines = self.bill.line_items
        if len(lines) > 2000 or len({line.line_no for line in lines}) != len(lines):
            raise ValueError('Use at most 2000 bill lines with unique line_no values')
        values = [self.bill.stated_gross_total, self.bill.admission_days, self.bill.actual_room_rent_per_day,
                  self.policy_terms.sum_insured, self.policy_terms.room_rent_limit,
                  self.policy_terms.icu_rent_limit, self.policy_terms.room_rent_limit_pct]
        for line in lines:
            values.extend([line.qty, line.unit_price, line.amount])
            if line.qty <= 0:
                raise ValueError('Bill quantities must be positive; credits require separate normalization')
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in values):
            raise ValueError('Audit amounts and quantities must be finite and non-negative')
        if self.policy_terms.room_rent_limit_pct is not None and self.policy_terms.room_rent_limit_pct > 100:
            raise ValueError('Room-rent percentage must be between 0 and 100')
        return self


class HealthcareFinding(Finding):
    claim_id: str
    bill_item_id: str | None = None
    severity: Literal['info', 'warning', 'high']
    status: Literal['clear', 'flagged', 'needs_review']
    billed_amount: float | None = None
    reference_amount: float | None = None
    potential_difference: float | None = None
    rule_id: str | None = None
    rule_name: str
    confidence_score: float = Field(ge=0, le=1)
    match_status: Literal['confirmed_match', 'possible_match', 'no_match', 'not_applicable'] = 'not_applicable'
    comparison: str | None = None


def _day(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def policy_citation(data, topic):
    ref = data.policy_terms.clause_refs.get(topic)
    clauses = [c for c in data.policy_doc.clauses if c.clause_id == ref and c.source
               and c.source == data.policy_doc.source and c.text.strip() and 'page' in c.model_fields_set
               and (not data.policy_doc.document_id or c.document_id == data.policy_doc.document_id)
               and (not data.policy_doc.version_id or c.version_id == data.policy_doc.version_id)]
    if len(clauses) != 1:
        return None
    clause = clauses[0]
    return PolicyCitation(clause_id=clause.clause_id, title=clause.title, quote=clause.text,
                          source=clause.source, page=clause.page, document_id=clause.document_id,
                          version_id=clause.version_id)


def _room_rule_matches(rule, data):
    policy = data.policy_doc
    citation = rule.policy_citation
    return (policy.document_id == rule.policy_document_id and policy.version_id == rule.policy_version_id
            and citation.document_id == policy.document_id and citation.version_id == policy.version_id
            and any(c.clause_id == citation.clause_id and c.text == citation.quote and c.page == citation.page
                    and c.source == citation.source and c.title == citation.title
                    and c.document_id == policy.document_id and c.version_id == policy.version_id for c in policy.clauses))


def audit_healthcare(data: HealthcareAuditInput, *, rates=(), room_rules=(), regulatory_rules=()):
    findings = []

    def add(kind, line, status, explanation, *, source=None, difference=None, reference=None,
            score=0.0, rule_id=None, rule_name=None, match='not_applicable', comparison=None,
            evidence=None, citation=None):
        seed = f'{data.claim_id}:{kind.value}:{line.line_no if line else "claim"}:{rule_id}:{len(findings)}'
        detail = dict(evidence or {})
        if citation and not source:
            source = {'source_name': citation.source, 'source_type': 'POLICY', 'document_id': citation.document_id,
                      'page_number': citation.page, 'version': citation.version_id, 'effective_date': None,
                      'imported_at': None, 'verification_status': 'provided_policy_evidence',
                      'notes': 'Calculation uses the supplied normalized policy limit and its cited wording.',
                      'source_quote': citation.quote}
        if source:
            detail['source'] = source
        detail['bill'] = {'line_no': line.line_no, 'page_number': line.source_page,
                          'source_text': line.source_text or None} if line else None
        findings.append(HealthcareFinding(
            finding_id='H-' + hashlib.sha256(seed.encode()).hexdigest()[:16], claim_id=data.claim_id,
            bill_item_id=str(line.line_no) if line else None, type=kind,
            title=rule_name or kind.value.replace('_', ' ').title(), rule_name=rule_name or kind.value,
            bill_lines=[line.line_no] if line else [],
            bill_evidence=f'Line {line.line_no}: {line.description}; billed {inr(line.amount)}' if line else 'Claim-level room-rent context',
            explanation=explanation, severity='warning' if status != 'clear' else 'info', status=status,
            confidence='High' if score >= .95 and status != 'needs_review' else 'Worth reviewing',
            confidence_score=score, impact=max(difference or 0, 0), billed_amount=line.amount if line else None,
            reference_amount=reference, potential_difference=difference, rule_id=rule_id,
            counterparty='hospital', detected_by='healthcare_reference_audit', method='deterministic source-backed check',
            evidence=detail, policy=citation, match_status=match, comparison=comparison))

    if not data.bill.line_items:
        add(FindingType.NON_PAYABLE_ITEM, None, 'needs_review', 'No normalized bill items were supplied; no item-level audit was performed.')

    # IRDAI matches use only the registry's currently approved, applicable source bindings.
    active = [(r, doc) for r, doc, status, _ in regulatory_rules if status == 'Applicable']
    for line in data.bill.line_items:
        matches = [(r, doc, match_description(line.description, r.item_rule.canonical_description, r.item_rule.aliases))
                   for r, doc in active if not r.item_rule.categories or line.category in r.item_rule.categories]
        confirmed = [(r, doc, m) for r, doc, m in matches if m.kind in {'exact', 'alias'}]
        candidates = confirmed or [(r, doc, m) for r, doc, m in matches if m.kind == 'possible']
        if not candidates:
            add(FindingType.NON_PAYABLE_ITEM, line, 'clear' if active else 'needs_review',
                'No item match was found among the applicable reviewed rules; this does not establish coverage.' if active else
                'No active, applicable, verified non-payable item rule is available. No exclusion was inferred.',
                match='no_match', comparison='no_match' if active else 'reference_unavailable')
            continue
        if len(candidates) > 1:
            add(FindingType.NON_PAYABLE_ITEM, line, 'needs_review', 'Multiple item rules match. Resolve the applicable rule before estimating any exclusion.',
                match='possible_match', evidence={'candidate_rule_ids': [r.rule_id for r, _, _ in candidates]})
            continue
        rule, doc, matched = candidates[0]
        exact = matched.kind in {'exact', 'alias'}
        deterministic = exact and rule.item_rule.effect == 'non_payable'
        add(FindingType.NON_PAYABLE_ITEM, line, 'flagged' if deterministic else 'needs_review',
            'This item matches an applicable reviewed non-payable reference. Confirm its treatment against the complete policy and claim evidence.' if deterministic else
            'A possible or policy-dependent non-payable item match requires policy verification; no deduction was calculated.',
            difference=money(line.amount) if deterministic else None, reference=0 if deterministic else None,
            score=matched.confidence, rule_id=rule.rule_id, rule_name=rule.rule_title,
            match='confirmed_match' if exact else 'possible_match',
            source={'source_name': doc.title, 'source_type': 'IRDAI', 'document_id': doc.document_id,
                    'source_url': doc.official_source_url, 'page_number': rule.source_page,
                    'effective_date': rule.effective_date.isoformat() if rule.effective_date else None,
                    'version': doc.sha256, 'verification_status': rule.human_review_status.value,
                    'imported_at': doc.retrieval_date.isoformat() if doc.retrieval_date else None,
                    'notes': rule.reviewer_explanation, 'source_quote': rule.source_quote},
            evidence={'match_kind': matched.kind})

    # Daily room cap and proportionate adjustment are separate checks.
    terms, bill = data.policy_terms, data.bill
    citation = policy_citation(data, 'room_rent')
    limit = terms.room_rent_limit
    limit_type = terms.room_rent_limit_type or 'per_day'  # Existing parser's numeric field is per day.
    if limit_type == 'percent_sum_insured_per_day':
        limit = (money(Decimal(str(terms.sum_insured)) * Decimal(str(terms.room_rent_limit_pct)) / 100)
                 if terms.sum_insured is not None and terms.room_rent_limit_pct is not None else None)
    elif limit_type != 'per_day':
        limit = None
    room_lines = [line for line in bill.line_items if line.category in {'ROOM', 'ICU'}]
    policy_identity_conflict = bool(bill.policy_number and data.policy_doc.policy_number
                                    and bill.policy_number != data.policy_doc.policy_number)
    room_known = citation is not None and 'room_rent' not in terms.uncertain_topics and not policy_identity_conflict
    for line in room_lines:
        daily_limit = terms.icu_rent_limit if line.category == 'ICU' else limit
        if not room_known or daily_limit is None or normalize_description(line.unit) != 'per day':
            add(FindingType.ROOM_RENT_LIMIT, line, 'needs_review', 'The daily room limit, unit or supporting policy wording is missing or ambiguous. No room deduction was inferred.')
            continue
        if abs(line.amount - line.qty * line.unit_price) > .01:
            add(FindingType.ROOM_RENT_LIMIT, line, 'needs_review', 'Quantity, daily rate and billed room amount do not reconcile. Resolve the bill arithmetic first.')
            continue
        allowed = money(min(Decimal(str(line.amount)), Decimal(str(line.qty)) * Decimal(str(daily_limit))))
        difference = money(Decimal(str(line.amount)) - Decimal(str(allowed)))
        add(FindingType.ROOM_RENT_LIMIT, line, 'flagged' if difference > 0 else 'clear',
            f'Room rent billed at {inr(line.unit_price)}/day; provided policy limit {inr(daily_limit)}/day. '
            f'For {line.qty:g} billed days, direct excess is {inr(difference)}. This does not activate proportionate adjustments to other charges.',
            difference=difference, reference=allowed, score=.98, citation=citation,
            evidence={'daily_limit': daily_limit, 'actual_daily_rate': line.unit_price, 'days': line.qty,
                      'calculation': 'billed_amount - min(billed_amount, days * daily_limit)', 'limit_type': limit_type})
    normal_rooms = [line for line in room_lines if line.category == 'ROOM']
    room_rates = {line.unit_price for line in normal_rooms}
    actual = bill.actual_room_rent_per_day if bill.actual_room_rent_per_day is not None else next(iter(room_rates), None)
    room_consistent = (len(room_rates) <= 1 and (not room_rates or actual in room_rates)
                       and all(normalize_description(line.unit) == 'per day'
                               and abs(line.amount - line.qty * line.unit_price) <= .01 for line in normal_rooms))
    if bill.admission_days is not None and normal_rooms and abs(sum(line.qty for line in normal_rooms) - bill.admission_days) > .01:
        room_consistent = False
    if normal_rooms or actual is not None:
        if not room_known or limit is None or actual is None or actual <= 0 or not room_consistent:
            add(FindingType.ROOM_RENT_PROPORTIONATE, None, 'needs_review', 'Room-rent or policy evidence is incomplete, mixed or conflicting. No proportionate deduction was applied.')
        elif actual <= limit:
            add(FindingType.ROOM_RENT_PROPORTIONATE, None, 'clear', 'The actual daily room rate does not exceed the provided limit; no proportionate adjustment was triggered.', score=.98)
        else:
            applicable = [r for r in room_rules if is_active(r, data.context.event_date) and _room_rule_matches(r, data)]
            if len(applicable) != 1:
                add(FindingType.ROOM_RENT_PROPORTIONATE, None, 'needs_review',
                    'Room rent exceeds the provided limit, but a unique verified policy-specific proportionate rule is unavailable. Other bill items were not adjusted.',
                    evidence={'daily_limit': limit, 'actual_daily_rate': actual, 'ratio': None})
            else:
                rule = applicable[0]
                ratio = Decimal(str(limit)) / Decimal(str(actual))
                for line in bill.line_items:
                    name = normalize_description(line.description)
                    affected = line.category in rule.affected_categories or name in {normalize_description(n) for n in rule.affected_items}
                    excluded = line.category in rule.excluded_categories or name in {normalize_description(n) for n in rule.excluded_items}
                    if line.category in {'ROOM', 'ICU'} or not affected:
                        continue
                    allowed = money(Decimal(str(line.amount)) * ratio)
                    difference = money(Decimal(str(line.amount)) - Decimal(str(allowed)))
                    add(FindingType.ROOM_RENT_PROPORTIONATE, line, 'clear' if excluded else 'flagged',
                        'This item is explicitly excluded from the configured proportionate adjustment.' if excluded else
                        f'The reviewed policy rule includes {line.category}. Allowed/actual daily room rent is '
                        f'{inr(limit)}/{inr(actual)}; {inr(line.amount)} × {float(ratio):.6f} = {inr(allowed)}. '
                        f'Estimated adjustment: {inr(difference)}; confirm all policy conditions.',
                        difference=0 if excluded else difference, reference=line.amount if excluded else allowed,
                        score=.98, rule_id=rule.rule_id, rule_name=rule.rule_name,
                        source=rule.model_dump(mode='json'), citation=rule.policy_citation,
                        evidence={'ratio': float(ratio), 'calculation': 'billed_amount * (allowed_room_rate / actual_room_rate)',
                                  'excluded': excluded, 'affected_categories': rule.affected_categories,
                                  'excluded_categories': rule.excluded_categories})

    for line in bill.line_items:
        for source_type in ('CGHS', 'PMJAY', 'NPPA'):
            kind = FindingType.NPPA_REFERENCE if source_type == 'NPPA' else FindingType.HEALTHCARE_BENCHMARK
            if source_type == 'NPPA' and line.category not in {'PHARMACY', 'IMPLANT', 'CONSUMABLE', 'OTHER'}:
                continue
            event = _day(line.service_date) if line.service_date else data.context.event_date
            matches = []
            for record in rates:
                if record.source_type != source_type or record.category != line.category or not is_active(record, event):
                    continue
                matched = match_description(line.description, record.normalized_name or record.item_name, record.aliases)
                if matched.kind != 'none':
                    matches.append((record, matched))
            confirmed = [(r, m) for r, m in matches if m.kind in {'exact', 'alias'}]
            candidates = confirmed or matches
            scoped, uncertain = [], []
            for record, matched in candidates:
                checks = [(record.geography, bill.region, 'geography'),
                          (record.facility_category, bill.facility_category, 'facility category'),
                          (record.strength, line.strength, 'strength'), (record.dosage_form, line.dosage_form, 'dosage form'),
                          (record.pack_size, line.pack_size, 'pack size'), (record.manufacturer, line.manufacturer, 'manufacturer')]
                missing, different = [], []
                for expected, actual_value, label in checks:
                    if label == 'geography' and expected == 'national':
                        continue
                    if expected and not actual_value:
                        missing.append(label)
                    elif expected and normalize_description(expected) != normalize_description(actual_value):
                        different.append(label)
                if different:
                    continue
                if not record.geography:
                    missing.append('verified geographic scope')
                if normalize_description(record.unit) != normalize_description(line.unit):
                    missing.append('compatible billing unit')
                if source_type == 'NPPA' and (record.price_basis == 'unknown' or line.price_basis != record.price_basis):
                    missing.append('matching tax/price basis')
                if source_type == 'NPPA' and line.category == 'PHARMACY' and not (record.strength and record.dosage_form):
                    missing.append('verified drug strength and dosage form')
                (uncertain if missing else scoped).append((record, matched, missing))
            if len(scoped) != 1 or uncertain:
                why = ('Multiple applicable reference versions match; select the correct tariff version.' if len(scoped) > 1 else
                       'Missing or incompatible qualifiers: ' + ', '.join(sorted({field for _, _, fields in uncertain for field in fields})) + '.' if uncertain else
                       'No active verified reference matches the item, date, product and geographic scope.')
                add(kind, line, 'needs_review', why, rule_name=f'{source_type} reference check',
                    comparison='needs_manual_review' if scoped or uncertain else 'benchmark_unavailable',
                    match='possible_match' if candidates else 'no_match',
                    evidence={'source_type': source_type, 'candidate_record_ids': [r.record_id for r, _ in candidates]})
                continue
            record, matched, _ = scoped[0]
            if matched.kind == 'possible':
                add(kind, line, 'needs_review', 'The medical description is only a fuzzy candidate; no price difference was calculated.',
                    score=matched.confidence, match='possible_match', rule_id=record.record_id,
                    source=record.model_dump(mode='json'), comparison='needs_manual_review')
                continue
            if abs(line.amount - line.unit_price * line.qty) > .01:
                add(kind, line, 'needs_review', 'Billed quantity, unit rate and amount do not reconcile. Resolve arithmetic before comparing prices.',
                    rule_id=record.record_id, source=record.model_dump(mode='json'), comparison='needs_manual_review')
                continue
            reference = money(Decimal(str(record.reference_amount)) * Decimal(str(line.qty)))
            difference = money(Decimal(str(line.amount)) - Decimal(str(reference)))
            comparison = 'above_benchmark' if difference > 0 else 'below_benchmark' if difference < 0 else 'within_benchmark'
            explanation = (f'The item is {inr(abs(difference))} {"above" if difference > 0 else "below" if difference < 0 else "from"} '
                           f'the {source_type} reference for {line.qty:g} {record.unit}. ')
            explanation += ('This ceiling comparison requires confirmation of product specifications, unit and tax basis; it is not a finding of a legal violation.'
                            if source_type == 'NPPA' else 'This is a benchmark comparison only and does not determine private insurer liability.')
            add(kind, line, 'flagged' if difference > 0 else 'clear', explanation,
                difference=difference, reference=reference, score=matched.confidence, match='confirmed_match',
                rule_id=record.record_id, rule_name=f'{source_type} reference check', comparison=comparison,
                source=record.model_dump(mode='json'),
                evidence={'reference_unit_price': record.reference_amount, 'quantity': line.qty,
                          'calculation': 'billed_amount - (reference_unit_price * quantity)',
                          'informational_only': source_type != 'NPPA'})

    # Maximum per item avoids counting the same charge multiple times across overlapping rules.
    differences, benchmarks = {}, {}
    for finding in findings:
        if finding.status != 'flagged' or not finding.bill_item_id:
            continue
        target = benchmarks if finding.type == FindingType.HEALTHCARE_BENCHMARK else differences
        target[finding.bill_item_id] = max(target.get(finding.bill_item_id, 0), finding.potential_difference or 0)
    total = bill.stated_gross_total if 'stated_gross_total' in bill.model_fields_set else sum(line.amount for line in bill.line_items)
    return {'claim_id': data.claim_id, 'total_billed': money(total),
            'total_billed_basis': 'printed_total' if 'stated_gross_total' in bill.model_fields_set else 'sum_of_bill_items',
            'potential_discrepancy': money(sum((Decimal(str(v)) for v in differences.values()), Decimal(0))),
            'benchmark_variance': money(sum((Decimal(str(v)) for v in benchmarks.values()), Decimal(0))),
            'finding_counts': {status: sum(f.status == status for f in findings) for status in ('clear', 'flagged', 'needs_review')},
            'warning_count': sum(f.severity in {'warning', 'high'} for f in findings),
            'findings': [f.model_dump(mode='json') for f in findings],
            'summary_basis': 'Potential discrepancy is the largest positive supported adjustment per bill item; CGHS/PMJAY benchmark variances are separate. These are audit scenarios, not guaranteed recovery or regulatory violation determinations.'}
