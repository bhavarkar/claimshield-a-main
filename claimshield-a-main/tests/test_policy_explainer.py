import json

import pytest

from claimshield.models import Clause, PolicyDoc
from claimshield.services.policy_explainer import explain_policy


class Retriever:
    def __init__(self, clause):
        self.clause = clause
        self.called = False

    def search(self, policy, query, top_k=3):
        self.called = True
        return [self.clause]


class Gemini:
    available = True

    def __init__(self, result):
        self.result = result
        self.prompt = None

    def _generate(self, prompt, **kwargs):
        self.prompt = prompt
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def run(clause_text, language="en", result=None):
    clause = Clause(clause_id="4.2", title="Co-payment", text=clause_text, page=3, source="policy.pdf")
    retriever, llm = Retriever(clause), Gemini(result if result is not None else RuntimeError("offline"))
    output = explain_policy(PolicyDoc(clauses=[clause]), "co-payment waiting period", language, retriever, llm)
    return output, retriever, llm


def test_numeric_preservation_and_original_clause_returned():
    text = "A co-payment of 20% applies to admissible expenses up to Rs. 50,000 from 2025-01-01."
    output, retriever, _ = run(text, "hi")
    assert retriever.called
    assert "20%" in output["explanation"]
    assert "20%" in output["passages"][0]["text"]
    assert output["passages"][0]["text"] == text
    assert "does not replace" in output["notice"]


def test_correct_copay_percentage_is_used():
    output, _, _ = run("A co-payment of 15% of eligible claims shall apply.", "kn")
    assert "15%" in output["explanation"]
    assert "20%" not in output["explanation"]


def test_waiting_period_wording_preserves_duration():
    output, _, _ = run("A 24 months waiting period applies to specified conditions.", "hi")
    assert "24 months" in output["explanation"]
    assert "किस उपचार" in output["explanation"]


def test_unclear_source_is_explicitly_uncertain():
    output, _, _ = run("... unclear clause ...", "en")
    assert output["uncertain"] is True
    assert "unclear" in output["explanation"].lower()


def test_unsupported_language_rejected():
    with pytest.raises(ValueError, match="Unsupported language"):
        explain_policy(PolicyDoc(), "copay", "fr", Retriever(None), Gemini(RuntimeError()))


def test_gemini_unavailable_uses_deterministic_fallback():
    output, _, llm = run("A co-payment of 10% applies.", "en", RuntimeError("service unavailable"))
    assert output["method"] == "deterministic_fallback"
    assert "10%" in output["explanation"]
    assert llm.prompt is not None


def test_gemini_output_with_changed_percentage_is_rejected():
    output, _, _ = run("A co-payment of 10% applies.", "en", json.dumps({"explanation": "A co-payment of 20% applies.", "uncertain": False}))
    assert output["method"] == "deterministic_fallback"
    assert "10%" in output["explanation"]


def test_gemini_used_only_after_retrieval_and_keeps_numeric_tokens():
    output, retriever, llm = run("A 20% co-payment applies for 30 days.", "en",
                                json.dumps({"explanation": "A 20% co-payment applies for 30 days.", "uncertain": False}))
    assert retriever.called and llm.prompt.index("Source passages") > llm.prompt.index("Question:")
    assert output["method"] == "gemini"
    assert "20%" in output["explanation"] and "30 days" in output["explanation"]
