"""Claim timeline validation, ordering, duplicate protection and draft guards."""
from fastapi.testclient import TestClient

from claimshield.api.main import app


def test_timeline_order_duplicates_missing_date_and_grievance_status():
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        case_id = client.post("/api/audit", data={"sample_bill": "BILL-001"}).json()["case_id"]
        url = f"/api/cases/{case_id}/timeline/events"
        later = {"event_type": "insurer_query", "date": "2026-05-10", "source": "Insurer email",
                 "claim_status": "query_pending", "required_documents": ["Discharge summary"]}
        earlier = {"event_type": "submission", "date": "2026-05-01", "source": "Portal receipt",
                   "claim_status": "submitted"}
        assert client.post(url, json=later).status_code == 200
        assert client.post(url, json=earlier).status_code == 200
        timeline = client.get(f"/api/cases/{case_id}/timeline").json()
        assert [event["date"] for event in timeline["events"]] == ["2026-05-01", "2026-05-10"]
        assert timeline["pending_actions"][0]["required_documents"] == ["Discharge summary"]
        assert client.post(url, json=later).status_code == 409
        missing_date = {"event_type": "submission", "source": "Receipt", "claim_status": "submitted"}
        assert client.post(url, json=missing_date).status_code == 422
        unsupported = {"event_type": "grievance", "date": "2026-05-11", "source": "Letter",
                       "claim_status": "query_pending"}
        assert client.post(url, json=unsupported).status_code == 422
        assert client.post(f"/api/cases/{case_id}/timeline/draft", json={"grievance": True}).status_code == 422
        rules = client.get(f"/api/cases/{case_id}/escalation-rules").json()
        assert rules == {"available": False, "version": None, "rules": []}
