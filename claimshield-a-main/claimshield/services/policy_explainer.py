"""Evidence-grounded, multilingual policy explanations with deterministic fallbacks."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..models import Clause, PolicyDoc
from .retrieval import LocalRetriever

LANGUAGES = {"en": "English", "hi": "Hindi", "kn": "Kannada"}


class ExplainRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    language: str = "en"


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d[\d,]*(?:\.\d+)?(?:\s*%|\s*(?:days?|months?|years?))?", text, flags=re.I)


def _fallback(clause: Clause, language: str, unclear: bool) -> str:
    text = clause.text.strip()
    copay = re.search(r"(\d+(?:\.\d+)?\s*%)\s*(?:of\s+)?(?:the\s+)?(?:admissible|eligible|claim|medical)?\s*(?:expenses?|amount|claim)?", text, re.I)
    wait = re.search(r"(\d+\s*(?:days?|months?|years?))", text, re.I)
    exclusion = bool(re.search(r"\b(exclud(?:ed|es|ing)|not covered|will not be covered|shall not|not payable)\b", text, re.I))
    if language == "en":
        if unclear:
            return "The supplied clause is unclear or incomplete. Its effect cannot be explained confidently; please check the original wording with your insurer."
        if copay:
            return f"This clause says a co-payment of {copay.group(1)} applies. The policyholder may need to pay that share of the eligible amount, subject to the full clause conditions."
        if wait:
            return f"This clause mentions a waiting period of {wait.group(1)}. Check the full clause for which treatment or condition it applies to and when the period starts."
        if exclusion:
            return "This clause contains exclusion or non-payment wording. The exact scope depends on the conditions in the original clause."
        return "This is a simplified guide to the retrieved clause. Read the original wording for all conditions and exceptions."
    # Keep financial and duration expressions verbatim so the fallback cannot alter them.
    if unclear:
        return ("इस खंड का अर्थ स्पष्ट नहीं है। इसका प्रभाव भरोसे से नहीं समझाया जा सकता; कृपया मूल शब्दावली के साथ बीमाकर्ता से पुष्टि करें।" if language == "hi"
                else "ಈ ಷರತ್ತಿನ ಅರ್ಥ ಸ್ಪಷ್ಟವಾಗಿಲ್ಲ. ಇದರ ಪರಿಣಾಮವನ್ನು ಖಚಿತವಾಗಿ ವಿವರಿಸಲು ಸಾಧ್ಯವಿಲ್ಲ; ಮೂಲ ಪಠ್ಯದೊಂದಿಗೆ ವಿಮಾದಾರರಿಂದ ದೃಢೀಕರಿಸಿ.")
    if copay:
        return (f"ಈ ಷರತ್ತಿನ ಪ್ರಕಾರ {copay.group(1)} ಸಹ-ಪಾವತಿ ಅನ್ವಯಿಸುತ್ತದೆ. ಪೂರ್ಣ ಷರತ್ತಿನ ನಿಯಮಗಳಿಗೆ ಒಳಪಟ್ಟು, ಅರ್ಹ ಮೊತ್ತದ ಆ ಪಾಲನ್ನು ಪಾಲಿಸಿದಾರರು ಪಾವತಿಸಬೇಕಾಗಬಹುದು." if language == "kn" else
                f"इस शर्त के अनुसार {copay.group(1)} सह-भुगतान लागू होता है। पूरी शर्त की सीमाओं के अधीन, पात्र राशि का यह हिस्सा पॉलिसीधारक को देना पड़ सकता है।")
    if wait:
        return (f"ಈ ಷರತ್ತಿನಲ್ಲಿ {wait.group(1)} ಕಾಯುವ ಅವಧಿ ಇದೆ. ಅದು ಯಾವ ಚಿಕಿತ್ಸೆ ಅಥವಾ ಸ್ಥಿತಿಗೆ ಅನ್ವಯಿಸುತ್ತದೆ ಮತ್ತು ಅವಧಿ ಯಾವಾಗ ಆರಂಭವಾಗುತ್ತದೆ ಎಂಬುದನ್ನು ಪೂರ್ಣ ಷರತ್ತಿನಲ್ಲಿ ಪರಿಶೀಲಿಸಿ." if language == "kn" else
                f"इस शर्त में {wait.group(1)} की प्रतीक्षा अवधि दी गई है। यह किस उपचार या स्थिति पर लागू होती है और अवधि कब शुरू होती है, यह पूरी शर्त में देखें।")
    if exclusion:
        return ("ಈ ಷರತ್ತಿನಲ್ಲಿ ಹೊರತಾಗಿಸುವ ಅಥವಾ ಪಾವತಿಸದಿರುವ ಪದಗಳಿವೆ. ಅದರ ವ್ಯಾಪ್ತಿ ಮೂಲ ಷರತ್ತಿನ ನಿಯಮಗಳ ಮೇಲೆ ಅವಲಂಬಿತವಾಗಿದೆ." if language == "kn" else
                "इस शर्त में अपवर्जन या भुगतान न करने की बात है। इसका दायरा मूल शर्त की सीमाओं पर निर्भर करता है।")
    return ("ಇದು ದೊರೆತ ಷರತ್ತಿನ ಸರಳ ವಿವರಣೆ. ಎಲ್ಲಾ ನಿಯಮಗಳು ಮತ್ತು ವಿನಾಯಿತಿಗಳಿಗಾಗಿ ಮೂಲ ಪಠ್ಯವನ್ನು ಓದಿ." if language == "kn" else
            "यह मिले हुए खंड का सरल विवरण है। सभी शर्तों और अपवादों के लिए मूल पाठ पढ़ें।")


def explain_policy(policy: PolicyDoc, question: str, language: str, retriever: Any, llm: Any) -> dict:
    if language not in LANGUAGES:
        raise ValueError("Unsupported language. Choose en, hi, or kn.")
    query = question.strip()
    try:
        passages = retriever.search(policy, query, top_k=3)
    except Exception:
        passages = []
    if not passages:
        passages = LocalRetriever().search(policy, query, top_k=3)
    if not passages:
        return {"language": language, "explanation": _fallback(Clause(clause_id="", title="", text=""), language, True),
                "uncertain": True, "uncertainty": "No matching source passage was found.", "passages": [],
                "notice": "This explanation is an aid only. The original policy wording remains contractual.", "method": "deterministic_fallback"}
    source = "\n\n".join(c.text for c in passages)
    unclear = len(source.strip()) < 25 or bool(re.search(r"\b(?:unclear|illegible|[.]{3,})\b", source, re.I))
    explanation = ""
    method = "deterministic_fallback"
    generate = getattr(llm, "_generate", None)
    if not unclear and callable(generate) and getattr(llm, "available", True):
        prompt = (f"Explain the supplied insurance policy passages in plain language in {LANGUAGES[language]}. "
                  "Do not provide legal advice or infer facts. Preserve every amount, percentage, date, duration, "
                  "exclusion, condition, and exception exactly. If wording is ambiguous, say so explicitly. "
                  "Return JSON with explanation (string) and uncertain (boolean).\n"
                  f"Question: {query}\nSource passages (verbatim):\n{source}")
        try:
            raw = generate(prompt, json_schema={"type": "OBJECT", "properties": {"explanation": {"type": "STRING"}, "uncertain": {"type": "BOOLEAN"}}, "required": ["explanation", "uncertain"]}, temperature=0)
            parsed = json.loads(raw)
            candidate = str(parsed.get("explanation", "")).strip()
            if candidate and _numbers(candidate) == _numbers(source):
                explanation, method = candidate, "gemini"
                unclear = unclear or bool(parsed.get("uncertain"))
        except Exception:
            pass
    if not explanation:
        explanation = _fallback(passages[0], language, unclear)
        figures = _numbers(source)
        if figures and any(value not in _numbers(explanation) for value in figures):
            label = "मूल पॉलिसी में दिए अंक" if language == "hi" else "ಮೂಲ ಪಾಲಿಸಿಯಲ್ಲಿರುವ ಅಂಕಿಗಳು" if language == "kn" else "Figures stated in the source policy"
            explanation += f"\n{label}: " + ", ".join(figures)
    uncertain = unclear
    return {"language": language, "explanation": explanation, "uncertain": uncertain,
            "uncertainty": "The wording is ambiguous or incomplete; confirm its meaning with the insurer." if uncertain else None,
            "passages": [{"clause_id": c.clause_id, "title": c.title, "text": c.text, "page": c.page, "source": c.source} for c in passages],
            "notice": "This explanation is an aid only. Translated or simplified text does not replace the original contractual policy wording.",
            "method": method}
