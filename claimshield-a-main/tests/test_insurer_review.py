import json
from pathlib import Path

from claimshield.services.review import (ReviewDecision, ReviewOutcome, build_review_item,
                                         confusion_matrix, outcome_metrics, record_outcome)

LABELS = json.loads((Path(__file__).parents[1] / "data/evaluation/synthetic_review_labels.json").read_text())["labels"]


def case_with(kind="DUPLICATE_CHARGE", title="Duplicate CBC charge"):
    return {"case_id": "case-123456", "status": "complete", "bill_name": "BILL-004",
            "findings": [{"finding_id": "f1", "type": kind, "title": title,
                          "bill_lines": [2, 5], "bill_evidence": "Same CBC code on separate days",
                          "confidence": "Worth reviewing", "impact": 450,
                          "evidence": {"details": "Specimen dates differ"}}]}


def test_legitimate_repeat_laboratory_charge_can_be_reviewed():
    item = build_review_item(case_with())
    assert item["findings"][0]["classification"] == "suspicious_pattern"
    reviewed = record_outcome(item, "f1", ReviewDecision(outcome="legitimate_charge", explanation="CBC tests occurred on separate dates."), "reviewer-a", "2026-10-15")
    assert reviewed["findings"][0]["human_outcome"] == "legitimate_charge"
    assert "not a fraud determination" in reviewed["findings"][0]["disclaimer"]


def test_confirmed_duplicate_is_not_confused_with_deterministic_arithmetic_error():
    item = build_review_item(case_with())
    reviewed = record_outcome(item, "f1", ReviewDecision(outcome=ReviewOutcome.CONFIRMED_ISSUE, explanation="The same service and date were billed twice."), "reviewer-a", "2026-10-15")
    assert reviewed["findings"][0]["classification"] == "suspicious_pattern"
    assert reviewed["findings"][0]["human_outcome"] == "confirmed_issue"


def test_ambiguous_charge_can_be_marked_inconclusive():
    item = build_review_item(case_with("PRICE_OUTLIER", "Unusual line charge"))
    reviewed = record_outcome(item, "f1", ReviewDecision(outcome="inconclusive", explanation="No comparable invoice detail was provided."), "reviewer-a", "2026-10-15")
    assert reviewed["findings"][0]["human_outcome"] == "inconclusive"


def test_reviewer_disagreement_is_retained():
    item = build_review_item(case_with())
    first = record_outcome(item, "f1", ReviewDecision(outcome="confirmed_issue", explanation="Duplicate on same service date."), "reviewer-a", "2026-10-15")
    second = record_outcome(first, "f1", ReviewDecision(outcome="legitimate_charge", explanation="Separate samples were taken on another day."), "reviewer-b", "2026-10-15")
    assert second["findings"][0]["disagreement"] is True
    assert len(second["findings"][0]["review_history"]) == 2
    assert outcome_metrics(second["findings"])["reviewer_disagreements"] == 1


def test_incorrect_automated_classification_stays_advisory():
    item = build_review_item(case_with("DUPLICATE_CHARGE", "Possible repeat test"))
    assert item["findings"][0]["human_outcome"] is None
    assert item["findings"][0]["disclaimer"].startswith("An audit flag")
    reviewed = record_outcome(item, "f1", ReviewDecision(outcome="legitimate_charge", explanation="Distinct dates and specimens confirm this was appropriate."), "reviewer-a", "2026-10-15")
    assert reviewed["findings"][0]["human_outcome"] == "legitimate_charge"


def test_clean_bill_with_zero_findings_is_present_and_synthetic_matrix_counts_it():
    item = build_review_item({"case_id": "case-clean", "status": "complete", "bill_name": "BILL-004", "findings": []})
    assert item["clean_bill"] and item["finding_count"] == 0 and item["findings"] == []
    matrix = confusion_matrix(LABELS)
    assert matrix["true_negative"] == 1
    assert matrix["false_positive"] == 2
    assert matrix["true_positive"] == 1
    assert matrix["inconclusive"] == 1
