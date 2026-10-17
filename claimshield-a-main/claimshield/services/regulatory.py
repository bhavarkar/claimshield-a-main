"""Versioned, source-bound IRDAI reference registry and deterministic applicability checks.

Regulatory content remains inactive until an administrator verifies both its provenance and
the exact citation. This module never decides whether an insurer violated a rule.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from enum import Enum
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..domain.medical_matching import normalize_description
from ..models import Category


IRDAI_HEALTH_CIRCULARS = "https://irdai.gov.in/circulars?filterDepartment=HLT&filtersApplied=true"
MAX_REGULATORY_PDF_BYTES = 15 * 1024 * 1024
MAX_REGULATORY_PDF_PAGES = 500
MAX_REGULATORY_EXTRACTED_CHARS = 8 * 1024 * 1024


def source_restricted_to_demo(*, synthetic=False, source_url=None, reviewer=None):
    """Explicit fixture markers and local-demo approvals cannot become production evidence."""
    try:
        host = urlparse(source_url or '').hostname or ''
    except ValueError:
        return True
    marked = synthetic or host == 'invalid' or host.endswith('.invalid') or reviewer == 'local-demo'
    return bool(marked and os.getenv('CLAIMSHIELD_AUTH_MODE', 'iap').strip().lower() != 'demo')


class ReviewStatus(str, Enum):
    NOT_IMPORTED = "not_imported"
    PENDING = "pending_human_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_UPDATE = "needs_update"


class RuleClassification(str, Enum):
    POLICYHOLDER_PROTECTION = "policyholder_protection"
    CLAIM_HANDLING = "claim_handling"
    PRODUCT_REQUIREMENT = "product_requirement"
    PROCESS_REQUIREMENT = "process_requirement"
    OTHER = "other"


class RegulatoryDocument(BaseModel):
    document_id: str
    sha256: str | None = None
    official_source_url: str
    regulator: str = "Insurance Regulatory and Development Authority of India"
    circular_number: str
    title: str
    publication_date: date | None = None
    effective_date: date | None = None
    applicability: list[str] = Field(default_factory=list)
    insurance_category: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    amended_by: list[str] = Field(default_factory=list)
    retrieval_date: date | None = None
    last_verification_date: date | None = None
    original_pdf_ref: str | None = None
    extracted_text_ref: str | None = None
    source_page: int | None = None
    source_paragraph: str | None = None
    human_review_status: ReviewStatus = ReviewStatus.NOT_IMPORTED
    currentness_checked: bool = False
    reviewer_explanation: str = ""
    reviewed_by: str | None = None
    ingestion_note: str = "Official listing metadata only; original PDF has not been imported."


class RuleApplicability(BaseModel):
    insurance_categories: list[str] = Field(default_factory=list)
    claim_contexts: list[str] = Field(default_factory=list)
    policy_types: list[str] = Field(default_factory=list)


class RegulatoryItemRule(BaseModel):
    """Human-reviewed interpretation of an exact quotation, never LLM activation."""
    model_config = ConfigDict(extra='forbid')
    canonical_description: str = Field(min_length=2, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    categories: list[Category] = Field(default_factory=list)
    effect: str = Field(default='policy_dependent', pattern=r'^(non_payable|policy_dependent)$')


class RegulatoryRule(BaseModel):
    rule_id: str
    rule_title: str
    source_document_id: str
    source_citation: str
    source_page: int | None = None
    source_paragraph: str | None = None
    source_quote: str = Field(min_length=1)
    applicability: RuleApplicability = Field(default_factory=RuleApplicability)
    effective_date: date | None = None
    expiry_date: date | None = None
    superseded_by: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    classification: RuleClassification = RuleClassification.OTHER
    human_review_status: ReviewStatus = ReviewStatus.PENDING
    enforcement_enabled: bool = False
    candidate_notes: str = ""
    reviewer_explanation: str = ""
    reviewed_by: str | None = None
    reviewed_at: date | None = None
    item_rule: RegulatoryItemRule | None = None
    synthetic: bool = False

    @model_validator(mode="after")
    def require_disable_until_review(self):
        if self.human_review_status != ReviewStatus.APPROVED and self.enforcement_enabled:
            raise ValueError("unreviewed regulatory rules cannot be enforcement-enabled")
        return self


class RuleReview(BaseModel):
    human_review_status: ReviewStatus
    enforcement_enabled: bool = False
    reviewer_explanation: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def approval_is_explicit(self):
        if self.enforcement_enabled and self.human_review_status != ReviewStatus.APPROVED:
            raise ValueError("enforcement can only be enabled for an approved rule")
        if self.human_review_status not in {ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.NEEDS_UPDATE}:
            raise ValueError("review must approve, reject, or request an update")
        return self


class DocumentReview(BaseModel):
    human_review_status: ReviewStatus
    currentness_checked: bool = False
    reviewer_explanation: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def review_status_is_final(self):
        if self.human_review_status not in {ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.NEEDS_UPDATE}:
            raise ValueError("document review must approve, reject, or request an update")
        return self


class ApplicabilityRequest(BaseModel):
    insurance_category: str | None = None
    event_date: date | None = None
    claim_context: str | None = None
    policy_type: str | None = None


class RegulatoryRegistry:
    """JSON metadata in existing app storage; originals/text are immutable hash-keyed objects."""

    _lock = RLock()
    REGISTRY_KEY = "regulatory/registry.json"

    def __init__(self, storage):
        self.storage = storage

    def _seed(self) -> dict[str, Any]:
        docs = [
            RegulatoryDocument(document_id="irdai-health-master-2024-en", official_source_url=IRDAI_HEALTH_CIRCULARS,
                circular_number="IRDAI/HLT/CIR/PRO/84/5/2024", title="Master Circular on Health Insurance Business (English)",
                publication_date=date(2024, 5, 29), applicability=["health insurance"], insurance_category=["health"]),
            RegulatoryDocument(document_id="irdai-health-master-2024-annexure-en", official_source_url=IRDAI_HEALTH_CIRCULARS,
                circular_number="IRDAI/HLT/CIR/PRO/84/5/2024", title="Annexure to Master Circular on Health Insurance Business (English)",
                publication_date=date(2024, 5, 29), applicability=["health insurance"], insurance_category=["health"]),
            RegulatoryDocument(document_id="irdai-health-master-2024-annexure-hi", official_source_url=IRDAI_HEALTH_CIRCULARS,
                circular_number="IRDAI/HLT/CIR/PRO/84/5/2024", title="Annexure to Master Circular on Health Insurance Business (Hindi)",
                publication_date=date(2024, 5, 29), applicability=["health insurance"], insurance_category=["health"]),
        ]
        return {"schema_version": 1, "documents": {d.document_id: d.model_dump(mode="json") for d in docs}, "rules": {}}

    def _load(self) -> tuple[dict[str, Any], int]:
        if not self.storage.exists(self.REGISTRY_KEY):
            seed = self._seed()
            seed["version"] = 1
            return seed, 0
        try:
            if hasattr(self.storage, "get_with_generation"):
                content, gen = self.storage.get_with_generation(self.REGISTRY_KEY)
            else:
                content, gen = self.storage.get(self.REGISTRY_KEY), 1
        except FileNotFoundError:
            seed = self._seed()
            seed["version"] = 1
            return seed, 0
        data = json.loads(content)
        if (not isinstance(data, dict) or data.get('schema_version') != 1
                or not isinstance(data.get('documents'), dict) or not isinstance(data.get('rules'), dict)):
            raise ValueError("unsupported regulatory registry schema")
        if "version" not in data:
            data["version"] = 1
        return data, gen

    def _save(self, data: dict[str, Any], generation: int = 0) -> None:
        new_version = data.get("version", 1) + 1
        data["version"] = new_version
        payload = json.dumps(data, sort_keys=True, indent=2).encode("utf-8")
        from .storage import StorageConcurrencyError
        try:
            if hasattr(self.storage, "put_if_generation_match"):
                self.storage.put_if_generation_match(self.REGISTRY_KEY, payload, generation, "application/json")
            else:
                self.storage.put(self.REGISTRY_KEY, payload, "application/json")
        except StorageConcurrencyError as exc:
            raise HTTPException(409, "Regulatory registry was modified concurrently by another administrator. Please reload and retry.") from exc
        try:
            self.storage.put(f"regulatory/history/v{new_version}.json", payload, "application/json")
        except Exception:
            pass

    def list_documents(self) -> list[RegulatoryDocument]:
        with self._lock:
            data, _ = self._load()
            return [RegulatoryDocument.model_validate(v) for v in data["documents"].values()]

    def list_rules(self) -> list[RegulatoryRule]:
        with self._lock:
            data, _ = self._load()
            return [RegulatoryRule.model_validate(v) for v in data["rules"].values()]

    def search_rules(self, query: str = "") -> list[RegulatoryRule]:
        terms = set(re.findall(r"[a-z0-9]+", query.casefold()))
        rules = self.list_rules()
        if not terms:
            return rules
        return [r for r in rules if terms & set(re.findall(r"[a-z0-9]+", (r.rule_title + " " + r.source_quote + " " + r.source_citation).casefold()))]

    def validate_source(self, document_id: str, source_url: str) -> RegulatoryDocument:
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname != "irdai.gov.in" or parsed.username or parsed.password:
            raise HTTPException(400, "Only the approved official IRDAI HTTPS listing may be recorded")
        doc = self.get_document(document_id)
        if doc.official_source_url != source_url:
            raise HTTPException(400, "Source URL does not match the approved document listing")
        return doc

    def preview(self, document_id: str, source_url: str, pdf_bytes: bytes) -> dict[str, Any]:
        doc = self.validate_source(document_id, source_url)
        if doc.sha256:
            raise HTTPException(409, "This document ID already has an imported source hash")
        pages = extract_pdf_pages(pdf_bytes)
        return {"document_id": document_id, "title": doc.title, "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                "bytes": len(pdf_bytes), "page_count": len(pages), "pages": pages,
                "provenance_status": "administrator_supplied_pending_source_authentication",
                "notice": "SHA-256 identifies these bytes; it does not authenticate that IRDAI published them."}

    def ingest(self, document_id: str, source_url: str, pdf_bytes: bytes) -> RegulatoryDocument:
        prior_doc = self.validate_source(document_id, source_url)
        # Never accept caller-supplied extracted text: derive it from the exact uploaded bytes.
        extracted_pages = extract_pdf_pages(pdf_bytes)
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        with self._lock:
            data, gen = self._load()
            prior = data["documents"].get(document_id)
            if prior is None:
                raise HTTPException(404, "Document ID is not in the approved IRDAI document allowlist")
            prior_doc = RegulatoryDocument.model_validate(prior)
            if prior_doc.sha256:
                if prior_doc.sha256 == digest:
                    return prior_doc
                raise HTTPException(409, "This document ID already has a different source hash")
            pdf_key, text_key = f"regulatory/documents/{digest}.pdf", f"regulatory/text/{digest}.json"
            if not self.storage.exists(pdf_key):
                self.storage.create(pdf_key, pdf_bytes, "application/pdf")
            if not self.storage.exists(text_key):
                self.storage.create(text_key, json.dumps({"pages": extracted_pages}, ensure_ascii=False).encode(), "application/json")
            record = prior_doc.model_copy(update={"sha256": digest, "retrieval_date": date.today(),
                "original_pdf_ref": pdf_key, "extracted_text_ref": text_key,
                "human_review_status": ReviewStatus.PENDING,
                "ingestion_note": "PDF received from an administrator; provenance and currentness still require human verification."})
            data["documents"][document_id] = record.model_dump(mode="json")
            self._save(data, gen)
            return record

    def get_document(self, document_id: str) -> RegulatoryDocument:
        with self._lock:
            data, _ = self._load()
            doc_data = data["documents"].get(document_id)
            if not doc_data:
                raise HTTPException(404, "Regulatory document not found")
            return RegulatoryDocument.model_validate(doc_data)

    def create_candidate(self, rule: RegulatoryRule) -> RegulatoryRule:
        rule.human_review_status = ReviewStatus.PENDING
        rule.enforcement_enabled = False
        with self._lock:
            data, gen = self._load()
            doc_data = data["documents"].get(rule.source_document_id)
            if not doc_data:
                raise HTTPException(404, "Source regulatory document not found")
            doc = RegulatoryDocument.model_validate(doc_data)
            if not doc.sha256 or not doc.extracted_text_ref:
                raise HTTPException(409, "Source document must be imported before creating a rule candidate")
            if rule.rule_id in data["rules"]:
                raise HTTPException(409, "Rule ID already exists")
            pages = json.loads(self.storage.get(doc.extracted_text_ref)).get("pages", [])
            if rule.source_page is None or rule.source_page < 1 or rule.source_page > len(pages):
                raise HTTPException(400, "Rule citation page is outside the source document")
            if _norm(rule.source_quote) not in _norm(pages[rule.source_page - 1]):
                raise HTTPException(400, "Exact rule quotation was not found on the cited source page")
            data["rules"][rule.rule_id] = rule.model_dump(mode="json")
            self._save(data, gen)
            return rule

    def review_rule(self, rule_id: str, review: RuleReview, reviewer_id: str = "") -> RegulatoryRule:
        with self._lock:
            data, gen = self._load()
            stored = data["rules"].get(rule_id)
            if not stored:
                raise HTTPException(404, "Regulatory rule not found")
            rule = RegulatoryRule.model_validate(stored)
            if review.human_review_status == ReviewStatus.APPROVED:
                if source_restricted_to_demo(synthetic=rule.synthetic):
                    raise HTTPException(409, 'Synthetic regulatory rules may only be reviewed in local demo mode')
                doc_data = data["documents"].get(rule.source_document_id)
                if not doc_data:
                    raise HTTPException(404, "Source regulatory document not found")
                doc = RegulatoryDocument.model_validate(doc_data)
                if doc.human_review_status != ReviewStatus.APPROVED:
                    raise HTTPException(409, "Source document must be human-reviewed before its rule can be approved")
                if not _citation_matches(self.storage, doc, rule):
                    raise HTTPException(409, "Rule citation no longer matches the selected source document")
                if rule.effective_date is None:
                    raise HTTPException(409, "A rule without a verified effective date cannot be approved")
                if rule.item_rule:
                    quote = ' ' + normalize_description(rule.source_quote) + ' '
                    labels = [rule.item_rule.canonical_description, *rule.item_rule.aliases]
                    if any(not normalize_description(label) or ' ' + normalize_description(label) + ' ' not in quote
                           for label in labels):
                        raise HTTPException(409, 'Item names and aliases must appear explicitly in the reviewed source quotation')
            rule.human_review_status = review.human_review_status
            rule.enforcement_enabled = review.enforcement_enabled
            rule.reviewer_explanation = review.reviewer_explanation
            rule.reviewed_by = reviewer_id or None
            rule.reviewed_at = date.today()
            data["rules"][rule_id] = rule.model_dump(mode="json")
            self._save(data, gen)
            return rule

    def review_document(self, document_id: str, review: DocumentReview, verification_date: date | None = None,
                        reviewer_id: str = "") -> RegulatoryDocument:
        with self._lock:
            data, gen = self._load()
            doc_data = data["documents"].get(document_id)
            if not doc_data:
                raise HTTPException(404, "Regulatory document not found")
            doc = RegulatoryDocument.model_validate(doc_data)
            if review.human_review_status == ReviewStatus.APPROVED and not doc.sha256:
                raise HTTPException(409, "An unimported document cannot be approved")
            if review.human_review_status == ReviewStatus.APPROVED and not review.currentness_checked:
                raise HTTPException(409, "Currentness and subsequent amendments must be checked before approval")
            doc.human_review_status = review.human_review_status
            doc.currentness_checked = review.currentness_checked
            doc.reviewer_explanation = review.reviewer_explanation
            doc.reviewed_by = reviewer_id or None
            doc.last_verification_date = verification_date or date.today()
            data["documents"][document_id] = doc.model_dump(mode="json")
            self._save(data, gen)
            return doc

    def evidence(self, document_id: str, page: int | None = None) -> dict[str, Any]:
        doc = self.get_document(document_id)
        if not doc.extracted_text_ref:
            raise HTTPException(404, "Source PDF has not been imported; evidence text is unavailable")
        pages = json.loads(self.storage.get(doc.extracted_text_ref)).get("pages", [])
        if page is not None:
            if page < 1 or page > len(pages):
                raise HTTPException(404, "Page not found")
            return {"document_id": document_id, "sha256": doc.sha256, "page": page, "text": pages[page - 1]}
        return {"document_id": document_id, "sha256": doc.sha256, "pages": len(pages)}

    def original_pdf(self, document_id: str) -> bytes:
        doc = self.get_document(document_id)
        if not doc.original_pdf_ref or not doc.sha256:
            raise HTTPException(404, "Original regulatory PDF has not been imported")
        content = self.storage.get(doc.original_pdf_ref)
        if hashlib.sha256(content).hexdigest() != doc.sha256:
            raise HTTPException(409, "Stored source PDF does not match its registered SHA-256")
        return content

    def applicability(self, rule_id: str, context: ApplicabilityRequest) -> dict[str, Any]:
        with self._lock:
            data, _ = self._load()
            raw = data["rules"].get(rule_id)
            if not raw:
                raise HTTPException(404, "Regulatory rule not found")
            rule = RegulatoryRule.model_validate(raw)
            rules = [RegulatoryRule.model_validate(v) for v in data["rules"].values()]
            status, reasons = evaluate_applicability(rule, context, rules)
            if source_restricted_to_demo(synthetic=rule.synthetic, reviewer=rule.reviewed_by):
                status, reasons = 'Undetermined', ['Synthetic or local-demo evidence cannot be used in production.']
            doc = RegulatoryDocument.model_validate(data["documents"][rule.source_document_id])
            return {"rule_id": rule.rule_id, "status": status, "reasons": reasons,
                    "human_review_status": rule.human_review_status.value,
                    "enforcement_enabled": rule.enforcement_enabled,
                    "source_document_id": doc.document_id, "source_sha256": doc.sha256,
                    "source_citation": rule.source_citation, "source_page": rule.source_page,
                    "source_paragraph": rule.source_paragraph,
                    "assessment_kind": "applicability_only_not_a_violation_determination"}

    def item_rules(self, context: ApplicabilityRequest):
        """Recheck document approval and citations at use time, including revoked sources."""
        rules = self.list_rules()
        result = []
        for rule in rules:
            if rule.item_rule is None:
                continue
            document = self.get_document(rule.source_document_id)
            status, reasons = evaluate_applicability(rule, context, rules)
            if source_restricted_to_demo(synthetic=rule.synthetic, reviewer=rule.reviewed_by):
                status, reasons = 'Undetermined', ['Synthetic or local-demo evidence cannot be used in production.']
            if (document.human_review_status != ReviewStatus.APPROVED or not document.currentness_checked
                    or not document.sha256 or not _citation_matches(self.storage, document, rule)):
                status, reasons = 'Undetermined', ['Source document or citation is not currently verified.']
            if status == 'Applicable':
                try:
                    self.original_pdf(document.document_id)  # Verify stored source integrity.
                except (HTTPException, OSError):
                    status, reasons = 'Undetermined', ['Original source integrity could not be verified.']
            result.append((rule, document, status, reasons))
        return result


def evaluate_applicability(rule: RegulatoryRule, context: ApplicabilityRequest,
                           rules: list[RegulatoryRule] | None = None) -> tuple[str, list[str]]:
    rule_set = rules or []
    replacement_ids = set(r.rule_id for r in rule_set if rule.rule_id in r.supersedes)
    replacement_ids.update([rule.superseded_by] if rule.superseded_by else [])
    replacements = [r for r in rule_set if r.rule_id in replacement_ids]
    if rule.superseded_by and not any(r.rule_id == rule.superseded_by for r in replacements):
        return "Undetermined", ["The recorded superseding rule is missing from the registry."]
    if replacements and context.event_date is None:
        return "Undetermined", ["The event date is required to select the applicable historical rule version."]
    dated_replacements = [r for r in replacements if r.effective_date is not None
                          and context.event_date is not None and context.event_date >= r.effective_date]
    effective_replacements = [r for r in dated_replacements if r.human_review_status == ReviewStatus.APPROVED]
    if effective_replacements:
        return "Superseded", ["A later human-reviewed rule is effective for the supplied event date."]
    if any(r.effective_date is None for r in replacements) or any(
            r.human_review_status not in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}
            for r in dated_replacements):
        return "Undetermined", ["The amendment relationship exists but its effective date or event date is missing."]
    if rule.expiry_date and context.event_date and context.event_date > rule.expiry_date:
        return "Superseded", ["The event occurred after this rule's recorded expiry date."]
    active_conflicts = [r for r in rule_set if r.rule_id != rule.rule_id
                        and r.human_review_status == ReviewStatus.APPROVED
                        and (context.event_date is None or r.effective_date is None or r.effective_date <= context.event_date)
                        and not any(r.rule_id in newer.supersedes and newer.human_review_status == ReviewStatus.APPROVED
                                    and newer.effective_date is not None and context.event_date is not None
                                    and newer.effective_date <= context.event_date for newer in rule_set)]
    if rule.conflicts_with or any(rule.rule_id in r.conflicts_with for r in active_conflicts):
        return "Undetermined", ["Conflicting amendment relationship requires human resolution."]
    if rule.human_review_status != ReviewStatus.APPROVED or not rule.enforcement_enabled:
        return "Undetermined", ["Rule is not both human-reviewed and enabled; no regulatory conclusion is available."]
    if context.event_date is None or rule.effective_date is None:
        return "Undetermined", ["A relevant event date or verified rule effective date is missing."]
    if rule.expiry_date and context.event_date > rule.expiry_date:
        return "Superseded", ["The event occurred after this rule's recorded expiry date."]
    if context.event_date < rule.effective_date:
        return "Not applicable", ["The event occurred before the rule effective date."]
    if not context.insurance_category:
        return "Undetermined", ["Missing applicability input(s): insurance category."]
    if not any((rule.applicability.insurance_categories, rule.applicability.claim_contexts,
                rule.applicability.policy_types)):
        return "Undetermined", ["No human-reviewed applicability conditions are recorded for this rule."]
    checks = [
        (rule.applicability.insurance_categories, context.insurance_category, "insurance category"),
        (rule.applicability.claim_contexts, context.claim_context, "claim context"),
        (rule.applicability.policy_types, context.policy_type, "policy type"),
    ]
    missing, mismatched = [], []
    for allowed, actual, label in checks:
        if allowed and actual is None:
            missing.append(label)
        elif allowed and actual.casefold() not in {value.casefold() for value in allowed}:
            mismatched.append(label)
    if mismatched:
        return "Not applicable", ["Context does not match: " + ", ".join(mismatched) + "."]
    if missing:
        return "Undetermined", ["Missing applicability input(s): " + ", ".join(missing) + "."]
    return "Applicable", ["The reviewed rule's recorded scope and effective period match the supplied context."]


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    if not pdf_bytes or len(pdf_bytes) > MAX_REGULATORY_PDF_BYTES:
        raise HTTPException(413 if len(pdf_bytes) > MAX_REGULATORY_PDF_BYTES else 400,
                            "Regulatory PDF is empty or exceeds the 15 MiB upload limit")
    if not pdf_bytes.startswith(b"%PDF-") or b"%%EOF" not in pdf_bytes[-2048:]:
        raise HTTPException(400, "The supplied file does not have a complete PDF structure")
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages or len(pdf.pages) > MAX_REGULATORY_PDF_PAGES:
                raise ValueError("invalid page count")
            pages = [(page.extract_text() or "") for page in pdf.pages]
        if not pages or not any(page.strip() for page in pages):
            raise ValueError("PDF contains no extractable page text")
        if sum(len(page) for page in pages) > MAX_REGULATORY_EXTRACTED_CHARS:
            raise ValueError("extracted text exceeds limit")
        return pages
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Could not extract text from the supplied PDF ({type(exc).__name__})") from None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _citation_matches(storage, document: RegulatoryDocument, rule: RegulatoryRule) -> bool:
    if not document.extracted_text_ref or rule.source_page is None:
        return False
    try:
        extracted = json.loads(storage.get(document.extracted_text_ref))
        pages = extracted.get('pages') if isinstance(extracted, dict) else None
        return bool(isinstance(pages, list) and all(isinstance(page, str) for page in pages)
                    and 1 <= rule.source_page <= len(pages)
                    and _norm(rule.source_quote) in _norm(pages[rule.source_page - 1]))
    except (OSError, ValueError, TypeError):
        return False
