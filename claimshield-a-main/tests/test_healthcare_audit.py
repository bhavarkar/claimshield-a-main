"""All prices, policies and regulatory text here are SYNTHETIC TEST FIXTURES ONLY."""
from datetime import date
from io import BytesIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from claimshield.api.main import app
from claimshield.domain.healthcare_audit import HealthcareAuditInput, audit_healthcare
from claimshield.domain.medical_matching import match_description
from claimshield.models import Bill, Clause, LineItem, PolicyDoc, PolicyTerms, PolicyCitation
from claimshield.services import get_services
from claimshield.services.healthcare_audit import run_healthcare_audit
from claimshield.services.healthcare_references import (
    HealthcareRate, HealthcareReferenceRegistry, ReferenceImport, ReferenceReview, RoomAdjustmentRule)
from claimshield.services.regulatory import (ApplicabilityRequest, DocumentReview, IRDAI_HEALTH_CIRCULARS,
    RegulatoryItemRule, RegulatoryRegistry, RegulatoryRule, ReviewStatus, RuleApplicability, RuleReview)


EVENT = date(2026, 2, 1)
POLICY_ID, VERSION = 'synthetic-policy-only', 'synthetic-version-only'


def item(n=1, description='CBC', category='LAB', price=100, qty=1, **updates):
    fields = dict(line_no=n, service_date=EVENT.isoformat(), description=description, category=category,
                  qty=qty, unit_price=price, amount=price * qty, unit='per_test')
    fields.update(updates)
    return LineItem(**fields)


def policy(limit=3000):
    clauses = [Clause(clause_id='R1', title='Synthetic room cap', text=f'Synthetic policy daily room limit: Rs. {limit}.',
                      source='synthetic-policy.pdf', page=2, document_id=POLICY_ID, version_id=VERSION),
               Clause(clause_id='R2', title='Synthetic proportional fixture',
                      text='Synthetic fixture: eligible consultation and lab charges follow allowed/actual room rate; lab is excluded.',
                      source='synthetic-policy.pdf', page=3, document_id=POLICY_ID, version_id=VERSION)]
    return PolicyDoc(source='synthetic-policy.pdf', document_id=POLICY_ID, version_id=VERSION, clauses=clauses)


def data(lines=None, limit=3000, **changes):
    lines = lines if lines is not None else [item()]
    fields = dict(claim_id='synthetic-claim', bill=Bill(line_items=lines, region='fixture-city',
                  stated_gross_total=sum(line.amount for line in lines)), policy_doc=policy(limit),
                  policy_terms=PolicyTerms(room_rent_limit=limit, clause_refs={'room_rent': 'R1'}),
                  context=ApplicabilityRequest(insurance_category='health', event_date=EVENT, claim_context='hospitalization'))
    fields.update(changes)
    return HealthcareAuditInput(**fields)


def rate(record_id='test-cghs', source_type='CGHS', **updates):
    fields = dict(record_id=record_id, source_name='SYNTHETIC TEST SOURCE ONLY', source_type=source_type, synthetic=True,
                  source_url='https://example.invalid/synthetic-test-fixture', effective_date=date(2026, 1, 1),
                  version='synthetic-test-v1', notes='Synthetic test fixture, never production data',
                  item_name='CBC', category='LAB', unit='per_test', reference_amount=100, geography='fixture-city')
    fields.update(updates)
    return HealthcareRate(**fields)


def room_rule(**updates):
    clause = policy().clauses[1]
    fields = dict(rule_id='synthetic-proportion', rule_name='Synthetic policy-only room calculation', synthetic=True,
                  source_name='SYNTHETIC POLICY TEST SOURCE', document_id=POLICY_ID,
                  effective_date=date(2026, 1, 1), version=VERSION,
                  policy_document_id=POLICY_ID, policy_version_id=VERSION,
                  policy_citation=PolicyCitation(clause_id=clause.clause_id, title=clause.title, quote=clause.text,
                    source=clause.source, page=clause.page, document_id=POLICY_ID, version_id=VERSION),
                  affected_categories=['CONSULT', 'LAB'], excluded_categories=['LAB'])
    fields.update(updates)
    return RoomAdjustmentRule(**fields)


