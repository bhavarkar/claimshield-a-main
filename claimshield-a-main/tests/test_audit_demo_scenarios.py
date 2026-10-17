"""Demo-ready audit scenarios for hackathon judging.

ALL prices, policies, regulatory text, source citations and reference rates here
are SYNTHETIC TEST FIXTURES ONLY. Not real IRDAI/CGHS/PM-JAY/NPPA data.

Scenarios:
  Test 1: clean claim - zero discrepancy, all clear/info
  Test 2: flagged claim - amounts, provenance, confidence, explanation
  Test 3: ambiguous claim - needs_review, zero discrepancy
  Test 4: missing authoritative source - pending rule never activates
  Test 5: snapshot endpoint - admin-only, no reference amounts
"""
from datetime import date
from io import BytesIO
import pytest
from fastapi.testclient import TestClient
from claimshield.api.main import app
from claimshield.domain.healthcare_audit import HealthcareAuditInput, audit_healthcare
from claimshield.models import Bill, Clause, LineItem, PolicyCitation, PolicyDoc, PolicyTerms
from claimshield.services import get_services
from claimshield.services.healthcare_audit import run_healthcare_audit
from claimshield.services.healthcare_references import (
    HealthcareRate, HealthcareReferenceRegistry, ReferenceImport, ReferenceReview, RoomAdjustmentRule,
)
from claimshield.services.regulatory import (
    ApplicabilityRequest, DocumentReview, IRDAI_HEALTH_CIRCULARS,
    RegulatoryItemRule, RegulatoryRegistry, RegulatoryRule,
    ReviewStatus, RuleApplicability, RuleReview,
)

_EVENT = date(2026, 3, 1)
_POLICY_ID = "demo-synthetic-policy"
_VERSION = "demo-synthetic-v1"
_ROOM_LIMIT = 3000
_CGHS_LAB_RATE = 120
_CGHS_CONSULT_RATE = 400


def _line(n, description, category, price, qty=1, unit="per_item", **kw):
    return LineItem(line_no=n, service_date=_EVENT.isoformat(), description=description,
                    category=category, qty=qty, unit_price=price, amount=price * qty, unit=unit, **kw)


def _policy_doc(limit=_ROOM_LIMIT):
    room_clause = Clause(
        clause_id="D-R1", title="SYNTHETIC Daily room limit",
        text=f"SYNTHETIC FIXTURE: normal room charges are limited to Rs. {limit} per day. "
             f"Charges in excess of the daily limit are borne by the insured.",
        source="synthetic-demo-policy.pdf", page=4,
        document_id=_POLICY_ID, version_id=_VERSION,
    )
    return PolicyDoc(source="synthetic-demo-policy.pdf", document_id=_POLICY_ID,
                     version_id=_VERSION, clauses=[room_clause])


def _policy_terms(limit=_ROOM_LIMIT, uncertain=()):
    return PolicyTerms(
        sum_insured=300000, room_rent_limit=limit, room_rent_limit_type="per_day",
        clause_refs={"room_rent": "D-R1"}, uncertain_topics=list(uncertain),
    )


def _audit_input(lines, limit=_ROOM_LIMIT, uncertain=()):
    return HealthcareAuditInput(
        claim_id="demo-claim-001",
        bill=Bill(line_items=lines, region="demo-city",
                  stated_gross_total=sum(l.amount for l in lines)),
        policy_doc=_policy_doc(limit),
        policy_terms=_policy_terms(limit, uncertain),
        context=ApplicabilityRequest(insurance_category="health", event_date=_EVENT,
                                     claim_context="hospitalization"),
    )


def _rate(record_id, source_type, item_name, category, unit, reference_amount, **kw):
    return HealthcareRate(
        record_id=record_id, source_name="SYNTHETIC TEST SOURCE ONLY",
        source_type=source_type,
        source_url="https://example.invalid/synthetic-fixture",
        effective_date=date(2026, 1, 1), version="demo-synthetic-v1",
        notes="SYNTHETIC TEST FIXTURE not a real rate schedule",
        item_name=item_name, category=category, unit=unit,
        reference_amount=reference_amount, geography="demo-city", **kw,
    )


def _approve_rate(registry, record_id):
    registry.review("rates", record_id, ReferenceReview(
        verification_status="approved", enforcement_enabled=True,
        currentness_checked=True, source_checked=True, interpretation_checked=True,
        explanation="SYNTHETIC TEST ONLY: fixture approval"), "demo-admin")


def _synthetic_pdf(quote):
    from reportlab.pdfgen import canvas
    stream = BytesIO()
    c = canvas.Canvas(stream)
    c.drawString(36, 720, quote)
    c.save()
    return stream.getvalue()


