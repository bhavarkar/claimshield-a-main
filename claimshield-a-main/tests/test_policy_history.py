from datetime import date
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from claimshield.api.main import app
from claimshield.models import Clause, PolicyDoc
from claimshield.services.policy_history import PolicyHistoryRequest, check_policy_history


class _Retriever:
    def search(self, policy, query, top_k=8):
        return [clause for clause in policy.clauses if "wait" in clause.text.lower()][:top_k]


def _document(kind, start, expiry, *, number="P-100", endorsement=False):
    lines = [f"Policy Start Date: {start}", f"Policy Expiry Date: {expiry}"]
    if endorsement:
        lines.append("Endorsement No. EN-7 effective 01/03/2026: updated treatment schedule")
    text = "\n".join(lines)
    policy = PolicyDoc(insurer="Example Health", plan_name="Gold", policy_number=number,
        clauses=[Clause(clause_id="5.2", title="Waiting period", text="A waiting period applies to knee replacement for 24 months.", page=2)])
    return {"policy": policy, "text": text, "bytes": text.encode(), "filename": f"{kind}.pdf"}


def _request(docs, *, treatment_date="2026-10-01", continuity="unknown"):
    return PolicyHistoryRequest(treatment="Knee replacement", treatment_date=treatment_date,
        continuity_status=continuity, documents=[{"document_type": item} for item in docs])


def test_first_year_waiting_period_uses_policy_start_and_versioned_clause():
    prior = _document("prior", "01/01/2025", "31/12/2025")
    portability = _document("portability", "01/01/2025", "31/12/2025")
    portability["text"] += "\nPortability Certificate: continuity benefits accepted and credits transferred."
    portability["bytes"] = portability["text"].encode()
    current = _document("current", "01/01/2026", "31/12/2026")
    result = check_policy_history(_request(["prior_policy", "portability_certificate", "current_policy"], continuity="confirmed_with_documents"),
                                 [prior, portability, current], _Retriever())
    assert result["applicable_policy_dates"]["start"] == "2026-01-01"
    assert result["waiting_periods"][0]["months_since_current_start"] == 9
    assert result["waiting_periods"][0]["status"] == "period_may_apply"
    assert result["waiting_periods"][0]["version_id"].startswith("current_policy-")


def test_renewed_policy_selects_new_version_but_does_not_infer_continuity():
    current = _document("current", "01/01/2025", "31/12/2025")
    renewal = _document("renewal", "01/01/2026", "31/12/2026")
    result = check_policy_history(_request(["current_policy", "renewal"]), [current, renewal], _Retriever())
    assert result["applicable_policy_version"].startswith("renewal-")
    assert result["continuity_used_for_waiting_check"] is False
    assert result["waiting_periods"][0]["status"] == "continuity_unknown"
    assert any("Renewal dates by themselves" in warning for warning in result["warnings"])


def test_endorsement_is_versioned_and_waiting_clause_cites_its_document_version():
    endorsement = _document("endorsement", "01/01/2026", "31/12/2026", endorsement=True)
    result = check_policy_history(_request(["endorsement"], continuity="not_confirmed"), [endorsement], _Retriever())
    version = result["policy_versions"][0]
    assert version["document_type"] == "endorsement"
    assert version["endorsements"]
    assert result["waiting_periods"][0]["version_id"] == version["version_id"]


def test_unknown_continuity_is_reported_even_with_policy_dates():
    docs = [_document("current", "01/01/2025", "31/12/2025"), _document("renewal", "01/01/2026", "31/12/2026")]
    result = check_policy_history(_request(["current_policy", "renewal"]), docs, _Retriever())
    assert result["continuity_status"] == "unknown"
    assert not result["continuity_used_for_waiting_check"]
    assert "Portability / continuity certificate explicitly confirming continuity credits" in result["missing_historical_documents"]


def test_conflicting_effective_dates_are_identified():
    docs = [_document("one", "01/01/2025", "31/12/2025"), _document("two", "01/02/2025", "31/12/2025")]
    result = check_policy_history(_request(["current_policy", "current_policy"]), docs, _Retriever())
    assert any(conflict["kind"] == "effective_date" for conflict in result["conflicts"])


def test_missing_treatment_date_is_rejected():
    with pytest.raises(ValidationError):
        PolicyHistoryRequest(treatment="Knee replacement", documents=[{"document_type": "current_policy"}])


def test_policy_history_api_persists_version_metadata_and_rejects_missing_date():
    pdf = Path(__file__).resolve().parents[1] / "data/samples/policies/suraksha_health_gold.pdf"
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        missing = client.post("/api/policy-history/check", data={"metadata": json.dumps({
            "treatment": "Knee replacement", "documents": [{"document_type": "current_policy"}]})},
            files=[("documents", ("policy.pdf", pdf.read_bytes(), "application/pdf"))])
        assert missing.status_code == 422
        metadata = {"treatment": "Knee replacement", "treatment_date": "2026-10-01",
                    "documents": [{"document_type": "current_policy"}]}
        response = client.post("/api/policy-history/check", data={"metadata": json.dumps(metadata)},
            files=[("documents", ("policy.pdf", pdf.read_bytes(), "application/pdf"))])
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["policy_versions"][0]["sha256"]
        assert client.get(f"/api/policy-history/{data['check_id']}").json()["check_id"] == data["check_id"]
