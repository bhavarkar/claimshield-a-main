import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from claimshield.api.main import app
from claimshield.models import Clause, PolicyDoc
from claimshield.services.preauthorization import PreAuthorizationIntake, prepare_request


def _intake(**overrides):
    values = {"patient_name": "Sam Example", "patient_consent": True, "treatment": "Knee replacement",
              "estimated_cost": 125000, "hospital_name": "Example Hospital", "hospital_address": "10 Main Road",
              "network_status": "unknown", "insurer_name": "Northstar Health", "policy_number": "P-100",
              "document_types": ["hospital_estimate", "doctor_advice", "policy_schedule"]}
    values.update(overrides)
    return PreAuthorizationIntake(**values)


def test_complete_request_has_complete_document_checklist_and_summary():
    result = prepare_request(_intake(), None, ["estimate.pdf"])
    assert result["documents_complete"] is True
    assert result["missing_required_documents"] == []
    assert "cannot grant pre-authorization" in result["approval_disclaimer"].lower()
    assert "not been submitted" in result["summary"]


def test_missing_patient_consent_is_rejected():
    with pytest.raises(ValidationError, match="consent"):
        _intake(patient_consent=False)


def test_missing_hospital_information_is_rejected():
    with pytest.raises(ValidationError):
        _intake(hospital_name="")


def test_invalid_treatment_cost_is_rejected():
    with pytest.raises(ValidationError):
        _intake(estimated_cost=-1)


def test_unknown_network_status_is_flagged_without_assuming_network_coverage():
    result = prepare_request(_intake(), None, [])
    assert any(w["kind"] == "network_status" for w in result["warnings"])


def test_conflicting_policy_information_is_flagged_for_review():
    policy = PolicyDoc(insurer="Other Health", policy_number="P-999", clauses=[
        Clause(clause_id="4.1", title="Knee exclusion", text="Knee replacement is excluded from policy benefits.")])
    result = prepare_request(_intake(), policy, [])
    assert result["documents_complete"] is False
    assert len([w for w in result["warnings"] if w["kind"] == "policy_conflict"]) == 2
    assert result["coverage_restriction_status"] == "restricted"


def test_pre_authorization_api_persists_request_and_manual_status():
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        response = client.post("/api/preauthorizations", data={"intake": _intake().model_dump_json()})
        assert response.status_code == 200, response.text
        data = response.json()
        resource_id = data["preauthorization_id"]
        assert client.get(f"/api/preauthorizations/{resource_id}").json()["authorization_status"] == "not_recorded"
        updated = client.patch(f"/api/preauthorizations/{resource_id}/status", json={"status": "pending"})
        assert updated.status_code == 200
        assert updated.json()["status_source"].startswith("manual user entry")
        assert client.get(f"/api/preauthorizations/{resource_id}").json()["authorization_status"] == "pending"
