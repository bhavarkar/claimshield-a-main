"""Reconciliation report and dispute-letter PDFs (ReportLab). Amounts use 'Rs.' because the
built-in PDF fonts have no rupee glyph."""

from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..models import FINDING_LABELS, Bill, Finding, Letter, PolicyDoc, PolicyTerms, Reconciliation
from ..money import rs

_S = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_S["Heading1"], fontSize=16, spaceAfter=2, textColor=colors.HexColor("#0b3b5c"))
H2 = ParagraphStyle("h2", parent=_S["Heading2"], fontSize=11.5, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#0b3b5c"))
BODY = ParagraphStyle("body", parent=_S["BodyText"], fontSize=9, leading=12.5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.8, leading=10, textColor=colors.HexColor("#555555"))
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10)
QUOTE = ParagraphStyle("quote", parent=BODY, fontSize=8.3, leading=11, leftIndent=8, textColor=colors.HexColor("#333333"),
                       borderPadding=(3, 3, 3, 6), backColor=colors.HexColor("#f4f6f8"))
LETTER = ParagraphStyle("letter", parent=BODY, fontSize=10, leading=14.5)

TEAL = colors.HexColor("#0f766e")
AMBER = colors.HexColor("#b45309")


def p(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(escape(text).replace("₹", "Rs. ").replace("\n", "<br/>"), style)


def _footer(canvas, doc):
    canvas.setFont("Helvetica", 7.2)
    canvas.setFillColor(colors.grey)
    canvas.drawString(15 * mm, 10 * mm, "ClaimShield AI - flags items worth disputing. Not legal advice and not an allegation of fraud.")
    canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")


def _grid(t: Table, header: bool = True) -> Table:
    style = [("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c8d0d8")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("FONT", (0, 0), (-1, -1), "Helvetica", 8)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6eef5")), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8)]
    t.setStyle(TableStyle(style))
    return t


def report_pdf(case_id: str, bill: Bill, policy: PolicyDoc, findings: list[Finding], rec: Reconciliation,
               stack: dict[str, str], terms: PolicyTerms | None = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"ClaimShield reconciliation report {case_id}", author="ClaimShield AI")
    story = [
        p("ClaimShield AI - Claim Reconciliation Report", H1),
        p(f"Case {case_id} | generated {date.today().isoformat()}", SMALL),
        p(f"Policy document SHA-256: {policy.document_id or 'unavailable'} | selected version: {policy.version_id or 'unavailable'}", SMALL),
        Spacer(1, 6),
    ]
    if terms and terms.uncertain_topics:
        story += [p("Policy wording needs review", H2),
                  p("These extracted topics contain ambiguous wording and should not be treated as confirmed terms without review: "
                    + ", ".join(terms.uncertain_topics), SMALL)]
    info = [
        ["Patient", bill.patient_name, "Hospital", bill.hospital],
        ["Bill no.", bill.bill_number, "Stay", f"{bill.admission_date} to {bill.discharge_date}"],
        ["Procedure", bill.procedure, "Policy", f"{policy.plan_name} ({bill.policy_number or policy.policy_number})"],
    ]
    story.append(_grid(Table([[p(c, CELL) for c in row] for row in info], colWidths=[22 * mm, 68 * mm, 22 * mm, 68 * mm]), header=False))

    story.append(p("Summary", H2))
    summary = [
        ["", "Amount"],
        ["Total billed by hospital", rs(rec.billed_gross)],
        ["Paid by insurer", rs(rec.insurer_paid)],
        ["Paid by you", rs(rec.patient_paid)],
        ["Hospital overcharges (high confidence)", rs(rec.hospital_overcharge_high)],
        ["Insurer shortfall vs policy (high confidence)", rs(rec.insurer_shortfall)],
        ["What you should owe under the policy", rs(rec.fair_patient_payable_high)],
        ["Likely recovery for you", rs(rec.recovery_likely)],
        ["Upper bound incl. 'worth reviewing' items", rs(rec.recovery_upper)],
    ]
    t = _grid(Table(summary, colWidths=[120 * mm, 60 * mm]))
    t.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONT", (0, 7), (-1, 7), "Helvetica-Bold", 9),
                           ("TEXTCOLOR", (0, 7), (-1, 7), TEAL)]))
    story.append(t)

    e = rec.entitlement_fair
    story.append(p("How the policy amount was calculated (after high-confidence corrections)", H2))
    calc = [
        ["Step", "Amount"],
        ["Corrected bill", rs(e.gross)],
        ["Less: items the policy does not pay for", rs(e.non_payable)],
        ["Less: room rent above the policy limit", rs(e.room_excess)],
        ["Admissible amount", rs(e.admissible)],
        ["After sub-limit" + (f" (cap {rs(e.sub_limit_applied)})" if e.sub_limit_applied is not None else " (none applies)"), rs(e.after_sub_limit)],
        [f"Less: co-payment at {e.copay_pct:g}%", rs(e.copay)],
        ["Insurer should pay", rs(e.insurer_payable)],
        ["You should pay", rs(e.patient_payable)],
    ]
    t = _grid(Table(calc, colWidths=[120 * mm, 60 * mm]))
    t.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT")]))
    story.append(t)
    for note in rec.notes:
        story.append(p(f"Note: {note}", SMALL))

    story.append(p(f"Findings ({len(findings)})", H2))
    if not findings:
        story.append(p("No discrepancies found. The itemised bill adds up, no charge stood out against reference pricing, "
                       "and the insurer's settlement matches the policy terms."))
    else:
        rows = [["ID", "Finding", "Lines", "Confidence", "Amount"]]
        for f in findings:
            rows.append([f.finding_id, p(f"{FINDING_LABELS[f.type]}: {f.title}", CELL), ", ".join(map(str, f.bill_lines)) or "-",
                         f.confidence, rs(f.impact)])
        t = _grid(Table(rows, colWidths=[11 * mm, 104 * mm, 16 * mm, 25 * mm, 24 * mm], repeatRows=1))
        t.setStyle(TableStyle([("ALIGN", (4, 1), (4, -1), "RIGHT")]))
        story.append(t)
        for f in findings:
            colour = TEAL if f.confidence == "High" else AMBER
            block = [
                Spacer(1, 6),
                Paragraph(f'<font color="{colour.hexval()}"><b>{f.finding_id} | {escape(f.confidence)}</b></font> '
                          f'<b>{escape(f.title)}</b> - {rs(f.impact)}', BODY),
                p(f"Bill evidence: {f.bill_evidence}", CELL),
                p(f.explanation, CELL),
            ]
            if f.policy:
                block.append(p(f"Policy clause {f.policy.clause_id} {f.policy.title} ({f.policy.source}, p.{f.policy.page}; document {f.policy.document_id or 'unavailable'}; version {f.policy.version_id or 'unavailable'}): \"{f.policy.quote}\"", QUOTE))
            block.append(p(f"Detected by {f.detected_by} via {f.method}. Raise with: {f.counterparty}.", SMALL))
            story.append(KeepTogether(block))

    story.append(p("How this report was produced", H2))
    story.append(p("; ".join(f"{k}: {v}" for k, v in stack.items()), SMALL))
    story.append(p("Amounts flagged 'High' follow directly from the bill's own figures or the policy wording. 'Worth reviewing' items "
                   "may have a legitimate explanation and are phrased as questions. This report is not legal advice and does not allege fraud.", SMALL))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def letter_pdf(letter: Letter, bill: Bill) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=20 * mm, bottomMargin=18 * mm,
                            title=letter.subject, author=bill.patient_name or "Policyholder")
    story = [
        p(f"Date: {date.today().strftime('%d %B %Y')}", LETTER), Spacer(1, 8),
        p(f"To,\n{letter.to}", LETTER), Spacer(1, 10),
        Paragraph(f"<b>Subject: {escape(letter.subject)}</b>", LETTER), Spacer(1, 10),
    ]
    for para in letter.body.split("\n\n"):
        story += [p(para.strip(), LETTER), Spacer(1, 6)]
    doc.build(story)
    return buf.getvalue()