def approve(registry, kind, record_id):
    return registry.review(kind, record_id, ReferenceReview(verification_status='approved', enforcement_enabled=True,
        currentness_checked=True, source_checked=True, interpretation_checked=True,
        explanation='SYNTHETIC TEST: reviewed the fixture only'), 'synthetic-test-admin')


def references(*rates, rules=()):
    registry = HealthcareReferenceRegistry(get_services().storage)
    registry.import_records(ReferenceImport(rates=list(rates), room_rules=list(rules)))
    for record in rates:
        approve(registry, 'rates', record.record_id)
    for record in rules:
        approve(registry, 'room_rules', record.rule_id)
    return registry


def regulatory(*, approve_rule=True, aliases=(), quote='Synthetic TEST fixture: Admission kit is a non-payable item.'):
    from reportlab.pdfgen import canvas
    stream = BytesIO()
    page = canvas.Canvas(stream)
    page.drawString(36, 720, quote)
    page.save()
    registry = RegulatoryRegistry(get_services().storage)
    doc_id = 'irdai-health-master-2024-en'
    registry.ingest(doc_id, IRDAI_HEALTH_CIRCULARS, stream.getvalue())
    registry.review_document(doc_id, DocumentReview(human_review_status='approved', currentness_checked=True,
        reviewer_explanation='SYNTHETIC TEST SOURCE ONLY'), reviewer_id='fixture-admin')
    registry.create_candidate(RegulatoryRule(rule_id='fixture-non-payable', rule_title='Synthetic item test rule',
        synthetic=True,
        source_document_id=doc_id, source_citation='Synthetic test fixture page 1', source_page=1, source_quote=quote,
        effective_date=date(2026, 1, 1), applicability=RuleApplicability(insurance_categories=['health'], claim_contexts=['hospitalization']),
        item_rule=RegulatoryItemRule(canonical_description='Admission kit', aliases=list(aliases), effect='non_payable')))
    if approve_rule:
        registry.review_rule('fixture-non-payable', RuleReview(human_review_status='approved', enforcement_enabled=True,
            reviewer_explanation='SYNTHETIC TEST ONLY: reviewed fixture interpretation'), reviewer_id='fixture-admin')
    return registry


def findings(result, finding_type):
    return [f for f in result['findings'] if f['type'] == finding_type]


def test_clean_claim_is_clear_against_available_fixture_sources():
    references(rate(), rate('test-pmjay', 'PMJAY'))
    regulatory()
    result = run_healthcare_audit(data([item(price=90)]), get_services())
    assert result['finding_counts'] == {'clear': 3, 'flagged': 0, 'needs_review': 0}
    assert result['potential_discrepancy'] == result['benchmark_variance'] == 0
    assert result['findings'][1]['comparison'] == 'below_benchmark'


def test_flagged_claim_has_money_explanations_confidence_and_provenance():
    references(rate(), rate('test-pmjay', 'PMJAY', reference_amount=120))
    regulatory()
    request = data([item(1, 'Room', 'ROOM', price=5000, qty=2, unit='per_day'),
                    item(2, price=150), item(3, 'Admission kit', 'NON_MEDICAL', price=200, unit='per_item')])
    result = run_healthcare_audit(request, get_services())
    assert result['potential_discrepancy'] == 4200  # Room 4000 + item 200; benchmarks are separate.
    assert result['benchmark_variance'] == 50  # Same CBC isn't counted twice for two schemes.
    flagged = [f for f in result['findings'] if f['status'] == 'flagged']
    assert len(flagged) == 4
    assert all(f['claim_id'] == request.claim_id and f['explanation'] and f['confidence_score'] >= .95 for f in flagged)
    assert all(f['evidence']['source']['source_name'] for f in flagged)
    assert findings(result, 'ROOM_RENT_PROPORTIONATE')[0]['status'] == 'needs_review'


