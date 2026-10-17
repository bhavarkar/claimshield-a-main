from datetime import date
from io import BytesIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claimshield.api import main
from claimshield.api.auth import Principal
from claimshield.services.regulatory import (
    ApplicabilityRequest,
    DocumentReview,
    IRDAI_HEALTH_CIRCULARS,
    RegulatoryDocument,
    RegulatoryRegistry,
    RegulatoryRule,
    ReviewStatus,
    RuleApplicability,
    RuleClassification,
    RuleReview,
    MAX_REGULATORY_PDF_BYTES,
    evaluate_applicability,
)
from claimshield.services.storage import LocalStorage


DOC_ID = "irdai-health-master-2024-en"
QUOTE = "A synthetic source quotation used only in this test."


def _pdf(text=QUOTE):
    from reportlab.pdfgen import canvas
    stream = BytesIO()
    page = canvas.Canvas(stream)
    page.drawString(72, 720, text)
    page.save()
    return stream.getvalue()


def _registry(tmp_path):
    return RegulatoryRegistry(LocalStorage(tmp_path / "storage"))


def _import_and_approve_document(registry):
    doc = registry.ingest(DOC_ID, IRDAI_HEALTH_CIRCULARS, _pdf())
    assert doc.human_review_status == ReviewStatus.PENDING
    return registry.review_document(DOC_ID, DocumentReview(
        human_review_status=ReviewStatus.APPROVED, currentness_checked=True,
        reviewer_explanation="Synthetic test administrator reviewed the source and amendment check."),
        reviewer_id="test-admin")


def _rule(rule_id="test-rule", **updates):
    base = dict(rule_id=rule_id, rule_title="Synthetic test rule", source_document_id=DOC_ID,
                source_citation="Synthetic citation; test fixture only", source_page=1,
                source_paragraph="P-test", source_quote=QUOTE,
                applicability=RuleApplicability(insurance_categories=["health"],
                                                claim_contexts=["hospitalization"]),
                effective_date=date(2024, 5, 29), classification=RuleClassification.CLAIM_HANDLING,
                human_review_status=ReviewStatus.APPROVED, enforcement_enabled=True)
    base.update(updates)
    return RegulatoryRule(**base)


def _context(event_date=date(2025, 1, 1), **updates):
    base = dict(insurance_category="health", event_date=event_date,
                claim_context="hospitalization", policy_type="individual")
    base.update(updates)
    return ApplicabilityRequest(**base)


def test_document_hashing_duplicate_ingestion_is_idempotent_and_preserves_original(tmp_path):
    registry = _registry(tmp_path)
    pdf = _pdf()
    first = registry.ingest(DOC_ID, IRDAI_HEALTH_CIRCULARS, pdf)
    second = registry.ingest(DOC_ID, IRDAI_HEALTH_CIRCULARS, pdf)
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
    assert registry.storage.get(first.original_pdf_ref) == pdf
    assert registry.original_pdf(DOC_ID) == pdf
    assert registry.evidence(DOC_ID, 1)["text"] == QUOTE
    with pytest.raises(HTTPException) as conflict:
        registry.ingest(DOC_ID, IRDAI_HEALTH_CIRCULARS, _pdf("Different valid PDF text"))
    assert conflict.value.status_code == 409


