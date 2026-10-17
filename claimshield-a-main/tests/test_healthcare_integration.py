"""Integration tests use isolated storage and explicitly synthetic source records only."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from claimshield.api import main
from claimshield.services import get_services
from claimshield.services.healthcare_references import ReferenceImport, ReferenceReview
from claimshield.services.regulatory import RegulatoryRegistry
from tests.test_auth_and_isolation import identity, headers, ALICE, BOB, ORIGIN
from tests.test_healthcare_audit import data, item, rate, room_rule, regulatory


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS', 'local-demo')
    monkeypatch.setenv('CLAIMSHIELD_USE_ADK', 'false')
    with TestClient(main.app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as connection:
        yield connection


def activate(client, *records, room_rules=()):
    batch = ReferenceImport(rates=list(records), room_rules=list(room_rules)).model_dump(mode='json')
    response = client.post('/api/admin/healthcare-references/import', json=batch)
    assert response.status_code == 200, response.text
    review = ReferenceReview(verification_status='approved', enforcement_enabled=True, source_checked=True,
        interpretation_checked=True, currentness_checked=True, explanation='SYNTHETIC integration fixture review only').model_dump(mode='json')
    for kind, key in [('rates', r.record_id) for r in records] + [('room_rules', r.rule_id) for r in room_rules]:
        response = client.post(f'/api/admin/healthcare-references/{kind}/{key}/review', json=review)
        assert response.status_code == 200, response.text


def wait(client, case_id):
    for _ in range(150):
        response = client.get('/api/cases/' + case_id)
        assert response.status_code == 200, response.text
        body = response.json()
        if body['status'] != 'running':
            assert body['status'] == 'complete', body
            return body
        time.sleep(.05)
    raise AssertionError('Audit did not finish')


def test_json_has_shared_report_envelope_and_flat_compatibility(client):
    response = client.post('/api/audit', json=data().model_dump(mode='json'))
    assert response.status_code == 200
    body = response.json()
    assert body['healthcare_audit']['findings'] == body['findings']
    assert body['healthcare_audit']['potential_discrepancy'] == body['potential_discrepancy']
    assert body['status'] == 'complete'


@pytest.mark.parametrize('payload', [[], {'schema_version': 1, 'rates': [], 'room_rules': {}},
                                   {'schema_version': 1, 'rates': {}, 'room_rules': None}])
def test_valid_json_but_corrupt_catalog_shape_fails_closed(client, payload):
    get_services().storage.put('healthcare-reference/registry.json', json.dumps(payload).encode())
    result = client.post('/api/audit', json=data().model_dump(mode='json'))
    assert result.status_code == 200
    body = result.json()
    assert body['warnings'] and body['potential_discrepancy'] == 0


def test_guided_claim_type_reaches_stored_report_context(client, monkeypatch):
    seen = []
    original = RegulatoryRegistry.item_rules
    def capture(self, context):
        seen.append(context.claim_context)
        return original(self, context)
    monkeypatch.setattr(RegulatoryRegistry, 'item_rules', capture)
    root = get_services().settings.data_dir / 'samples'
    response = client.post('/api/claim-review', data={'claim_type': 'cashless'}, files=[
        ('medical_documents', ('bill.pdf', (root / 'bills/bill_001_sunrise_appendectomy.pdf').read_bytes())),
        ('insurance_documents', ('policy.pdf', (root / 'policies/suraksha_health_gold.pdf').read_bytes()))])
    assert response.status_code == 200, response.text
    wait(client, response.json()['case_id'])
    assert seen == ['cashless']


def test_scenario_a_clean_claim_through_real_api(client):
    activate(client, rate(), rate('test-pmjay', 'PMJAY'))
    regulatory()  # Existing PDF ingestion/review service; synthetic source only.
    body = client.post('/api/audit', json=data([item(price=90)]).model_dump(mode='json')).json()
    assert body['finding_counts'] == {'clear': 3, 'flagged': 0, 'needs_review': 0}
    assert body['potential_discrepancy'] == body['benchmark_variance'] == 0


def test_scenario_b_flagged_claim_through_real_api(client):
    activate(client, rate(), rate('test-pmjay', 'PMJAY', reference_amount=120), room_rules=[room_rule()])
    request = data([item(1, 'Room', 'ROOM', 6000, 2, unit='per_day'),
                    item(2, 'Consultation', 'CONSULT', 1000), item(3, price=150)])
    body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
    assert body['potential_discrepancy'] == 6500
    assert body['benchmark_variance'] == 50
    prop = [f for f in body['findings'] if f['type'] == 'ROOM_RENT_PROPORTIONATE' and f['status'] == 'flagged']
    assert prop[0]['potential_difference'] == 500 and prop[0]['evidence']['source']['verification_status'] == 'approved'
    assert prop[0]['explanation']


@pytest.mark.parametrize('description', ['CBC with ESR', 'CBC + Platelet Count', 'CBC Profile'])
def test_scenario_c_compound_descriptions_require_review(client, description):
    activate(client, rate(aliases=['Complete Blood Count', 'Hemogram']))
    body = client.post('/api/audit', json=data([item(description=description, price=200)]).model_dump(mode='json')).json()
    assert body['potential_discrepancy'] == body['benchmark_variance'] == 0
    assert body['finding_counts']['needs_review'] > 0
    assert all(f['potential_difference'] is None for f in body['findings'])


@pytest.mark.parametrize('description', ['CBC', 'Complete Blood Count', 'Hemogram'])
def test_explicit_aliases_match_without_global_medical_equivalence(client, description):
    activate(client, rate(aliases=['Complete Blood Count', 'Hemogram']))
    body = client.post('/api/audit', json=data([item(description=description, price=120)]).model_dump(mode='json')).json()
    row = next(f for f in body['findings'] if f['rule_id'] == 'test-cghs')
    assert row['match_status'] == 'confirmed_match' and row['confidence_score'] >= .98
    assert body['benchmark_variance'] == 20 and body['potential_discrepancy'] == 0


def test_scenario_d_sources_unavailable_is_an_explicit_warning(client):
    body = client.post('/api/audit', json=data().model_dump(mode='json')).json()
    assert body['warnings']
    assert body['potential_discrepancy'] == body['benchmark_variance'] == 0
    assert all(f['reference_amount'] is None for f in body['findings'])


def test_scenario_e_pdf_upload_persists_report_and_survives_memory_clear(client):
    root = get_services().settings.data_dir / 'samples'
    response = client.post('/api/audit', files={
        'bill': ('bill.pdf', (root / 'bills/bill_001_sunrise_appendectomy.pdf').read_bytes()),
        'policy': ('policy.pdf', (root / 'policies/suraksha_health_gold.pdf').read_bytes())})
    assert response.status_code == 200 and set(response.json()) == {'case_id'}
    case_id = response.json()['case_id']
    result = wait(client, case_id)
    saved = json.loads(get_services().storage.get(f'cases/{case_id}/case.json'))
    assert result['healthcare_audit'] == saved['healthcare_audit']
    assert result['reconciliation']['recovery_likely'] == 24846
    main.CASES.clear()
    main.TRACES.discard(case_id)
    assert client.get('/api/cases/' + case_id).json()['healthcare_audit'] == result['healthcare_audit']
    assert 'event: end' in client.get(f'/api/cases/{case_id}/events').text
    assert client.get(result['files']['reconciliation_report.pdf']).content.startswith(b'%PDF')


@pytest.mark.parametrize('body', [b'{', b'[]', b'null', b'{"claim_id":"x"}'])
def test_json_validation_errors_are_422_and_serializable(client, body):
    response = client.post('/api/audit', content=body, headers={'Content-Type': 'application/json; charset=utf-8'})
    assert response.status_code == 422 and isinstance(response.json()['detail'], list)


def test_reference_administration_authorization(identity, monkeypatch):
    monkeypatch.setenv('CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS', ALICE)
    batch = ReferenceImport(rates=[rate()]).model_dump(mode='json')
    with TestClient(main.app, base_url=ORIGIN) as client:
        path = '/api/admin/healthcare-references/import'
        assert client.post(path, json=batch).status_code == 401
        assert client.post(path, json=batch, headers=headers(identity(BOB))).status_code == 403
        imported = client.post(path, json=batch, headers=headers(identity(ALICE)))
        assert imported.status_code == 200 and not imported.json()['enforcement_enabled']
        review_path = '/api/admin/healthcare-references/rates/test-cghs/review'
        review = {'verification_status': 'rejected', 'explanation': 'Synthetic fixture cannot be used in production'}
        assert client.post(review_path, json=review).status_code == 401
        assert client.post(review_path, json=review, headers=headers(identity(BOB))).status_code == 403
        assert client.post(review_path, json=review, headers=headers(identity(ALICE))).status_code == 200


def test_fractional_room_days_round_in_decimal_not_binary_float(client):
    request = data([item(1, 'Room', 'ROOM', 2, .1, unit='per_day')], limit=1.15)
    body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
    row = next(f for f in body['findings'] if f['type'] == 'ROOM_RENT_LIMIT')
    assert row['reference_amount'] == .12 and row['potential_difference'] == .08


def test_one_paise_reference_difference_is_not_rounded_into_clear(client):
    activate(client, rate(reference_amount=1.14))
    body = client.post('/api/audit', json=data([item(price=1.15)]).model_dump(mode='json')).json()
    row = next(f for f in body['findings'] if f['rule_id'] == 'test-cghs')
    assert row['status'] == 'flagged' and row['comparison'] == 'above_benchmark'
    assert body['benchmark_variance'] == .01 and body['potential_discrepancy'] == 0


def test_gemini_fallback_preserves_normalization_unit_contract(monkeypatch):
    from types import SimpleNamespace
    from claimshield.agents.steps import intake_step
    from claimshield.services.parsing import RawDocument
    from claimshield.trace import Trace
    svc = get_services()
    policy_bytes = (svc.settings.data_dir / 'samples/policies/suraksha_health_gold.pdf').read_bytes()
    policy_raw = svc.local_parser.parse(policy_bytes)
    bill_raw = RawDocument(pages=['SYNTHETIC unfamiliar bill layout'])
    svc.storage.put('integration-bill.pdf', b'Synthetic layout fixture')
    svc.storage.put('integration-policy.pdf', policy_bytes)
    svc.parser = SimpleNamespace(name='synthetic parser', parse=lambda content: policy_raw if content == policy_bytes else bill_raw)
    svc.llm = SimpleNamespace(available=True, name='mocked Gemini', extract_bill=lambda _: {
        'line_items': [{'line_no': 1, 'service_date': '2026-02-01', 'description': 'Room', 'dept': 'room',
                        'qty': 2, 'unit_price': 5000, 'amount': 10000}], 'stated_gross_total': 10000})
    result = intake_step({'case_id': 'integration-fallback', 'bill_key': 'integration-bill.pdf',
                          'policy_key': 'integration-policy.pdf'}, Trace('integration-fallback'), svc)
    assert result['bill']['line_items'][0]['unit'] == 'per_day'


def test_synthetic_reference_cannot_be_activated_by_production_admin(identity, monkeypatch):
    monkeypatch.setenv('CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS', ALICE)
    with TestClient(main.app, base_url=ORIGIN, headers=headers(identity(ALICE))) as client:
        batch = ReferenceImport(rates=[rate()]).model_dump(mode='json')
        assert client.post('/api/admin/healthcare-references/import', json=batch).status_code == 200
        review = ReferenceReview(verification_status='approved', enforcement_enabled=True, source_checked=True,
            interpretation_checked=True, currentness_checked=True, explanation='This test source must not activate in production')
        response = client.post('/api/admin/healthcare-references/rates/test-cghs/review', json=review.model_dump(mode='json'))
        assert response.status_code == 409


def test_demo_approved_sources_are_inactive_after_switch_to_production(client, monkeypatch):
    activate(client, rate(), room_rules=[room_rule()])
    registry = regulatory()
    monkeypatch.setenv('CLAIMSHIELD_AUTH_MODE', 'iap')
    from claimshield.services.healthcare_audit import run_healthcare_audit
    request = data([item(price=200)])
    body = run_healthcare_audit(request, get_services())
    assert body['benchmark_variance'] == body['potential_discrepancy'] == 0
    assert registry.item_rules(request.context)[0][2] == 'Undetermined'


def test_explicit_measurement_unit_column_is_not_parsed_as_currency():
    from claimshield.services.parsing import RawDocument, normalize_bill, charge_unit
    raw = RawDocument(pages=['Synthetic unit-column fixture'], tables=[[
        ['#', 'Description', 'Qty', 'Unit', 'Rate', 'Amount'],
        ['1', 'CBC', '2', 'per_test', '10', '20']]])
    line = normalize_bill(raw).line_items[0]
    assert line.unit_price == 10 and line.unit == 'per_test'
    assert charge_unit('ICU') == 'per_day'


def test_overlapping_adjustments_and_nppa_quantity_do_not_double_count(client):
    medicine = rate('nppa-fixture', 'NPPA', item_name='Admission kit', category='NON_MEDICAL')
    # NPPA compares relevant devices/medicines; unrelated non-medical references never apply.
    activate(client, medicine)
    regulatory()
    request = data([item(1, 'Admission kit', 'NON_MEDICAL', 100)])
    body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
    assert body['potential_discrepancy'] == 100
    assert not any(f['type'] == 'NPPA_REFERENCE' for f in body['findings'])
    request = data([item(1, 'Room', 'ROOM', 6000, 1, unit='per_day'), item(2, 'Admission kit', 'NON_MEDICAL', 100)])
    quote = 'SYNTHETIC fixture: admission kit is an affected non-medical charge for the allowed/actual room adjustment.'
    request.policy_doc.clauses[1].text = quote
    rule = room_rule(affected_categories=['NON_MEDICAL'], excluded_categories=[])
    rule.policy_citation.quote = quote
    activate(client, room_rules=[rule])
    body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
    assert body['potential_discrepancy'] == 3100  # Room 3000 plus max(exclusion 100, proportion 50).
    drug = rate('nppa-drug', 'NPPA', item_name='Fixture medicine', category='PHARMACY', strength='5 mg',
                dosage_form='tablet', unit='per_tablet', reference_amount=10, price_basis='net_excluding_tax')
    activate(client, drug)
    request = data([item(1, 'Fixture medicine', 'PHARMACY', 15, 3, unit='per_tablet', strength='5 mg',
                         dosage_form='tablet', price_basis='net_excluding_tax')])
    body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
    assert body['potential_discrepancy'] == 15
    request.bill.line_items[0].price_basis = 'unknown'
    body = client.post('/api/audit', json=request.model_dump(mode='json')).json()
    assert body['potential_discrepancy'] == 0
    assert any(f['type'] == 'NPPA_REFERENCE' and f['status'] == 'needs_review' for f in body['findings'])


@pytest.mark.parametrize('text', ['[]', '{"pages":null}', '{"pages":[42]}'])
def test_corrupted_source_page_structure_never_becomes_an_exclusion(client, text):
    registry = regulatory()
    source = registry.get_document('irdai-health-master-2024-en')
    get_services().storage.put(source.extracted_text_ref, text.encode())
    response = client.post('/api/audit', json=data([item(description='Admission kit', category='NON_MEDICAL')]).model_dump(mode='json'))
    assert response.status_code == 200
    assert response.json()['potential_discrepancy'] == 0
    assert response.json()['warnings']


def test_deployment_env_file_transports_reference_admin_allowlist(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = (Path(__file__).resolve().parents[1] / 'scripts/deploy_cloud_run.sh').read_text()
    embedded = script.split('"$PYTHON" - "$ENV_FILE" <<\'PY\'\n', 1)[1].split('\nPY', 1)[0]
    monkeypatch.setenv('CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS', 'test-admin-one,test-admin-two')
    target = tmp_path / 'deployment-env.json'
    subprocess.run([sys.executable, '-', str(target)], input=embedded, text=True, check=True, env=os.environ.copy())
    assert json.loads(target.read_text())['CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS'] == 'test-admin-one,test-admin-two'


def test_real_http_server_json_and_pdf_paths(tmp_path):
    """Exercise an independent uvicorn process, actual sockets, queue and saved result."""
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import httpx
    from tests.conftest import LOCAL_ENV

    root = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    work = tmp_path / 'http-cases'
    env = {**os.environ, **LOCAL_ENV, 'CLAIMSHIELD_USE_ADK': 'false',
           'CLAIMSHIELD_WORK_DIR': str(work), 'CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS': 'local-demo'}
    with (tmp_path / 'http-server.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'claimshield.api.main:app',
            '--host', '127.0.0.1', '--port', str(port)], cwd=root, env=env, stdout=log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=30,
                              headers={'X-ClaimShield-Request': '1'}) as connection:
                for _ in range(150):
                    try:
                        if connection.get('/api/health').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    assert process.poll() is None, 'HTTP server exited before startup'
                    time.sleep(.1)
                else:
                    raise AssertionError('HTTP server did not start')
                # D: no catalog produces warnings without invented money.
                missing = connection.post('/api/audit', json=data().model_dump(mode='json'))
                assert missing.status_code == 200 and missing.json()['warnings']
                assert missing.json()['potential_discrepancy'] == 0
                activate(connection, rate(), rate('test-pmjay', 'PMJAY', reference_amount=120),
                         room_rules=[room_rule()])
                # A: available price checks clear; absent regulatory source remains review.
                clean = connection.post('/api/audit', json=data([item(price=90)]).model_dump(mode='json')).json()
                assert clean['finding_counts']['clear'] == 2 and clean['potential_discrepancy'] == 0
                # B: exact published request, configured synthetic policy and benchmarks.
                demo = json.loads((root / 'tests/fixtures/healthcare/integration_demo_request.json').read_text())
                flagged = connection.post('/api/audit', json=demo).json()
                assert flagged['status'] == 'complete' and flagged['total_billed'] == 13150
                assert flagged['potential_discrepancy'] == 6500 and flagged['benchmark_variance'] == 50
                assert flagged['healthcare_audit']['findings'] == flagged['findings']
                # C: compound service never becomes a supported deduction.
                ambiguous = connection.post('/api/audit', json=data([item(description='CBC with ESR', price=200)]).model_dump(mode='json')).json()
                assert ambiguous['finding_counts']['needs_review'] > 0
                assert ambiguous['potential_discrepancy'] == ambiguous['benchmark_variance'] == 0
                # E: multipart returns the legacy identifier; worker saves the additive report.
                samples = root / 'data/samples'
                uploaded = connection.post('/api/audit', files={
                    'bill': ('bill.pdf', (samples / 'bills/bill_001_sunrise_appendectomy.pdf').read_bytes()),
                    'policy': ('policy.pdf', (samples / 'policies/suraksha_health_gold.pdf').read_bytes())})
                assert uploaded.status_code == 200 and set(uploaded.json()) == {'case_id'}
                case_id = uploaded.json()['case_id']
                result = wait(connection, case_id)
                saved = json.loads((work / 'cases' / case_id / 'case.json').read_text())
                assert saved['healthcare_audit'] == result['healthcare_audit']
                assert result['reconciliation']['recovery_likely'] == 24846
                assert 'event: end' in connection.get(f'/api/cases/{case_id}/events').text
                assert connection.get(result['files']['reconciliation_report.pdf']).content.startswith(b'%PDF')
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
