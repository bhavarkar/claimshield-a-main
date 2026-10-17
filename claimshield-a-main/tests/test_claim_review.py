import asyncio
import json
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from claimshield.api.main import app
from claimshield.config import PROJECT_ROOT
from claimshield.services.jobs import JobQueue, QueueFull
from claimshield.agents.claim_review import clause_review_agent
from claimshield.trace import Trace


def _clause_ai(proposals):
    policy_quote = 'Inpatient appendectomy is covered subject to the stated conditions.'
    medical_quote = 'Patient underwent inpatient appendectomy on the date of admission.'
    state = {'policy_doc': {'clauses': [{'clause_id': '1.1', 'title': 'Surgery', 'text': policy_quote}]}}
    pages = [{'document_id': 'D1', 'page': 1, 'filename': 'report.pdf', 'text': medical_quote}]
    draft = {'clause_id': '1.1', 'assessment': 'potentially_eligible', 'policy_quote': policy_quote,
             'explanation': 'The procedure is mentioned in both documents; confirm the remaining conditions.',
             'evidence': [{'document_id': 'D1', 'page': 1, 'quote': medical_quote}], 'missing_information': []}
    supplied = proposals(draft)
    svc = SimpleNamespace(llm=SimpleNamespace(available=True, review_claim_clauses=lambda *args: supplied))
    return clause_review_agent(state, pages, svc, Trace('test-ai'))


def test_ai_clause_draft_requires_exact_document_and_policy_quotes():
    result = _clause_ai(lambda draft: [draft])
    assert result['proposals'][0]['review_status'] == 'unconfirmed_ai_draft'
    assert result['proposals'][0]['evidence'][0]['filename'] == 'report.pdf'
    for mutation in [
        {'policy_quote': 'This is invented policy wording and must be rejected.'},
        {'evidence': [{'document_id': 'D2', 'page': 1, 'quote': 'An invented report quote'}]},
        {'evidence': []},
        {'assessment': 'approved'},
        {'explanation': 'You will receive Rs. 999999.'},
        {'clause_id': ['invalid']},
        {'evidence': [{'document_id': [], 'page': 1, 'quote': 'An invented report quote'}]},
    ]:
        assert not _clause_ai(lambda draft: [{**draft, **mutation}])['proposals']
    assert not _clause_ai(lambda draft: [draft, draft])['proposals']


def test_missing_evidence_downgrades_ai_proposal():
    result = _clause_ai(lambda draft: [{**draft, 'missing_information': ['Confirm policy start date']}])
    assert result['proposals'][0]['assessment'] == 'needs_verification'


def test_queue_capacity_is_atomic_and_owner_scoped(tmp_path):
    queue = JobQueue(tmp_path / 'queue.db', capacity=4, per_owner=2)
    def submit(i):
        try:
            queue.submit(str(i), 'alice', {'i': i})
            return True
        except QueueFull:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(submit, range(12))) == 2
    queue.submit('bob', 'bob', {})
    queue.submit('carol', 'carol', {})
    with pytest.raises(QueueFull):
        queue.submit('dave', 'dave', {})


def test_worker_leases_restart_recovery_and_stale_ack(tmp_path):
    path = tmp_path / 'queue.db'
    queue = JobQueue(path)
    queue.submit('case-1', 'owner', {'test': 'persisted'})
    first = queue.claim()
    assert first['payload'] == {'test': 'persisted'}
    restarted = JobQueue(path)
    assert restarted.claim() is None
    with queue.connect() as db:
        db.execute('UPDATE jobs SET lease=0')
    second = restarted.claim()
    assert second['attempts'] == 2
    queue.finish(first)  # An old worker cannot acknowledge the new lease.
    assert queue.status('case-1')['status'] == 'running'
    restarted.finish(second, retry=True)
    assert queue.claim() is None  # Backoff.
    with queue.connect() as db:
        db.execute('UPDATE jobs SET available=0')
    third = queue.claim()
    queue.finish(third)
    assert queue.claim() is None


