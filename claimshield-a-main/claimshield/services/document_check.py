"""Text-grounded document classification and configurable claim checklists."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

from ..services.parsing import RawDocument

DOC_LABELS = {
    "hospital_bill": "Hospital bill", "discharge_summary": "Discharge summary",
    "payment_receipt": "Payment receipt", "prescription": "Prescription",
    "investigation_report": "Investigation report", "claim_form": "Claim form",
    "policy_document": "Policy document",
}

SIGNALS = {
    "hospital_bill": (r"in.patient final bill", r"hospital bill", r"itemi[sz]ed (?:charges|bill)", r"tax invoice", r"qty\s+.*amount", r"gross total"),
    "discharge_summary": (r"discharge summary", r"discharge date", r"date of discharge", r"condition at discharge", r"discharge diagnosis"),
    "payment_receipt": (r"payment receipt", r"receipt no", r"amount received", r"payment mode", r"paid via", r"receipt for payment"),
    "prescription": (r"prescription", r"\brx\b", r"prescribed medication", r"medication:", r"prescriber:", r"take .* (?:daily|times a day)", r"doctor.?s prescription"),
    "investigation_report": (r"investigation report", r"laboratory report", r"diagnostic report", r"test results?", r"radiology report", r"impression:"),
    "claim_form": (r"health insurance claim form", r"claim form", r"claimant details", r"details of claim", r"signature of claimant"),
    "policy_document": (r"policy wordings", r"policy wording", r"sum insured", r"policy schedule", r"\b\d+\.\d+\s+[A-Z].{2,80}\."),
}

FIELD_SIGNALS = {
    "hospital_bill": {
        "hospital/provider": (r"hospital", r"clinic", r"medical centre"),
        "patient/member name": (r"patient name", r"name of patient", r"member name"),
        "bill/invoice number": (r"bill no", r"invoice no", r"bill number"),
        "itemized charges": (r"itemi[sz]ed", r"qty", r"quantity"),
        "total amount": (r"gross total", r"bill total", r"total amount", r"grand total"),
    },
    "discharge_summary": {
        "patient/member name": (r"patient name", r"name of patient"), "admission date": (r"admission date", r"date of admission"),
        "discharge date": (r"discharge date", r"date of discharge"), "diagnosis": (r"diagnosis",),
        "treatment/procedure": (r"procedure", r"treatment", r"operation performed"),
    },
    "payment_receipt": {
        "amount paid/received": (r"amount received", r"amount paid", r"paid amount", r"total paid"),
        "receipt number": (r"receipt no", r"receipt number"), "payment date": (r"payment date", r"date of payment", r"receipt date"),
    },
    "prescription": {"patient name": (r"patient",), "prescriber/doctor": (r"doctor", r"prescriber", r"consultant"),
                      "prescription date": (r"date",), "medication details": (r"tablet", r"capsule", r"mg", r"medicine")},
    "investigation_report": {"patient/member name": (r"patient name", r"patient:"), "test/investigation": (r"test name", r"investigation", r"examination"),
                             "result/findings": (r"result", r"finding", r"impression"), "report date": (r"report date", r"collection date", r"date:" )},
    "claim_form": {"claimant/patient name": (r"claimant", r"patient name", r"insured name"), "policy number": (r"policy no", r"policy number"),
                   "claim number": (r"claim no", r"claim number"), "signature": (r"signature", r"signed")},
    "policy_document": {"policy number": (r"policy no", r"policy number"), "plan/insurer name": (r"policy wording", r"insurance", r"assurance"),
                        "sum insured or coverage terms": (r"sum insured", r"coverage", r"benefit")},
}


def _plain_text(raw: RawDocument) -> str:
    return "\n".join(page for page in raw.pages if page and page.strip()).strip()


def _classify(text: str) -> tuple[str | None, str, str]:
    if len(re.sub(r"\s+", "", text)) < 40:
        return None, "uncertain", "Too little readable text was extracted to identify this document reliably."
    scores = {kind: sum(bool(re.search(pattern, text, re.I)) for pattern in patterns) for kind, patterns in SIGNALS.items()}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, score = ranked[0]
    runner_up = ranked[1][1]
    if score < 2 or score == runner_up:
        return None, "uncertain", "The extracted text does not distinguish a supported document type with enough confidence."
    matched = [pattern for pattern in SIGNALS[best] if re.search(pattern, text, re.I)]
    return best, "detected", "Matched text indicators: " + ", ".join(matched[:3])


def _fields(kind: str, text: str) -> tuple[list[str], list[str]]:
    lower = text.casefold()
    detected, missing = [], []
    for label, patterns in FIELD_SIGNALS.get(kind, {}).items():
        (detected if any(re.search(pattern, lower, re.I) for pattern in patterns) else missing).append(label)
    return detected, missing


def load_requirements(data_dir: Path, insurer: str | None = None) -> dict[str, Any]:
    path = data_dir / "document_requirements.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "generic": {}, "insurers": {}}
    insurer_config = None
    if insurer:
        insurer_config = next((v for k, v in config.get("insurers", {}).items() if k.casefold() == insurer.casefold()), None)
    return {"version": config.get("version", 1), "generic": config.get("generic", {}),
            "insurer_config": insurer_config, "insurer_name": insurer}


def check_documents(documents: list[dict[str, Any]], *, claim_type: str, data_dir: Path,
                    insurer: str | None = None) -> dict[str, Any]:
    if claim_type not in {"reimbursement", "cashless", "day_care", "default"}:
        raise ValueError("claim_type must be reimbursement, cashless, day_care, or default")
    config = load_requirements(data_dir, insurer)
    generic = config["generic"]
    required = generic.get("required", {}).get(claim_type, generic.get("required", {}).get("default", []))
    recommended = generic.get("recommended", {}).get(claim_type, generic.get("recommended", {}).get("default", []))
    insurer_requirements = config.get("insurer_config") or {}
    insurer_verified = insurer_requirements.get("verified") is True and bool(insurer_requirements.get("source"))
    insurer_required = insurer_requirements.get("required", []) if insurer_verified else []
    insurer_unverified = [] if insurer_verified or not insurer_requirements else insurer_requirements.get("required", [])

    classified = []
    hashes: dict[str, int] = {}
    types_found: dict[str, int] = {}
    for index, doc in enumerate(documents, 1):
        filename = str(doc.get("filename") or f"document-{index}.pdf")
        content = bytes(doc.get("content", b""))
        digest = hashlib.sha256(content).hexdigest()
        duplicate_of = hashes.get(digest)
        if duplicate_of is None:
            hashes[digest] = index
        raw = doc.get("raw")
        text = _plain_text(raw) if raw is not None else ""
        kind, state, why = _classify(text)
        if duplicate_of is not None:
            state = "duplicate"
            why = f"This file is byte-for-byte identical to uploaded document {duplicate_of}."
        detected_fields, missing_fields = _fields(kind, text) if kind else ([], [])
        if kind and duplicate_of is None:
            types_found[kind] = index
        classified.append({"id": f"D{index}", "filename": filename, "detected_type": kind,
                           "type_label": DOC_LABELS.get(kind, "Uncertain document"), "status": state,
                           "reason": why, "text_characters": len(text), "fields_detected": detected_fields,
                           "fields_not_detected": missing_fields, "duplicate_of": f"D{duplicate_of}" if duplicate_of else None,
                           "evidence_excerpt": re.sub(r"\s+", " ", text)[:360] if text else ""})

    checklist = []
    for kind in required:
        checklist.append(_requirement(kind, "required generic recommendation", False, "Generic checklist configuration", types_found, classified))
    for kind in recommended:
        if kind not in required:
            checklist.append(_requirement(kind, "recommended generic document", False, "Generic checklist configuration", types_found, classified))
    for kind in insurer_required:
        checklist.append(_requirement(kind, "verified insurer requirement", True, insurer_requirements.get("source", ""), types_found, classified))
    for kind in insurer_unverified:
        checklist.append({"document_type": kind, "label": DOC_LABELS.get(kind, kind.replace("_", " ").title()),
                          "status": "uncertain", "requirement_kind": "unverified insurer configuration",
                          "verified": False, "source": insurer_requirements.get("source", "Configuration lacks a verification source"),
                          "note": "Not enforced as an insurer requirement until its source and verification are configured."})

    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    writer.writerow(["Document", "Status", "Requirement type", "Verified", "Source", "Note"])
    for item in checklist:
        writer.writerow([item["label"], item["status"], item["requirement_kind"], item["verified"], item["source"], item.get("note", "")])
    summary = {state: sum(1 for item in checklist if item["status"] == state) for state in ("complete", "missing", "uncertain")}
    return {"claim_type": claim_type, "insurer": insurer, "documents": classified, "checklist": checklist,
            "summary": summary, "generic_recommendations_notice": "Generic checklist entries are recommendations, not verified insurer requirements.",
            "export_csv": csv_buffer.getvalue()}


def _requirement(kind: str, requirement_kind: str, verified: bool, source: str,
                 types_found: dict[str, int], documents: list[dict[str, Any]]) -> dict[str, Any]:
    index = types_found.get(kind)
    document = documents[index - 1] if index else None
    if document is None:
        status = "missing"
        note = "No uploaded document was classified as this type based on its extracted text."
    elif document["status"] in {"uncertain", "duplicate"}:
        status = "uncertain"
        note = "The uploaded file could not be counted as a distinct, reliably classified document."
    elif document["fields_not_detected"]:
        status = "uncertain"
        note = "Some checklist fields were not detected in extracted text; verify the original document."
    else:
        status = "complete"
        note = "A matching document and the configured key fields were detected in extracted text."
    return {"document_type": kind, "label": DOC_LABELS.get(kind, kind.replace("_", " ").title()),
            "status": status, "requirement_kind": requirement_kind, "verified": verified,
            "source": source, "note": note, "document_id": document["id"] if document else None,
            "fields_not_detected": document["fields_not_detected"] if document else []}
