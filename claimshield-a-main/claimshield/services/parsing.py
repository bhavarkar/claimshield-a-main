"""Document parsing: PDF -> normalised Bill / PolicyDoc.

Two backends produce the same raw shape (header key/values, tables, page texts):
  * DocumentAIParser - Document AI Form Parser (tables + key/value pairs)
  * LocalPdfParser   - pdfplumber, for local development without credentials

`normalize_bill` / `normalize_policy` then turn the raw shape into models. If the raw shape
doesn't look like an itemised bill (an unfamiliar layout) and Gemini is enabled, the intake
agent falls back to Gemini structured extraction (see llm.GeminiLLM.extract_bill).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..money import parse_amount
from ..models import Bill, Clause, Deduction, LineItem, PolicyDoc


class ParseError(Exception):
    pass


@dataclass
class RawDocument:
    pages: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    table_pages: list[int] = field(default_factory=list)
    parser: str = ""
    document_id: str | None = None


# ---------------------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------------------


class LocalPdfParser:
    name = "pdfplumber (local)"

    def parse(self, pdf_bytes: bytes) -> RawDocument:
        import io

        import pdfplumber

        raw = RawDocument(parser=self.name, document_id=policy_document_identity(pdf_bytes))
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                # Drop rotated and oversized glyphs: diagonal watermarks otherwise interleave with body text.
                clean = page.filter(
                    lambda o: o.get("object_type") != "char" or (o.get("upright", True) and o.get("size", 0) < 20)
                )
                raw.pages.append(clean.extract_text() or "")
                for table in clean.extract_tables():
                    raw.tables.append([[(cell or "").strip() for cell in row] for row in table])
                    raw.table_pages.append(page_no)
        return raw


class DocumentAIParser:
    name = "Document AI Form Parser"

    def __init__(self, project: str, location: str, processor_id: str) -> None:
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai

        self._documentai = documentai
        self._client = documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
        )
        self._processor = self._client.processor_path(project, location, processor_id)

    def parse(self, pdf_bytes: bytes) -> RawDocument:
        documentai = self._documentai
        result = self._client.process_document(
            request=documentai.ProcessRequest(
                name=self._processor,
                raw_document=documentai.RawDocument(content=pdf_bytes, mime_type="application/pdf"),
            )
        )
        doc = result.document
        text = doc.text

        def anchor_text(layout) -> str:
            return "".join(
                text[int(seg.start_index): int(seg.end_index)] for seg in layout.text_anchor.text_segments
            ).strip()

        raw = RawDocument(parser=self.name, document_id=policy_document_identity(pdf_bytes))
        for page_no, page in enumerate(doc.pages, start=1):
            raw.pages.append(anchor_text(page.layout))
            for table in page.tables:
                rows = [[anchor_text(c.layout) for c in r.cells] for r in table.header_rows]
                rows += [[anchor_text(c.layout) for c in r.cells] for r in table.body_rows]
                raw.tables.append(rows)
                raw.table_pages.append(page_no)
            # Form Parser returns header key/values as form fields; expose them as a 2-col table
            # so normalize_bill reads them the same way it reads the local header grid.
            kv = [[anchor_text(f.field_name.layout).rstrip(":"), anchor_text(f.field_value.layout)] for f in page.form_fields]
            if kv:
                raw.tables.insert(0, kv)
                raw.table_pages.insert(0, page_no)
        return raw


# ---------------------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------------------

DEPT_CATEGORY = {
    "room": "ROOM", "icu": "ICU", "consultation": "CONSULT", "laboratory": "LAB", "lab": "LAB",
    "radiology": "RADIOLOGY", "surgery": "PROCEDURE", "operation theatre": "PROCEDURE", "ot": "PROCEDURE",
    "implant": "IMPLANT", "anaesthesia": "ANAESTHESIA", "anesthesia": "ANAESTHESIA", "pharmacy": "PHARMACY",
    "consumables": "CONSUMABLE", "non-medical": "NON_MEDICAL", "physiotherapy": "THERAPY",
}
PERSONAL_KEYWORDS = ("attendant food", "television", "tv charges", "telephone", "toiletr", "laundry")


def categorize(dept: str, description: str, known: dict[str, str] | None = None, code: str = "") -> str:
    """Reference-table category wins; otherwise dept heuristics; personal-comfort keywords
    override either because the policy treats them as a separate exclusion."""
    desc = description.lower()
    if any(k in desc for k in PERSONAL_KEYWORDS):
        return "PERSONAL"
    if known and code in known:
        return known[code]
    return DEPT_CATEGORY.get(dept.strip().lower(), "OTHER")


def charge_unit(category: str, description: str = "") -> str:
    """Normalize price comparison units so per-day/visit/test prices are never mixed."""
    return {"ROOM": "per_day", "ICU": "per_day", "CONSULT": "per_visit", "LAB": "per_test", "RADIOLOGY": "per_scan",
            "PROCEDURE": "per_procedure", "THERAPY": "per_session"}.get(category, "per_item")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\n", " ")).strip()


def _find_table(tables: list[list[list[str]]], *needles: str) -> list[list[str]] | None:
    for t in tables:
        if not t:
            continue
        head = " ".join(_norm(c).lower() for c in t[0])
        if all(n in head for n in needles):
            return t
    return None


def _header_fields(tables: list[list[list[str]]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for t in tables:
        for row in t:
            cells = [_norm(c) for c in row]
            for i in range(0, len(cells) - 1, 2):
                key, value = cells[i], cells[i + 1]
                if key and value and not re.fullmatch(r"[\d,.\s]+", key):
                    fields.setdefault(key.lower(), value)
    return fields


def _get(fields: dict[str, str], *keys: str) -> str:
    for k in keys:
        for fk, fv in fields.items():
            if fk.startswith(k):
                return fv
    return ""


def normalize_bill(raw: RawDocument, known_categories: dict[str, str] | None = None) -> Bill:
    items_t = _find_table(raw.tables, "qty", "amount")
    if items_t is None:
        raise ParseError("No itemised-charges table (with Qty and Amount columns) found in the bill.")
    header = [_norm(c).lower() for c in items_t[0]]
    item_table_index = next((i for i, table in enumerate(raw.tables) if table is items_t), None)
    item_page = (raw.table_pages[item_table_index]
                 if item_table_index is not None and item_table_index < len(raw.table_pages) else None)

    def col(*names: str) -> int | None:
        for i, h in enumerate(header):
            if any(h.startswith(n) for n in names):
                return i
        return None

    c_no, c_date, c_code, c_desc = col("#", "s.no", "sl"), col("date"), col("code"), col("description", "particular", "item")
    c_dept, c_qty, c_rate, c_amt = col("dept", "department"), col("qty", "quantity"), col("rate", "unit rate", "unit price"), col("amount")
    c_unit = next((i for i, name in enumerate(header) if name in {'unit', 'uom', 'billing unit', 'unit of measure'}), None)
    if c_desc is None or c_qty is None or c_amt is None:
        raise ParseError(f"Itemised table is missing description/qty/amount columns: {header}")

    lines: list[LineItem] = []
    for idx, row in enumerate(items_t[1:], start=1):
        cells = [_norm(c) for c in row]
        if not any(cells) or not cells[c_desc]:
            continue
        code = cells[c_code] if c_code is not None else ""
        dept = cells[c_dept] if c_dept is not None else ""
        qty = parse_amount(cells[c_qty])
        amount = parse_amount(cells[c_amt])
        rate = parse_amount(cells[c_rate]) if c_rate is not None else (amount / qty if qty else amount)
        no = cells[c_no] if c_no is not None else ""
        category = categorize(dept, cells[c_desc], known_categories, code)
        lines.append(LineItem(
            line_no=int(no) if no.isdigit() else idx,
            service_date=cells[c_date] if c_date is not None else "",
            code=code, description=cells[c_desc], dept=dept,
            category=category, unit=cells[c_unit] if c_unit is not None and cells[c_unit] else charge_unit(category, cells[c_desc]),
            qty=qty, unit_price=rate, amount=amount,
            source_page=item_page, source_text=" | ".join(cells),
        ))

    fields = _header_fields([t for t in raw.tables if t is not items_t])
    deductions: list[Deduction] = []
    ded_t = _find_table(raw.tables, "deduction")
    if ded_t:
        for row in ded_t[1:]:
            cells = [_norm(c) for c in row]
            if len(cells) >= 3 and cells[0]:
                deductions.append(Deduction(label=cells[0], basis=cells[1], amount=parse_amount(cells[-1])))

    age_txt = _get(fields, "age")
    age_match = re.match(r"\s*(\d+)", age_txt)
    network = _get(fields, "network")
    return Bill(
        bill_number=_get(fields, "bill no", "invoice no"),
        hospital=_norm(raw.pages[0].splitlines()[0]) if raw.pages and raw.pages[0] else "",
        hospital_address=_norm(raw.pages[0].splitlines()[1]) if raw.pages and len(raw.pages[0].splitlines()) > 1 else "",
        region=_get(fields, "region", "city", "district", "location"),
        network_hospital=not network.lower().startswith("n") if network else True,
        patient_name=_get(fields, "patient name", "patient"),
        patient_age=int(age_match.group(1)) if age_match else None,
        policy_number=_get(fields, "policy no", "policy number"),
        insurer_tpa=_get(fields, "insurer", "tpa"),
        admission_date=_get(fields, "admission", "date of admission"),
        discharge_date=_get(fields, "discharge", "date of discharge"),
        diagnosis=_get(fields, "diagnosis"),
        procedure=_get(fields, "procedure"),
        line_items=lines,
        stated_gross_total=parse_amount(_get(fields, "gross total", "total")),
        deductions=deductions,
        insurer_approved=parse_amount(_get(fields, "insurer approved", "approved")),
        patient_payable=parse_amount(_get(fields, "patient payable", "balance payable")),
        parser=raw.parser,
    )


CLAUSE_RE = re.compile(r"^(\d+\.\d+)\s+([A-Z][^.]{2,80})\.\s+(.*)$")


def policy_document_identity(pdf_bytes: bytes) -> str:
    """Stable content identity; this does not verify insurer or official policy status."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def normalize_policy(raw: RawDocument, source: str, document_id: str | None = None,
                     version_id: str | None = None) -> PolicyDoc:
    document_id = document_id or raw.document_id
    clauses: list[Clause] = []
    current: Clause | None = None
    for page_no, text in enumerate(raw.pages, start=1):
        for line in text.splitlines():
            line = line.strip()
            m = CLAUSE_RE.match(line)
            if m:
                current = Clause(clause_id=m.group(1), title=m.group(2).strip(), text=m.group(3).strip(),
                                 page=page_no, source=source, document_id=document_id, version_id=version_id or document_id)
                clauses.append(current)
            elif current is not None and line and not re.match(r"^Section \d+", line) and not line.startswith("Synthetic document"):
                current.text = f"{current.text} {line}".strip()
            elif re.match(r"^Section \d+", line):
                current = None
    if not clauses:
        raise ParseError("No numbered clauses (e.g. '3.1 Room Rent. ...') found in the policy document.")
    lines0 = raw.pages[0].splitlines() if raw.pages else []
    schedule: dict[str, str] = {}
    for t in raw.tables:
        for row in t:
            if len(row) == 2 and row[0] and row[1]:
                schedule[_norm(row[0])] = _norm(row[1])
    return PolicyDoc(
        insurer=_norm(lines0[0]) if lines0 else "",
        plan_name=_norm(lines0[1]).replace(" - Policy Wordings", "") if len(lines0) > 1 else "",
        policy_number=schedule.get("Policy Number", ""),
        source=source,
        clauses=clauses,
        schedule=schedule,
        document_id=document_id,
        version_id=version_id or document_id,
    )


def looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"
