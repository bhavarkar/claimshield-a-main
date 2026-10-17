"""HTTP API round trip, input validation, and the Gemini output guards."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from claimshield.api.main import app
from claimshield.models import Finding, FindingType
from claimshield.services.llm import GeminiLLM, amounts_in


def _wait(client: TestClient, case_id: str) -> dict:
    for _ in range(200):
        body = client.get(f"/api/cases/{case_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("audit did not finish")


def test_sample_audit_round_trip() -> None:
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert "BILL-001" in client.get("/api/samples").json()["bills"]
        case_id = client.post("/api/audit", data={"sample_bill": "BILL-001"}).json()["case_id"]
        body = _wait(client, case_id)
        assert body["status"] == "complete"
        assert len(body["findings"]) == 5
        assert set(body["files"]) == {"reconciliation_report.pdf", "dispute_letter_insurer.pdf", "dispute_letter_hospital.pdf"}
        pdf = client.get(body["files"]["reconciliation_report.pdf"])
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        events = client.get(f"/api/cases/{case_id}/events").text
        assert "intake_agent" in events and "event: end" in events
        assert client.get(f"/api/cases/{case_id}/letters/insurer.txt").text.startswith("To,")


def test_upload_audit_and_validation() -> None:
    from claimshield.config import PROJECT_ROOT

    samples = PROJECT_ROOT / "data" / "samples"
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        files = {
            "bill": ("my_bill.pdf", (samples / "bills" / "bill_003_lotus_knee_replacement.pdf").read_bytes(), "application/pdf"),
            "policy": ("my_policy.pdf", (samples / "policies" / "carefirst_family_silver.pdf").read_bytes(), "application/pdf"),
        }
        case_id = client.post("/api/audit", files=files).json()["case_id"]
        assert _wait(client, case_id)["reconciliation"]["recovery_likely"] == 42800

        bad = client.post("/api/audit", files={"bill": ("x.pdf", b"not a pdf", "application/pdf")}, data={"sample_policy": "POLICY-GOLD"})
        assert bad.status_code == 415
        assert client.post("/api/audit").status_code == 400
        assert client.get("/api/cases/../../etc").status_code == 404
        assert client.get("/api/samples/bills/..%2f..%2fREADME.md").status_code == 404


def _gemini(response: str) -> GeminiLLM:
    llm = object.__new__(GeminiLLM)
    llm._generate = lambda *a, **k: response  # type: ignore[attr-defined]
    llm.name = "Gemini (fake)"
    return llm


def test_gemini_letter_cannot_introduce_new_amounts() -> None:
    template = "Please refund ₹5,000 for the sub-limit."
    assert _gemini("Kindly refund ₹50,000 immediately.").draft_letter({}, template) == template
    polished = "I respectfully request a refund of ₹5,000 relating to the sub-limit."
    assert _gemini(polished).draft_letter({}, template) == polished


def test_gemini_explanation_falls_back_when_numbers_drift() -> None:
    f = Finding(finding_id="F1", type=FindingType.ARITHMETIC_ERROR, title="t", bill_evidence="Line 9: 6 x ₹180 = ₹1,080, billed ₹1,800",
                explanation="₹720 more than qty x rate.", confidence="High", impact=720, counterparty="hospital",
                detected_by="audit_agent", method="sql")
    assert _gemini("You were overcharged ₹7,200.").explain(f, None) == f.explanation  # type: ignore[arg-type]
    assert _gemini("The bill charges ₹720 more than 6 x ₹180.").explain(f, None) != f.explanation  # type: ignore[arg-type]


def test_amount_parser() -> None:
    assert amounts_in("₹1,22,530 and Rs. 450 and INR 3,000.50") == {122530, 450, 3000}