def test_proportionate_rule_is_explicit_scoped_and_excludes_configured_categories():
    references(rules=[room_rule()])
    result = run_healthcare_audit(data([item(1, 'Room', 'ROOM', 6000, 2, unit='per_day'),
        item(2, 'Consultation', 'CONSULT', 1000), item(3, 'Lab fixture', 'LAB', 600)]), get_services())
    rows = [f for f in findings(result, 'ROOM_RENT_PROPORTIONATE') if f['bill_item_id']]
    assert rows[0]['potential_difference'] == 500
    assert rows[0]['reference_amount'] == 500
    assert rows[0]['evidence']['ratio'] == .5
    assert rows[1]['status'] == 'clear' and rows[1]['potential_difference'] == 0
    assert result['potential_discrepancy'] == 6500


@pytest.mark.parametrize('change', ['wrong_version', 'pending', 'missing_clause', 'ambiguous_limit', 'mixed_rooms'])
def test_room_proportion_never_assumed_for_incomplete_or_unverified_policy(change):
    rule = room_rule(policy_version_id='wrong' if change == 'wrong_version' else VERSION)
    registry = references(rules=[rule])
    if change == 'pending':
        registry.review('room_rules', rule.rule_id, ReferenceReview(verification_status='needs_update', explanation='Needs a fixture recheck'), 'admin')
    request = data([item(1, 'Room', 'ROOM', 6000, 2, unit='per_day'), item(2, 'Consultation', 'CONSULT', 1000)])
    if change == 'missing_clause':
        request.policy_doc.clauses = request.policy_doc.clauses[:1]
    elif change == 'ambiguous_limit':
        request.policy_terms.uncertain_topics = ['room_rent']
    elif change == 'mixed_rooms':
        request.bill.line_items.append(item(3, 'Other room', 'ROOM', 5000, 1, unit='per_day'))
    result = run_healthcare_audit(request, get_services())
    rows = findings(result, 'ROOM_RENT_PROPORTIONATE')
    assert rows and all(f['status'] == 'needs_review' and f['potential_difference'] is None for f in rows)


def test_percent_sum_insured_room_limit_and_financial_precision():
    request = data([item(1, 'Room', 'ROOM', 3000, 3, unit='per_day')])
    request.policy_terms.room_rent_limit_type = 'percent_sum_insured_per_day'
    request.policy_terms.room_rent_limit_pct = 2
    request.policy_terms.sum_insured = 100000
    result = audit_healthcare(request)
    assert findings(result, 'ROOM_RENT_LIMIT')[0]['potential_difference'] == 3000


def test_ambiguous_description_and_missing_source_do_not_produce_deductions():
    references(rate())
    result = run_healthcare_audit(data([item(description='CBC extended panel')]), get_services())
    assert result['potential_discrepancy'] == result['benchmark_variance'] == 0
    assert all(f['status'] == 'needs_review' for f in result['findings'])
    assert match_description('Hemogram', 'CBC').kind == 'none'
    assert match_description('C B C', 'CBC').kind != 'exact'
    assert match_description('Hemogram', 'CBC', ['Hemogram']).kind == 'alias'
    assert match_description('Drug 5mg/10ml', 'Drug 5mg 10ml').kind != 'exact'


def test_pending_regulatory_record_is_not_used_as_active_rule():
    regulatory(approve_rule=False)
    result = run_healthcare_audit(data([item(description='Admission kit', category='NON_MEDICAL')]), get_services())
    exclusion = findings(result, 'NON_PAYABLE_ITEM')[0]
    assert exclusion['status'] == 'needs_review' and exclusion['potential_difference'] is None
    assert result['potential_discrepancy'] == 0