def test_claim_review_round_trip_and_validation(monkeypatch):
    monkeypatch.setenv('CLAIMSHIELD_USE_ADK', 'false')
    samples = PROJECT_ROOT / 'data' / 'samples'
    bill = (samples / 'bills' / 'bill_001_sunrise_appendectomy.pdf').read_bytes()
    policy = (samples / 'policies' / 'suraksha_health_gold.pdf').read_bytes()
    files = [('medical_documents', ('bill.pdf', bill, 'application/pdf')),
             ('medical_documents', ('duplicate.pdf', bill, 'application/pdf')),
             ('insurance_documents', ('policy.pdf', policy, 'application/pdf'))]
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as client:
        assert client.post('/api/claim-review', files=files, data={'main_policy': 7}).status_code == 422
        assert client.post('/api/claim-review', files=files, data={'claim_type': 'invented'}).status_code == 422
        response = client.post('/api/claim-review', files=files)
        assert response.status_code == 200, response.text
        case_id = response.json()['case_id']
        for _ in range(200):
            result = client.get(f'/api/cases/{case_id}').json()
            if result['status'] != 'running':
                break
            time.sleep(.05)
        assert result['status'] == 'complete', result
        review = result['claim_review']
        assert review['status'] == 'needs_verification'
        assert review['clause_count'] == len(result['policy_doc']['clauses']) > 0
        assert review['document_check']['documents'][1]['status'] == 'duplicate'
        assert review['document_check']['summary']['missing'] > 0
        assert review['next_steps'] and review['verification_gaps']
        assert review['recovery_scenario']['confirmed'] is False
        assert review['recovery_scenario']['likely'] == 24846
        for clause, source in zip(review['clauses'], result['policy_doc']['clauses']):
            assert clause['text'] == source['text']
            assert clause['page'] == source['page']
        assert 'event: end' in client.get(f'/api/cases/{case_id}/events').text


def test_guidance_does_not_approve_missing_or_conflicting_evidence():
    from claimshield.agents.claim_review import guidance_agent
    result = guidance_agent({'bill': {'policy_number': 'one'}, 'policy_doc': {'policy_number': 'two'}},
                            {'checklist': [], 'warnings': []},
                            [{'group': 'insurance', 'primary': False}])
    assert result['status'] == 'needs_verification'
    assert any('numbers differ' in gap for gap in result['verification_gaps'])
    assert any('Additional insurance' in gap for gap in result['verification_gaps'])
    assert any('No numbered' in gap for gap in result['verification_gaps'])
    assert result['recovery_scenario']['likely'] is None


def test_burst_requests_respect_worker_and_owner_limits(monkeypatch):
    from claimshield.api import main
    monkeypatch.setenv('CLAIMSHIELD_WORKERS', '2')
    monkeypatch.setenv('CLAIMSHIELD_USER_PENDING_LIMIT', '8')
    active, maximum, completed = 0, 0, []

    async def audit(case_id, *args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(.25)
            completed.append(case_id)
            return {'case_id': case_id, 'status': 'complete'}
        finally:
            active -= 1

    monkeypatch.setattr(main, 'run_audit', audit)
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as client:
        ids = []
        for _ in range(8):
            response = client.post('/api/audit', data={'sample_bill': 'BILL-001'})
            assert response.status_code == 200
            ids.append(response.json()['case_id'])
        for _ in range(100):
            if len(completed) == 8:
                break
            time.sleep(.05)
        assert len(completed) == len(set(completed)) == 8
        assert maximum == 2
        for case_id in ids:
            assert client.get(f'/api/cases/{case_id}').json()['status'] == 'complete'


def test_api_backpressure_returns_retry_header(monkeypatch):
    from claimshield.api import main
    monkeypatch.setenv('CLAIMSHIELD_USER_PENDING_LIMIT', '1')

    async def slow_audit(case_id, *args, **kwargs):
        await asyncio.sleep(.5)
        return {'case_id': case_id, 'status': 'complete'}

    monkeypatch.setattr(main, 'run_audit', slow_audit)
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as client:
        first = client.post('/api/audit', data={'sample_bill': 'BILL-001'})
        assert first.status_code == 200
        second = client.post('/api/audit', data={'sample_bill': 'BILL-001'})
        assert second.status_code == 429
        assert second.headers['retry-after'] == '10'
        for _ in range(50):
            if client.get('/api/cases/' + first.json()['case_id']).json()['status'] == 'complete':
                break
            time.sleep(.05)


def test_http_worker_retries_transient_failure(monkeypatch):
    from claimshield.api import main
    original = main._process_job
    attempts = []

    async def transient(case_id, payload):
        attempts.append(case_id)
        if len(attempts) == 1:
            raise OSError('temporary storage failure')
        await original(case_id, payload)

    monkeypatch.setenv('CLAIMSHIELD_USE_ADK', 'false')
    monkeypatch.setattr(main, '_process_job', transient)
    with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000),
                    headers={'X-ClaimShield-Request': '1'}) as client:
        response = client.post('/api/audit', data={'sample_bill': 'BILL-001'})
        case_id = response.json()['case_id']
        for _ in range(200):
            result = client.get(f'/api/cases/{case_id}').json()
            if result['status'] != 'running':
                break
            time.sleep(.05)
        assert result['status'] == 'complete'
        assert attempts == [case_id, case_id]
