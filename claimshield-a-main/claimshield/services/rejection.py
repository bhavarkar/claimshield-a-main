"""Evidence-grounded analysis of an insurer's rejection notice."""

from __future__ import annotations

import re
from typing import Any

from ..models import Clause, PolicyDoc
from .llm import TemplateLLM
from .retrieval import LocalRetriever

CLASSIFICATIONS = ("Supported by provided evidence", "Potentially inconsistent with policy", "Insufficient evidence")
_DENIAL = re.compile(r"\b(reject(?:ed|ion)?|den(?:y|ied|ial)|not payable|not covered|excluded|inadmissible|disallow(?:ed|ance))\b", re.I)
_STOP = {"claim", "policy", "reason", "rejected", "rejection", "denied", "denial", "not", "covered", "payable", "treatment", "expense", "expenses", "under", "clause", "because", "your", "this", "that", "have", "been", "are", "was", "for", "the", "and", "with"}
_DOC_WORDS = ("discharge summary", "discharge card", "medical records", "investigation report", "test report",
              "original bill", "itemized bill", "itemised bill", "prescription", "pre-authorization", "preauthorization",
              "claim form", "pharmacy bill", "operative notes", "implant invoice", "diagnostic report")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _reason_candidates(text: str) -> list[str]:
    out: list[str] = []
    for paragraph in re.split(r"[\n\r]+|(?<=[.!?])\s+", text):
        sentence = _clean(paragraph.strip(" \t-*•"))
        is_heading = re.fullmatch(r"(?:claim\s+)?(?:rejection|denial)(?:\s+(?:notice|letter))?", sentence, re.I)
        if sentence and _DENIAL.search(sentence) and not is_heading and not re.search(r"please (?:submit|provide|send|furnish)", sentence, re.I):
            if sentence not in out:
                out.append(sentence[:600])
    return out[:12]


def _validated_ai_reasons(text: str, llm: Any) -> list[str]:
    extractor = getattr(llm, "extract_rejection_reasons", None)
    if not extractor:
        return []
    try:
        candidates = extractor(text)
    except Exception:  # model extraction is optional; source text remains authoritative
        return []
    source = _clean(text).casefold()
    grounded: list[str] = []
    for item in candidates or []:
        quote = _clean(str(item.get("quote", ""))) if isinstance(item, dict) else ""
        if quote and quote.casefold() in source and _DENIAL.search(quote) and quote not in grounded:
            grounded.append(quote[:600])
    return grounded[:12]


def _documents(text: str) -> tuple[list[str], list[str]]:
    requested: list[str] = []
    lowered = text.casefold()
    for name in _DOC_WORDS:
        if name in lowered:
            requested.append(name.title())
    explicit = re.findall(r"(?:please\s+)?(?:submit|provide|send|furnish|enclose)\s+(?:the\s+)?([^.;\n]{3,180})", text, re.I)
    for ref in explicit:
        cleaned = _clean(ref).rstrip(".,:")
        if cleaned and cleaned.casefold() not in {x.casefold() for x in requested}:
            requested.append(cleaned[:180])
    suggestions = []
    if not requested:
        # Recommendations are clearly separated from requirements stated by the insurer.
        suggestions = ["Ask the insurer which records would address the stated reason before sending additional documents."]
    return requested, suggestions


def _document_references(text: str, requested: list[str]) -> list[str]:
    refs = list(requested)
    for match in re.findall(r"\b(?:clause|section|form|document)\s+(?:no\.?\s*)?[A-Z0-9][A-Z0-9./-]{0,24}", text, re.I):
        value = _clean(match)
        if value.casefold() not in {item.casefold() for item in refs}:
            refs.append(value)
    return refs


def _terms(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.casefold()) if len(word) > 3 and word not in _STOP}