def _setup_irdai_rule(approve=True):
    quote = "SYNTHETIC DEMO FIXTURE: Admission kit and toiletries are non-payable items."
    registry = RegulatoryRegistry(get_services().storage)
    doc_id = "irdai-health-master-2024-en"
    registry.ingest(doc_id, IRDAI_HEALTH_CIRCULARS, _synthetic_pdf(quote))
    registry.review_document(doc_id, DocumentReview(
        human_review_status="approved", currentness_checked=True,
        reviewer_explanation="SYNTHETIC TEST SOURCE ONLY"), reviewer_id="demo-admin")
    registry.create_candidate(RegulatoryRule(
        rule_id="demo-non-payable", rule_title="SYNTHETIC non-payable admission kit rule",
        source_document_id=doc_id, source_citation="SYNTHETIC demo fixture page 1",
        source_page=1, source_quote=quote,
        effective_date=date(2026, 1, 1),
        applicability=RuleApplicability(
            insurance_categories=["health"], claim_contexts=["hospitalization"]),
        item_rule=RegulatoryItemRule(
            canonical_description="Admission kit", aliases=["Admission kit and toiletries"],
            categories=["NON_MEDICAL"], effect="non_payable"),
    ))
    if approve:
        registry.review_rule("demo-non-payable", RuleReview(
            human_review_status="approved", enforcement_enabled=True,
            reviewer_explanation="SYNTHETIC TEST ONLY: demo fixture review"), reviewer_id="demo-admin")
    return registry


def _fof(result, finding_type):
    return [f for f in result["findings"] if f["type"] == finding_type]


def test_scenario_1_clean_claim_zero_discrepancy_and_no_flagged_findings():
    """Test 1 (Clean claim): within-limit, within-benchmark -> zero discrepancy, no flagged."""
    reg = HealthcareReferenceRegistry(get_services().storage)
    reg.import_records(ReferenceImport(rates=[
        _rate("s1-cbc", "CGHS", "Complete Blood Count", "LAB", "per_test", _CGHS_LAB_RATE),
        _rate("s1-con", "CGHS", "Outpatient consultation", "CONSULT", "per_visit", _CGHS_CONSULT_RATE),
    ]))
    _approve_rate(reg, "s1-cbc")
    _approve_rate(reg, "s1-con")
    lines = [
        _line(1, "Room charges General ward", "ROOM", _ROOM_LIMIT, qty=3, unit="per_day"),
        _line(2, "Complete Blood Count", "LAB", _CGHS_LAB_RATE, unit="per_test"),
        _line(3, "Outpatient consultation", "CONSULT", 350, unit="per_visit"),
        _line(4, "IV fluids 500 ml", "CONSUMABLE", 80),
    ]
    result = run_healthcare_audit(_audit_input(lines), get_services())
    assert result["potential_discrepancy"] == 0, f"Clean claim should have zero discrepancy, got {result['potential_discrepancy']}"
    assert result["finding_counts"]["flagged"] == 0, "Clean claim: no flagged findings expected"
    assert all(f["status"] == "clear" for f in _fof(result, "ROOM_RENT_LIMIT"))
    for f in result["findings"]:
        assert "fraud" not in f["explanation"].lower(), "Audit must not use the word fraud"
    assert "total_billed" in result
    assert result["summary_basis"]


def test_scenario_2_flagged_claim_has_amounts_provenance_confidence_and_explanation():
    """Test 2 (Flagged claim): room above limit + CGHS variance + non-payable -> structured findings."""
    reg = HealthcareReferenceRegistry(get_services().storage)
    reg.import_records(ReferenceImport(rates=[
        _rate("s2-cbc", "CGHS", "Complete Blood Count", "LAB", "per_test", _CGHS_LAB_RATE),
    ]))
    _approve_rate(reg, "s2-cbc")
    _setup_irdai_rule(approve=True)
    room_daily = 5000
    stay_days = 3
    room_excess = (room_daily - _ROOM_LIMIT) * stay_days
    lab_billed = 200
    kit_amt = 350
    lines = [
        _line(1, "Room Single AC", "ROOM", room_daily, qty=stay_days, unit="per_day"),
        _line(2, "Complete Blood Count", "LAB", lab_billed, unit="per_test"),
        _line(3, "Admission kit", "NON_MEDICAL", kit_amt),
    ]
    result = run_healthcare_audit(_audit_input(lines), get_services())
    assert result["potential_discrepancy"] == room_excess + kit_amt
    assert result["benchmark_variance"] == lab_billed - _CGHS_LAB_RATE
    flagged = [f for f in result["findings"] if f["status"] == "flagged"]
    assert len(flagged) >= 3, f"Expected >=3 flagged, got {len(flagged)}"
    for f in flagged:
        assert f["explanation"], f"Finding {f['finding_id']} missing explanation"
        assert "fraud" not in f["explanation"].lower()
        src = (f.get("evidence") or {}).get("source") or {}
        assert src.get("source_name"), f"Finding {f['finding_id']} missing source provenance"
        assert f["confidence_score"] >= 0.95, f"Confidence {f['confidence_score']} < 0.95"
    room_f = [f for f in _fof(result, "ROOM_RENT_LIMIT") if f["status"] == "flagged"]
    assert any(f["potential_difference"] == room_excess for f in room_f)
    nonpay = [f for f in _fof(result, "NON_PAYABLE_ITEM") if f["status"] == "flagged"]
    assert nonpay and nonpay[0]["potential_difference"] == kit_amt


