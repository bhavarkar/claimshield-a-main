"""Tests for Person 2: Multi-instance reliability, durable job queue, concurrent registry updates,
and deployment safeguards.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claimshield.api.auth import Principal
from claimshield.api.main import app
from claimshield.services.cases import CaseRepository
from claimshield.services.healthcare_references import (
    HealthcareRate,
    HealthcareReferenceRegistry,
    ReferenceImport,
    ReferenceReview,
    RoomAdjustmentRule,
)
from claimshield.services.jobs import JobQueue
from claimshield.services.regulatory import (
    DocumentReview,
    RegulatoryRegistry,
    ReviewStatus,
)
from claimshield.services.storage import LocalStorage, StorageConcurrencyError


@pytest.fixture
def temp_storage(tmp_path: Path):
    return LocalStorage(tmp_path / "var")


def test_storage_optimistic_concurrency_generation_match(temp_storage):
    key = "test/doc.txt"
    temp_storage.put(key, b"initial")
    content, gen1 = temp_storage.get_with_generation(key)
    assert content == b"initial"
    assert gen1 != 0

    # Updating with valid generation succeeds
    temp_storage.put_if_generation_match(key, b"second", gen1)
    content2, gen2 = temp_storage.get_with_generation(key)
    assert content2 == b"second"
    assert gen2 != gen1

    # Updating with stale generation fails with StorageConcurrencyError
    with pytest.raises(StorageConcurrencyError):
        temp_storage.put_if_generation_match(key, b"third", gen1)


def test_job_queue_multi_instance_status_lookup(temp_storage, tmp_path):
    # Simulate Instance A
    queue_a = JobQueue(tmp_path / "instance_a" / "jobs.sqlite3", storage=temp_storage)
    case_id = "case-test-12345"
    owner = "owner-abc"
    payload = {"bill_key": "k1", "policy_key": "k2"}

    queue_a.submit(case_id, owner, payload)
    status_a = queue_a.status(case_id)
    assert status_a is not None
    assert status_a["status"] == "queued"

    # Simulate Instance B (completely separate SQLite file, but sharing the same durable storage)
    queue_b = JobQueue(tmp_path / "instance_b" / "jobs.sqlite3", storage=temp_storage)
    status_b = queue_b.status(case_id)
    assert status_b is not None
    assert status_b["status"] == "queued"

    # Instance A claims and processes the job
    job = queue_a.claim()
    assert job is not None
    assert job["id"] == case_id

    # Instance B checks status while running
    status_b_running = queue_b.status(case_id)
    assert status_b_running is not None
    assert status_b_running["status"] == "running"

    # Instance A finishes the job
    queue_a.finish(job)
    status_b_complete = queue_b.status(case_id)
    assert status_b_complete is not None
    assert status_b_complete["status"] == "complete"


def test_healthcare_reference_registry_concurrent_write_detection(temp_storage):
    reg_a = HealthcareReferenceRegistry(temp_storage)
    reg_b = HealthcareReferenceRegistry(temp_storage)

    rate = HealthcareRate(
        record_id="cghs-cbc-1",
        item_name="Complete Blood Count",
        source_name="CGHS Delhi",
        source_type="CGHS",
        source_url="https://cghs.nic.in/tariffs.pdf",
        version="2024-v1",
        category="LAB",
        unit="per test",
        reference_amount=150.0,
        synthetic=True,
    )
    reg_a.import_records(ReferenceImport(rates=[rate]))

    # Both instances load the registry at the current generation
    data_a, gen_a = reg_a._load()
    data_b, gen_b = reg_b._load()
    assert gen_a == gen_b

    # Administrator A submits a review -> succeeds
    review_a = ReferenceReview(
        verification_status="needs_update",
        explanation="First reviewer notes additional verification needed",
    )
    reg_a.review("rates", "cghs-cbc-1", review_a, reviewer="reviewer_a")

    # Administrator B attempts to save using the stale generation loaded earlier
    with pytest.raises(HTTPException) as exc_info:
        reg_b._save(data_b, gen_b)
    assert exc_info.value.status_code == 409
    assert "concurrently" in exc_info.value.detail

    # Verify history snapshot was saved
    history_key = "healthcare-reference/history/v2.json"
    assert temp_storage.exists(history_key)


def test_regulatory_registry_concurrent_write_detection(temp_storage):
    reg_a = RegulatoryRegistry(temp_storage)
    reg_b = RegulatoryRegistry(temp_storage)

    # Both load initial seed registry
    data_a, gen_a = reg_a._load()
    data_b, gen_b = reg_b._load()
    assert gen_a == gen_b

    # Administrator A updates a document review
    review = DocumentReview(
        human_review_status=ReviewStatus.REJECTED,
        currentness_checked=True,
        reviewer_explanation="Reviewed and marked superseded",
    )
    reg_a.review_document("irdai-health-master-2024-en", review, reviewer_id="admin-a")

    # Administrator B attempts to write with stale generation
    with pytest.raises(HTTPException) as exc_info:
        reg_b._save(data_b, gen_b)
    assert exc_info.value.status_code == 409
    assert "concurrently" in exc_info.value.detail


def test_internal_job_process_endpoint_security_and_idempotency(temp_storage, monkeypatch):
    from claimshield.services import get_services
    monkeypatch.setattr(get_services(), "storage", temp_storage)

    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), headers={"X-ClaimShield-Request": "1"}) as client:
        case_id = "case-test-internal-999"
        # Unauthorized request from non-loopback with wrong token
        with TestClient(app, base_url="http://example.com", client=("192.168.1.100", 50000)) as unauth_client:
            resp_unauth = unauth_client.post(f"/api/internal/jobs/{case_id}/process", headers={"X-ClaimShield-Internal-Token": "wrong"})
            assert resp_unauth.status_code == 403

        # Create dummy case.json marked complete
        temp_storage.put(f"cases/{case_id}/case.json", json.dumps({"case_id": case_id, "status": "complete"}).encode())

        # Call with valid loopback
        resp = client.post(f"/api/internal/jobs/{case_id}/process")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "complete"
        assert data.get("already_processed") is True