def test_regulatory_alias_must_be_in_reviewed_source_and_revoked_source_is_inactive():
    registry = regulatory(approve_rule=False, aliases=['Hemogram'])
    with pytest.raises(HTTPException) as error:
        registry.review_rule('fixture-non-payable', RuleReview(human_review_status='approved', enforcement_enabled=True,
            reviewer_explanation='This unsupported alias must be rejected'), reviewer_id='admin')
    assert error.value.status_code == 409
    registry.review_document('irdai-health-master-2024-en', DocumentReview(human_review_status='needs_update',
        reviewer_explanation='Source no longer verified'), reviewer_id='admin')
    rows = registry.item_rules(data().context)
    assert rows[0][2] == 'Undetermined'


def test_reference_import_cannot_self_activate_and_dates_units_scope_are_checked():
    registry = HealthcareReferenceRegistry(get_services().storage)
    record = rate(verification_status=ReviewStatus.APPROVED, enforcement_enabled=True)
    registry.import_records(ReferenceImport(rates=[record]))
    saved = registry.snapshot()[0][0]
    assert saved.verification_status == ReviewStatus.PENDING and saved.enforcement_enabled is False
    assert saved.imported_at is not None and saved.reviewed_by is None
    with pytest.raises(HTTPException):
        registry.import_records(ReferenceImport(rates=[record]))
    approve(registry, 'rates', record.record_id)
    for mutate in ['unit', 'date', 'region', 'facility']:
        request = data([item(price=200)])
        if mutate == 'unit': request.bill.line_items[0].unit = 'per_package'
        if mutate == 'date': request.bill.line_items[0].service_date = '2025-01-01'
        if mutate == 'region': request.bill.region = 'different-city'
        if mutate == 'facility':
            extra = rate('facility', facility_category='accredited', reference_amount=90)
            registry.import_records(ReferenceImport(rates=[extra]))
            approve(registry, 'rates', extra.record_id)
        rows = findings(run_healthcare_audit(request, get_services()), 'HEALTHCARE_BENCHMARK')
        if mutate != 'facility':
            assert rows[0]['status'] == 'needs_review' and rows[0]['potential_difference'] is None
        else:
            assert rows[0]['status'] == 'needs_review'  # Unknown accreditation cannot select a tariff.


def test_overlapping_versions_are_ambiguous_not_cheapest_rate_selection():
    references(rate(), rate('another', reference_amount=50))
    result = run_healthcare_audit(data([item(price=200)]), get_services())
    row = findings(result, 'HEALTHCARE_BENCHMARK')[0]
    assert row['status'] == 'needs_review' and row['potential_difference'] is None


def test_nppa_requires_product_unit_and_tax_basis_matches():
    record = rate('nppa-fixture', 'NPPA', item_name='Synthetic medicine 5 mg tablet', category='PHARMACY',
        strength='5 mg', dosage_form='tablet', unit='per_tablet', reference_amount=10,
        price_basis='net_excluding_tax')
    references(record)
    line = item(description=record.item_name, category='PHARMACY', price=15, qty=2, unit='per_tablet',
                strength='5 mg', dosage_form='tablet', price_basis='net_excluding_tax')
    result = run_healthcare_audit(data([line]), get_services())
    row = findings(result, 'NPPA_REFERENCE')[0]
    assert row['status'] == 'flagged' and row['potential_difference'] == 10 and row['reference_amount'] == 20
    line.price_basis = 'unknown'
    assert findings(run_healthcare_audit(data([line]), get_services()), 'NPPA_REFERENCE')[0]['status'] == 'needs_review'
    line.price_basis = 'net_excluding_tax'
    line.strength = '10 mg'
    assert findings(run_healthcare_audit(data([line]), get_services()), 'NPPA_REFERENCE')[0]['potential_difference'] is None


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -1])
def test_structured_input_rejects_nonfinite_and_negative_amounts(invalid):
    with pytest.raises(ValidationError):
        data([item(price=invalid)])


