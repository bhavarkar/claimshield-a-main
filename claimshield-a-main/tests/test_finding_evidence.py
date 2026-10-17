from claimshield.models import Bill, Clause, Finding, FindingType, LineItem, PolicyCitation, PolicyDoc
from claimshield.services.finding_evidence import attach_finding_evidence
from claimshield.services.parsing import RawDocument, normalize_bill


def _finding(policy=None):
    return Finding(finding_id="F1", type=FindingType.ARITHMETIC_ERROR, title="Rate mismatch",
                   bill_lines=[1], bill_evidence="Line 1", policy=policy,
                   explanation="Check the rate", confidence="High", impact=20,
                   counterparty="hospital", detected_by="audit_agent", method="test")


def _bill(page=2):
    return Bill(line_items=[LineItem(line_no=1, service_date="2026-01-01", description="Lab", qty=2,
                                     unit_price=100, amount=220, source_page=page,
                                     source_text="1 | 2026-01-01 | Lab | 2 | 100 | 220")], stated_gross_total=220)


def _policy():
    clause = Clause(clause_id="2.1", title="Covered expenses", text="Eligible expenses are covered.", page=3, source="policy.pdf")
    return PolicyDoc(source="policy.pdf", clauses=[clause]), PolicyCitation(clause_id="2.1", title=clause.title,
            quote=clause.text, source=clause.source, page=clause.page)


def test_correct_evidence_mapping_and_consistent_calculation():
    policy, citation = _policy()
    result = attach_finding_evidence(_finding(citation), _bill(), policy, "cases/x/bill.pdf", "cases/x/policy.pdf")
    evidence = result.evidence
    assert evidence["bill_lines"][0]["document_id"] == "cases/x/bill.pdf"
    assert evidence["bill_lines"][0]["page"] == 2
    assert evidence["bill_lines"][0]["source_text"].endswith("220")
    assert evidence["policy"]["status"] == "verified"
    assert evidence["policy"]["page"] == 3
    assert evidence["calculation"]["steps"][0]["result"] == 200
    assert evidence["calculation"]["steps"][1]["result"] == 20
    assert evidence["calculation"]["total"] == 20


def test_wrong_document_reference_is_not_verified():
    policy, citation = _policy()
    citation.source = "other-policy.pdf"
    result = attach_finding_evidence(_finding(citation), _bill(), policy, "bill.pdf", "policy.pdf")
    assert result.evidence["policy"]["status"] == "unavailable"
    assert result.evidence["status"] == "partial"


def test_missing_source_page_is_explicit():
    policy, citation = _policy()
    result = attach_finding_evidence(_finding(citation), _bill(page=None), policy, "bill.pdf", "policy.pdf")
    assert result.evidence["bill_lines"][0]["page"] is None
    assert result.evidence["bill_lines"][0]["page_status"] == "unavailable"
    assert result.evidence["status"] == "partial"


def test_missing_clause_and_old_finding_payload_are_supported():
    policy, _ = _policy()
    result = attach_finding_evidence(_finding(None), _bill(), policy, "bill.pdf", "policy.pdf")
    assert result.evidence["policy"]["status"] == "unavailable"
    old = {"finding_id": "F2", "type": "ARITHMETIC_ERROR", "title": "old", "bill_evidence": "bill",
           "explanation": "explain", "confidence": "High", "impact": 5, "counterparty": "hospital",
           "detected_by": "audit_agent", "method": "old"}
    parsed = Finding.model_validate(old)
    assert parsed.evidence is None
    assert parsed.model_dump(mode="json")["title"] == "old"


def test_parser_retains_bill_table_page_and_source_row():
    raw = RawDocument(pages=["bill", "table"], tables=[[['#', 'Description', 'Qty', 'Rate', 'Amount'],
                    ['1', 'Lab', '2', '100', '220']]], table_pages=[2])
    line = normalize_bill(raw).line_items[0]
    assert line.source_page == 2
    assert "Lab" in line.source_text and "220" in line.source_text
