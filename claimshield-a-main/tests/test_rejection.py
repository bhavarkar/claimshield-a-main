import io

from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from claimshield.api.main import app
from claimshield.models import Clause, PolicyDoc
from claimshield.services.rejection import CLASSIFICATIONS, analyze_rejection


def _pdf(*lines: str) -> bytes:
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer, pagesize=letter)
    y = 760
    for line in lines:
        doc.drawString(50, y, line)
        y -= 20
    doc.save()
    return buffer.getvalue()


def _policy(*clauses: Clause) -> PolicyDoc:
    return PolicyDoc(insurer="Example Assurance", plan_name="Example Plan", clauses=list(clauses))


def test_valid_exclusion_is_supported_with_clause_quote():
    policy = _policy(Clause(clause_id="4.1", title="Cosmetic Treatment",
                            text="Expenses for cosmetic or aesthetic treatment are not payable."))
    result = analyze_rejection("Your claim is rejected because cosmetic treatment expenses are not payable.", policy)
    reason = result["reasons"][0]
    assert reason["classification"] == CLASSIFICATIONS[0]
    assert reason["policy_evidence"][0]["clause_id"] == "4.1"


def test_incorrect_exclusion_is_potentially_inconsistent_not_illegal():
    policy = _policy(Clause(clause_id="2.1", title="Knee Replacement",
                            text="Knee replacement expenses are covered and payable under this policy."))
    result = analyze_rejection("Knee replacement costs are rejected as excluded from the policy.", policy)
    assert result["reasons"][0]["classification"] == CLASSIFICATIONS[1]
    assert "lawful" in result["disclaimer"]


def test_missing_documents_are_reported_from_notice_text():
    policy = _policy(Clause(clause_id="1.1", title="Hospitalisation", text="The company will pay eligible hospital expenses."))
    result = analyze_rejection("The claim is denied pending the discharge summary and investigation report.", policy)
    assert "Discharge Summary" in result["requested_documents"]
    assert "Investigation Report" in result["requested_documents"]


def test_ambiguous_policy_wording_is_insufficient_evidence():
    policy = _policy(Clause(clause_id="3.2", title="Knee Benefit",
                            text="Knee replacement may be covered subject to applicable conditions."))
    result = analyze_rejection("Knee replacement is not payable under your policy.", policy)
    assert result["reasons"][0]["classification"] == CLASSIFICATIONS[2]


def test_missing_rejection_reason_returns_insufficient_evidence():
    result = analyze_rejection("Dear member, please contact our office regarding your claim.", PolicyDoc())
    assert result["reasons"][0]["reason"] is None
    assert result["reasons"][0]["classification"] == CLASSIFICATIONS[2]
    assert "provide the reason" in result["appeal_draft"]


def test_unsupported_ai_assertion_is_discarded():
    class FakeLLM:
        def extract_rejection_reasons(self, text):
            return [{"quote": "Claim denied because the patient used an unlicensed provider."}]

        def draft_letter(self, facts, template):
            return template

    result = analyze_rejection("Dear member, we have completed reviewing your claim.", PolicyDoc(), llm=FakeLLM())
    assert result["reasons"][0]["reason"] is None
    assert "unlicensed provider" not in str(result)


def test_rejection_analysis_api_accepts_both_pdfs():
    rejection = _pdf("Claim Rejection Notice", "Your claim is rejected under clause 4.1 because cosmetic treatment expenses are not payable.",
                     "Please provide the discharge summary.")
    policy = _pdf("Example Assurance", "Example Plan - Policy Wordings", "Section 4 - Exclusions",
                  "4.1 Cosmetic Treatment. Expenses for cosmetic or aesthetic treatment are not payable.")
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        response = client.post("/api/rejection-analysis",
                               files={"rejection_letter": ("rejection.pdf", rejection, "application/pdf"),
                                      "policy": ("policy.pdf", policy, "application/pdf")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reasons"][0]["classification"] == CLASSIFICATIONS[0], body
    assert "discharge summary" in [v.casefold() for v in body["requested_documents"]]
    assert "clause 4.1" in [v.casefold() for v in body["document_references"]]
    assert "clause 4.1" in body["appeal_draft"]
