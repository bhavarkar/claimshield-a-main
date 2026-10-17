"""Attach traceable source references and reproducible arithmetic to findings."""

from __future__ import annotations

from typing import Any

from ..models import Bill, Finding, PolicyDoc


def _calculation(finding: Finding, bill: Bill) -> dict[str, Any]:
    lines = [line for line in bill.line_items if line.line_no in finding.bill_lines]
    steps: list[dict[str, Any]] = []
    status = "verified"
    if finding.type.value == "ARITHMETIC_ERROR" and lines:
        line = lines[0]
        expected = round(line.qty * line.unit_price, 2)
        difference = round(line.amount - expected, 2)
        steps = [{"label": "Quantity × unit rate", "inputs": {"quantity": line.qty, "unit_rate": line.unit_price}, "result": expected},
                 {"label": "Billed amount − expected amount", "inputs": {"billed": line.amount, "expected": expected}, "result": difference}]
        if abs(difference - finding.impact) > 0.01: status = "inconsistent"
    elif finding.type.value == "DUPLICATE_CHARGE" and lines:
        first = lines[0]
        count = max(len(lines) - 1, 0)
        computed = round(first.amount * count, 2)
        steps = [{"label": "Repeated charge × extra occurrences", "inputs": {"charge": first.amount, "extra_occurrences": count}, "result": computed}]
        if abs(computed - finding.impact) > 0.01: status = "inconsistent"
    elif finding.type.value == "QUANTITY_MISMATCH" and lines:
        days, nights = sum(line.qty for line in lines), max(bill.nights, 1)
        rate = max(lines, key=lambda line: line.unit_price).unit_price
        computed = round(max(days - nights, 0) * rate, 2)
        steps = [{"label": "Excess billed room days × selected room rate", "inputs": {"billed_days": days, "allowed_nights": nights, "rate": rate}, "result": computed}]
        if abs(computed - finding.impact) > 0.01: status = "inconsistent"
    elif finding.type.value == "TOTAL_MISMATCH":
        total = round(sum(line.amount for line in bill.line_items), 2)
        gap = round(bill.stated_gross_total - total, 2)
        steps = [{"label": "Printed gross total − itemised line sum", "inputs": {"printed_total": bill.stated_gross_total, "line_sum": total}, "result": gap}]
        if abs(gap - finding.impact) > 0.01: status = "inconsistent"
    else:
        steps = [{"label": "Recorded finding impact", "inputs": {"finding_id": finding.finding_id}, "result": finding.impact}]
        status = "partial"
    return {"status": status, "currency": "INR", "steps": steps, "total": finding.impact}


def attach_finding_evidence(finding: Finding, bill: Bill, policy: PolicyDoc,
                            bill_document_id: str | None, policy_document_id: str | None) -> Finding:
    bill_lines = []
    for line in bill.line_items:
        if line.line_no in finding.bill_lines:
            bill_lines.append({"line_no": line.line_no, "document_id": bill_document_id,
                               "page": line.source_page,
                               "page_status": "verified" if line.source_page is not None and line.source_text else "unavailable",
                               "source_text": line.source_text or None, "description": line.description,
                               "quantity": line.qty, "unit_rate": line.unit_price, "amount": line.amount})
    citation = finding.policy
    clause = next((clause for clause in policy.clauses if citation and citation.clause_id != "?"
                   and clause.clause_id == citation.clause_id
                   and clause.text.strip() == citation.quote.strip()
                   and clause.page == citation.page and clause.title == citation.title
                   and clause.source == citation.source
                   and ((policy.document_id and citation.document_id == policy.document_id
                         and clause.document_id == policy.document_id)
                        or (not policy.document_id and not citation.document_id))), None)
    if clause and policy.version_id and citation.version_id != policy.version_id:
        clause = None
    identity = policy.document_id or policy_document_id
    policy_evidence = ({"status": "verified", "document_id": identity, "version_id": policy.version_id,
                        "source": clause.source, "clause_id": clause.clause_id, "title": clause.title,
                        "page": clause.page, "text": clause.text}
                       if clause else {"status": "unavailable", "document_id": identity,
                                      "version_id": policy.version_id,
                                      "reason": "No exact matching clause from the selected policy document and version was found."})
    calc = _calculation(finding, bill)
    pages_verified = bool(bill_lines) and all(x["page_status"] == "verified" for x in bill_lines)
    if not finding.bill_lines:
        pages_verified = True
    overall = "verified" if pages_verified and policy_evidence["status"] == "verified" and calc["status"] == "verified" else "partial"
    if not bill_lines and finding.bill_lines:
        overall = "insufficient"
    return finding.model_copy(update={"evidence": {"status": overall, "bill_document_id": bill_document_id,
        "bill_lines": bill_lines, "policy": policy_evidence, "calculation": calc, "confidence": finding.confidence}})
