import io
from pathlib import Path

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from claimshield.api.main import app
from claimshield.services.document_check import check_documents
from claimshield.services.parsing import LocalPdfParser

DATA = Path(__file__).resolve().parents[1] / "data"


def _pdf(text: str) -> bytes:
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer)
    y = 790
    for line in text.splitlines():
        doc.drawString(40, y, line)
        y -= 18
    doc.save()
    return buffer.getvalue()


TEXTS = {
    "bill": "Sunrise Hospital\nIN-PATIENT FINAL BILL (ITEMISED)\nPatient Name: Rohan Mehta\nBill No: B-1\nQty Amount\nGross Total Rs. 5,000",
    "discharge": "DISCHARGE SUMMARY\nPatient Name: Rohan Mehta\nAdmission Date: 2026-01-01\nDischarge Date: 2026-01-03\nDiagnosis: Appendicitis\nTreatment: Appendectomy",
    "receipt": "PAYMENT RECEIPT\nReceipt No: R-1\nAmount Received: Rs. 5,000\nPayment Date: 2026-01-03",
    "prescription": "PRESCRIPTION\nPatient: Rohan Mehta\nDoctor: Dr. Rao\nDate: 2026-01-01\nMedication: Tablet 500 mg daily",
    "investigation": "INVESTIGATION REPORT\nPatient Name: Rohan Mehta\nTest Name: CBC\nResult: Normal\nReport Date: 2026-01-01",
    "claim": "HEALTH INSURANCE CLAIM FORM\nClaimant: Rohan Mehta\nPolicy No: P-1\nClaim No: C-1\nSignature of Claimant",
    "policy": "Example Insurance\nPolicy Wordings\nPolicy Number: P-1\nPolicy Schedule\nSum Insured: Rs. 500,000",
}


def _documents(types):
    parser = LocalPdfParser()
    return [{"filename": f"arbitrary-{key}.pdf", "content": (blob := _pdf(TEXTS[key])), "raw": parser.parse(blob)} for key in types]


def test_complete_reimbursement_submission_marks_required_documents_complete():
    docs = _documents(["bill", "discharge", "receipt", "claim", "policy", "prescription", "investigation"])
    result = check_documents(docs, claim_type="reimbursement", data_dir=DATA)
    required = [item for item in result["checklist"] if item["requirement_kind"] == "required generic recommendation"]
    assert len(required) == 5
    assert all(item["status"] == "complete" for item in required)
    assert all(not item["verified"] for item in required)


def test_reimbursement_missing_receipt_is_reported():
    result = check_documents(_documents(["bill", "discharge", "claim", "policy"]), claim_type="reimbursement", data_dir=DATA)
    receipt = next(item for item in result["checklist"] if item["document_type"] == "payment_receipt")
    assert receipt["status"] == "missing"
    assert receipt["verified"] is False


def test_missing_discharge_summary_is_reported():
    result = check_documents(_documents(["bill", "receipt", "claim", "policy"]), claim_type="reimbursement", data_dir=DATA)
    discharge = next(item for item in result["checklist"] if item["document_type"] == "discharge_summary")
    assert discharge["status"] == "missing"


def test_duplicate_files_are_detected_and_counted_once():
    parser = LocalPdfParser()
    blob = _pdf(TEXTS["bill"])
    result = check_documents([{"filename": "bill1.pdf", "content": blob, "raw": parser.parse(blob)},
                              {"filename": "different-name.pdf", "content": blob, "raw": parser.parse(blob)}],
                             claim_type="default", data_dir=DATA)
    assert result["documents"][1]["status"] == "duplicate"
    assert result["documents"][1]["duplicate_of"] == "D1"


def test_document_type_uses_content_instead_of_filename():
    parser = LocalPdfParser()
    blob = _pdf(TEXTS["prescription"])
    result = check_documents([{"filename": "hospital_bill.pdf", "content": blob, "raw": parser.parse(blob)}],
                             claim_type="default", data_dir=DATA)
    assert result["documents"][0]["detected_type"] == "prescription"
    assert next(item for item in result["checklist"] if item["document_type"] == "hospital_bill")["status"] == "missing"


def test_unreadable_scanned_pdf_remains_uncertain():
    parser = LocalPdfParser()
    blob = _pdf("")
    result = check_documents([{"filename": "scan.pdf", "content": blob, "raw": parser.parse(blob)}],
                             claim_type="default", data_dir=DATA)
    assert result["documents"][0]["status"] == "uncertain"
    assert result["documents"][0]["detected_type"] is None


def test_document_check_api_accepts_multiple_pdfs_and_exports_csv():
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        response = client.post("/api/document-check", data={"claim_type": "reimbursement"}, files=[
            ("files", ("bill.pdf", _pdf(TEXTS["bill"]), "application/pdf")),
            ("files", ("receipt.pdf", _pdf(TEXTS["receipt"]), "application/pdf")),
        ])
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["documents"]) == 2
    assert "Requirement type" in result["export_csv"]
    assert result["generic_recommendations_notice"].startswith("Generic checklist")