def test_json_audit_api_reuses_existing_route_and_admin_checks(monkeypatch):
    import json
    from pathlib import Path
    request = data([item(price=200)])
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as client:
        response = client.post('/api/audit', json=request.model_dump(mode='json'))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['claim_id'] == request.claim_id and body['finding_counts']['needs_review'] == 3
        fixtures = Path(__file__).parent / 'fixtures' / 'healthcare'
        published_input = json.loads((fixtures / 'normalized_input.json').read_text(encoding='utf-8'))
        published_output = json.loads((fixtures / 'source_unavailable_output.json').read_text(encoding='utf-8'))
        assert client.post('/api/audit', json=published_input).json() == published_output
        invalid = request.model_dump(mode='json')
        invalid['bill']['line_items'][0]['amount'] = -1
        assert client.post('/api/audit', json=invalid).status_code == 422
        batch = ReferenceImport(rates=[rate()]).model_dump(mode='json')
        assert client.post('/api/admin/healthcare-references/import', json=batch).status_code == 403
        monkeypatch.setenv('CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS', 'local-demo')
        imported = client.post('/api/admin/healthcare-references/import', json=batch)
        assert imported.status_code == 200 and imported.json()['enforcement_enabled'] is False
        review = ReferenceReview(verification_status='approved', enforcement_enabled=True, currentness_checked=True,
            source_checked=True, interpretation_checked=True, explanation='SYNTHETIC test administrator review')
        response = client.post('/api/admin/healthcare-references/rates/test-cghs/review', json=review.model_dump(mode='json'))
        assert response.status_code == 200, response.text
        body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
        assert body['benchmark_variance'] == 100 and body['potential_discrepancy'] == 0
        content = client.get('/openapi.json').json()['paths']['/api/audit']['post']['requestBody']['content']
        assert 'application/json' in content and 'multipart/form-data' in content


def test_unknown_source_page_and_empty_bill_are_explicitly_uncertain():
    request = data([item(1, 'Room', 'ROOM', 5000, 2, unit='per_day')])
    clause = request.policy_doc.clauses[0].model_dump(exclude={'page'})
    request.policy_doc.clauses[0] = Clause(**clause)
    assert findings(audit_healthcare(request), 'ROOM_RENT_LIMIT')[0]['potential_difference'] is None
    result = audit_healthcare(data([]))
    assert result['finding_counts']['needs_review'] == 1


def test_source_integrity_failure_and_corrupt_reference_storage_fail_closed():
    registry = regulatory()
    document = registry.get_document('irdai-health-master-2024-en')
    registry.storage.put(document.original_pdf_ref, b'not the approved PDF')
    assert registry.item_rules(data().context)[0][2] == 'Undetermined'
    get_services().storage.put(HealthcareReferenceRegistry.KEY, b'invalid JSON')
    result = run_healthcare_audit(data(), get_services())
    assert result['warnings'] and result['potential_discrepancy'] == 0


def test_conflicting_policy_identity_and_invalid_service_date_never_activate_estimates():
    references(rate())
    request = data([item(1, 'Room', 'ROOM', 5000, 2, unit='per_day'), item(2, price=200, service_date='invalid')])
    request.bill.policy_number, request.policy_doc.policy_number = 'one', 'different'
    result = run_healthcare_audit(request, get_services())
    assert findings(result, 'ROOM_RENT_LIMIT')[0]['potential_difference'] is None
    lab = [f for f in result['findings'] if f['bill_item_id'] == '2']
    assert all(f['potential_difference'] is None for f in lab)


def test_existing_pdf_audit_exposes_additive_report_without_changing_recovery(monkeypatch):
    import time
    monkeypatch.setenv('CLAIMSHIELD_USE_ADK', 'false')
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as client:
        case_id = client.post('/api/audit', data={'sample_bill': 'BILL-001'}).json()['case_id']
        for _ in range(100):
            body = client.get(f'/api/cases/{case_id}').json()
            if body['status'] != 'running':
                break
            time.sleep(.05)
        assert body['status'] == 'complete', body
        assert body['reconciliation']['recovery_likely'] == 24846  # Existing answer key.
        assert body['healthcare_audit']['claim_id'] == case_id
        assert body['healthcare_audit']['finding_counts']['needs_review'] > 0