def _classify(reason: str, clauses: list[Clause]) -> tuple[str, list[Clause], str]:
    reason_terms = _terms(reason)
    scored = []
    for clause in clauses:
        overlap = reason_terms & _terms(f"{clause.title} {clause.text}")
        if overlap:
            scored.append((len(overlap), clause))
    scored.sort(key=lambda item: item[0], reverse=True)
    relevant = [clause for _, clause in scored[:3]]
    if not relevant:
        return CLASSIFICATIONS[2], [], "No policy passage with matching subject matter was found in the supplied wording."

    clause_text = " ".join(c.text for c in relevant)
    restricted = re.search(r"\b(excluded|not payable|not covered|waiting period|shall not pay|will not pay)\b", clause_text, re.I)
    affirmed = re.search(r"\b(covered|payable|will pay|shall pay|included)\b", clause_text, re.I)
    conditional = re.search(r"\b(subject to|unless|except|provided that|may be covered|if applicable)\b", clause_text, re.I)
    if restricted and not conditional:
        return CLASSIFICATIONS[0], relevant, "The retrieved policy wording contains an applicable restriction matching the subject of the reason stated in the notice."
    if affirmed and not restricted and not conditional:
        return CLASSIFICATIONS[1], relevant, "The notice states a denial on this subject, while the retrieved policy passage describes the related expense or benefit as covered or payable. Confirm the clause conditions with the insurer."
    return CLASSIFICATIONS[2], relevant, "The retrieved wording is conditional or does not resolve the stated reason clearly enough to support a conclusion."


def analyze_rejection(letter_text: str, policy: PolicyDoc, *, retriever: Any | None = None,
                      llm: Any | None = None) -> dict[str, Any]:
    """Extract only source-grounded reasons and classify them against retrieved policy wording."""
    llm = llm or TemplateLLM()
    reasons = _validated_ai_reasons(letter_text, llm) or _reason_candidates(letter_text)
    requested_documents, recommended_documents = _documents(letter_text)
    document_references = _document_references(letter_text, requested_documents)
    search = retriever or LocalRetriever()
    results = []
    for index, reason in enumerate(reasons, 1):
        try:
            matches = search.search(policy, reason, top_k=4)
        except Exception:
            matches = LocalRetriever().search(policy, reason, top_k=4)
        classification, evidence, explanation = _classify(reason, matches)
        results.append({"id": f"R{index}", "reason": reason, "classification": classification,
                        "explanation": explanation,
                        "policy_evidence": [{"clause_id": c.clause_id, "title": c.title, "quote": c.text,
                                             "source": c.source, "page": c.page} for c in evidence]})
    if not results:
        results.append({"id": "R1", "reason": None, "classification": CLASSIFICATIONS[2],
                        "explanation": "No explicit rejection reason could be identified in the supplied letter. Request a written reason from the insurer before assessing the decision.",
                        "policy_evidence": []})
    all_evidence = {f"{e['clause_id']}:{e['title']}": e for r in results for e in r["policy_evidence"]}
    draft = _appeal_template(results, policy, requested_documents)
    facts = {"rejection_reasons": [r["reason"] for r in results if r["reason"]],
             "policy_evidence": list(all_evidence.values()), "requested_documents": requested_documents}
    try:
        polished = llm.draft_letter(facts, draft)
        # Keep the deterministic draft if the model introduces a clause reference that was not cited.
        allowed_refs = {e["clause_id"] for e in all_evidence.values()}
        cited_refs = set(re.findall(r"\b\d+\.\d+\b", polished))
        if cited_refs <= allowed_refs:
            draft = polished
    except Exception:
        pass
    return {"reasons": results, "requested_documents": requested_documents,
            "document_references": document_references,
            "additional_documents_to_confirm": recommended_documents,
            "appeal_draft": draft,
            "notice_excerpt": _clean(letter_text)[:4000],
            "disclaimer": "This analysis is informational. It does not determine whether a rejection is lawful or guarantee reconsideration or payment."}


def _appeal_template(reasons: list[dict[str, Any]], policy: PolicyDoc, requested: list[str]) -> str:
    lines = ["Dear Grievance Redressal Officer,", "", "Subject: Request for clarification and review of claim rejection", "",
             "I am writing to request a review of the decision described in the rejection notice. Please consider the policy wording and records for my claim.", ""]
    for idx, reason in enumerate(reasons, 1):
        if reason["reason"]:
            lines.append(f'{idx}. The notice states: "{reason["reason"]}"')
            for e in reason["policy_evidence"]:
                lines.append(f'   The supplied policy includes clause {e["clause_id"]} ({e["title"]}): "{e["quote"]}"')
            lines.append("   Please explain how the cited clause applies to the facts and documents considered for this claim.")
        else:
            lines.append("The supplied notice does not state a specific rejection reason. Please provide the reason and the policy clause relied upon.")
    if requested:
        lines += ["", "The notice requests the following documents:", *[f"- {name}" for name in requested]]
    lines += ["", "Please confirm whether any further records are required and provide a written response explaining the review.", "",
              "Yours faithfully,", "[Policyholder name]", "[Policy number]", "[Contact details]"]
    return "\n".join(lines)