def test_source_url_must_match_official_allowlisted_record(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(HTTPException) as bad_host:
        registry.ingest(DOC_ID, "https://example.com/fake.pdf", _pdf())
    assert bad_host.value.status_code == 400
    with pytest.raises(HTTPException) as bad_allowlist:
        registry.ingest("unknown-doc", IRDAI_HEALTH_CIRCULARS, _pdf())
    assert bad_allowlist.value.status_code == 404


def test_upload_requires_complete_pdf_and_enforces_size_limit(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(HTTPException) as malformed:
        registry.preview(DOC_ID, IRDAI_HEALTH_CIRCULARS, b"%PDF-1.7 not a complete PDF")
    assert malformed.value.status_code == 400
    with pytest.raises(HTTPException) as oversized:
        registry.preview(DOC_ID, IRDAI_HEALTH_CIRCULARS, b"%PDF-" + b"0" * MAX_REGULATORY_PDF_BYTES)
    assert oversized.value.status_code == 413


def test_preview_extracts_each_page_and_does_not_import_or_authenticate_source(tmp_path):
    registry = _registry(tmp_path)
    uploaded = _pdf("Synthetic page one evidence")
    preview = registry.preview(DOC_ID, IRDAI_HEALTH_CIRCULARS, uploaded)
    assert preview["page_count"] == 1
    assert "Synthetic page one evidence" in preview["pages"][0]
    assert preview["sha256"]
    assert "does not authenticate" in preview["notice"]
    assert registry.get_document(DOC_ID).sha256 is None


def test_preview_rejects_unlisted_document_and_noncanonical_source(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(HTTPException) as wrong_id:
        registry.preview("unlisted", IRDAI_HEALTH_CIRCULARS, _pdf())
    assert wrong_id.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_source:
        registry.preview(DOC_ID, "https://irdai.gov.in.evil.example/circulars?filterDepartment=HLT&filtersApplied=true", _pdf())
    assert wrong_source.value.status_code == 400


def test_document_approval_requires_pdf_import_and_currentness_review(tmp_path):
    registry = _registry(tmp_path)
    review = DocumentReview(human_review_status=ReviewStatus.APPROVED,
                            currentness_checked=False, reviewer_explanation="Checked source")
    with pytest.raises(HTTPException) as missing_pdf:
        registry.review_document(DOC_ID, review)
    assert missing_pdf.value.status_code == 409
    registry.ingest(DOC_ID, IRDAI_HEALTH_CIRCULARS, _pdf())
    with pytest.raises(HTTPException) as missing_currentness:
        registry.review_document(DOC_ID, review)
    assert missing_currentness.value.status_code == 409


def test_exact_citation_is_required_for_candidate_rule(tmp_path):
    registry = _registry(tmp_path)
    _import_and_approve_document(registry)
    with pytest.raises(HTTPException) as wrong_quote:
        registry.create_candidate(_rule(source_quote="not in source"))
    assert wrong_quote.value.status_code == 400
    with pytest.raises(HTTPException) as wrong_page:
        registry.create_candidate(_rule(source_page=2))
    assert wrong_page.value.status_code == 400
    with pytest.raises(HTTPException) as wrong_document:
        registry.create_candidate(_rule(source_document_id="another-document"))
    assert wrong_document.value.status_code == 404
    candidate = registry.create_candidate(_rule(human_review_status=ReviewStatus.APPROVED,
                                                enforcement_enabled=True))
    assert candidate.human_review_status == ReviewStatus.PENDING
    assert not candidate.enforcement_enabled


def test_rule_without_effective_date_cannot_be_approved(tmp_path):
    registry = _registry(tmp_path)
    _import_and_approve_document(registry)
    registry.create_candidate(_rule(effective_date=None))
    with pytest.raises(HTTPException) as missing_date:
        registry.review_rule("test-rule", RuleReview(human_review_status=ReviewStatus.APPROVED,
            enforcement_enabled=True, reviewer_explanation="Reviewed synthetic rule."))
    assert missing_date.value.status_code == 409


def test_historical_event_uses_pre_amendment_rule_and_later_event_marks_it_superseded():
    old = _rule("old", effective_date=date(2024, 1, 1))
    amendment = _rule("amendment", effective_date=date(2025, 1, 1), supersedes=["old"])
    before = evaluate_applicability(old, _context(date(2024, 12, 31)), [old, amendment])
    after = evaluate_applicability(old, _context(date(2025, 1, 1)), [old, amendment])
    current = evaluate_applicability(amendment, _context(date(2025, 1, 1)), [old, amendment])
    assert before[0] == "Applicable"
    assert after[0] == "Superseded"
    assert current[0] == "Applicable"
    future = _rule("future", effective_date=date(2026, 1, 1))
    assert evaluate_applicability(future, _context(date(2025, 1, 1)))[0] == "Not applicable"


def test_missing_rule_effective_date_or_applicability_context_is_undetermined():
    missing_date = _rule(effective_date=None)
    assert evaluate_applicability(missing_date, _context())[0] == "Undetermined"
    assert evaluate_applicability(_rule(), _context(insurance_category=None))[0] == "Undetermined"
    assert evaluate_applicability(_rule(), _context(event_date=None))[0] == "Undetermined"
    assert evaluate_applicability(_rule(), _context(claim_context=None))[0] == "Undetermined"


def test_conflicting_amendments_are_undetermined_and_unreviewed_candidate_never_applies():
    first = _rule("first", conflicts_with=["second"])
    second = _rule("second")
    assert evaluate_applicability(first, _context(), [first, second])[0] == "Undetermined"
    candidate = _rule("candidate", human_review_status=ReviewStatus.PENDING, enforcement_enabled=False)
    status, reasons = evaluate_applicability(candidate, _context())
    assert status == "Undetermined" and "human-reviewed" in reasons[0]


def test_expired_rule_is_superseded():
    expired = _rule(expiry_date=date(2024, 12, 31))
    assert evaluate_applicability(expired, _context(date(2025, 1, 1)))[0] == "Superseded"


def test_regulatory_applicability_api_is_not_a_violation_determination():
    storage = main.get_services().storage
    registry = RegulatoryRegistry(storage)
    doc = registry.ingest(DOC_ID, IRDAI_HEALTH_CIRCULARS, _pdf())
    assert doc.sha256
    # The seeded source remains pending: no synthetic test text is exposed as a real regulation.
    with TestClient(main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/regulatory/documents")
        assert response.status_code == 200
        assert all(item["human_review_status"] != "approved" for item in response.json()["documents"])


def test_unauthorized_regulatory_modification_is_forbidden(monkeypatch):
    monkeypatch.delenv("CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS", raising=False)
    body = {"human_review_status": "approved", "currentness_checked": True,
            "reviewer_explanation": "Should not be authorized"}
    with TestClient(main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        response = client.post(f"/api/admin/regulatory/documents/{DOC_ID}/review", json=body)
    assert response.status_code == 403


def test_applicability_response_carries_document_and_rule_citation(tmp_path):
    registry = _registry(tmp_path)
    doc = _import_and_approve_document(registry)
    registry.create_candidate(_rule())
    reviewed = registry.review_rule("test-rule", RuleReview(human_review_status=ReviewStatus.APPROVED,
        enforcement_enabled=True, reviewer_explanation="Synthetic human review complete."), reviewer_id="test-admin")
    assert reviewed.reviewed_by == "test-admin" and reviewed.reviewed_at == date.today()
    result = registry.applicability("test-rule", _context())
    assert result["status"] == "Applicable"
    assert result["source_sha256"] == doc.sha256
    assert result["source_citation"] == "Synthetic citation; test fixture only"
    assert result["assessment_kind"] == "applicability_only_not_a_violation_determination"