def test_scenario_3_ambiguous_claim_is_needs_review_not_deterministic():
    """Test 3 (Ambiguous claim): uncertain policy + fuzzy match -> needs_review, zero discrepancy."""
    reg = HealthcareReferenceRegistry(get_services().storage)
    reg.import_records(ReferenceImport(rates=[
        _rate("s3-cbc", "CGHS", "CBC", "LAB", "per_test", _CGHS_LAB_RATE),
    ]))
    _approve_rate(reg, "s3-cbc")
    lines = [
        _line(1, "Room Deluxe ward", "ROOM", 4500, qty=2, unit="per_day"),
        _line(2, "Haematology panel with differentials", "LAB", 250, unit="per_test"),
        _line(3, "Personal hygiene supplies", "NON_MEDICAL", 200),
    ]
    result = run_healthcare_audit(_audit_input(lines, uncertain=("room_rent",)), get_services())
    assert result["potential_discrepancy"] == 0, f"Ambiguous claim must have zero discrepancy, got {result['potential_discrepancy']}"
    flagged = [f for f in result["findings"] if f["status"] == "flagged"]
    assert not flagged, f"Ambiguous claim must have no flagged findings; got {[f['type'] for f in flagged]}"
    room_f = _fof(result, "ROOM_RENT_LIMIT") + _fof(result, "ROOM_RENT_PROPORTIONATE")
    assert all(f["status"] == "needs_review" for f in room_f), "Uncertain room must be needs_review"
    for f in _fof(result, "HEALTHCARE_BENCHMARK"):
        if f.get("match_status") == "possible_match":
            assert f["potential_difference"] is None, "Fuzzy benchmark match must not compute a price difference"


def test_scenario_4_pending_regulatory_rule_never_activates_and_revocation_deactivates():
    """Test 4 (Missing authoritative source): unreviewed rule -> needs_review; revocation removes it."""
    _setup_irdai_rule(approve=False)
    lines = [_line(1, "Admission kit", "NON_MEDICAL", 350)]
    result = run_healthcare_audit(_audit_input(lines), get_services())
    nonpay = _fof(result, "NON_PAYABLE_ITEM")
    assert nonpay, "NON_PAYABLE_ITEM finding emitted even for pending rule (as needs_review)"
    assert all(f["status"] == "needs_review" for f in nonpay), "Pending rule must not produce flagged"
    assert all(f["potential_difference"] is None for f in nonpay), "Pending rule must not compute deduction"
    assert result["potential_discrepancy"] == 0
    # Now approve rule, then revoke the source document — rule must become inactive
    registry = RegulatoryRegistry(get_services().storage)
    registry.review_rule("demo-non-payable", RuleReview(
        human_review_status="approved", enforcement_enabled=True,
        reviewer_explanation="SYNTHETIC approval for revocation test"), reviewer_id="demo-admin")
    registry.review_document("irdai-health-master-2024-en", DocumentReview(
        human_review_status="needs_update",
        reviewer_explanation="Revoked: source currentness no longer confirmed"), reviewer_id="demo-admin")
    result2 = run_healthcare_audit(_audit_input(lines), get_services())
    nonpay2 = _fof(result2, "NON_PAYABLE_ITEM")
    assert all(f["status"] == "needs_review" for f in nonpay2), "Revoked source must deactivate rule"
    assert result2["potential_discrepancy"] == 0


def test_scenario_5_snapshot_endpoint_admin_only_and_omits_reference_amounts(monkeypatch):
    """Test 5 (Snapshot endpoint): admin-only, returns metadata without reference amounts."""
    reg = HealthcareReferenceRegistry(get_services().storage)
    reg.import_records(ReferenceImport(rates=[
        _rate("snap-cghs", "CGHS", "Snap CBC", "LAB", "per_test", _CGHS_LAB_RATE),
    ]))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50030),
                    headers={"X-ClaimShield-Request": "1"}) as client:
        assert client.get("/api/admin/healthcare-references/snapshot").status_code == 403
        monkeypatch.setenv("CLAIMSHIELD_REGULATORY_ADMIN_SUBJECTS", "local-demo")
        resp = client.get("/api/admin/healthcare-references/snapshot")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "rates" in body and "room_rules" in body and "counts" in body
        assert body["counts"]["rates"] >= 1
        for record in body["rates"]:
            assert "reference_amount" not in record, "Snapshot must not expose reference amounts"
        assert body["rates"][0]["record_id"] == "snap-cghs"
        assert body["rates"][0]["verification_status"] == "pending_human_review"
        assert not body["rates"][0]["enforcement_enabled"]
        assert "notice" in body
