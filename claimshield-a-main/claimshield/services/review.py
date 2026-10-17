"""Human review queue and synthetic false-positive evaluation helpers."""
from __future__ import annotations

from collections import defaultdict
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReviewOutcome(str, Enum):
    CONFIRMED_ISSUE = "confirmed_issue"
    LEGITIMATE_CHARGE = "legitimate_charge"
    INCONCLUSIVE = "inconclusive"


class ReviewDecision(BaseModel):
    outcome: ReviewOutcome
    explanation: str = Field(min_length=3, max_length=2000)


DETERMINISTIC_TYPES = {"ARITHMETIC_ERROR", "TOTAL_MISMATCH", "QUANTITY_MISMATCH"}


def build_review_item(case: dict[str, Any]) -> dict[str, Any]:
    findings = case.get("findings") or []
    items = []
    for finding in findings:
        kind = finding.get("type", "")
        items.append({"finding_id": finding.get("finding_id"), "type": kind,
                      "title": finding.get("title", "Audit finding"),
                      "classification": "deterministic_error" if kind in DETERMINISTIC_TYPES else "suspicious_pattern",
                      "confidence": finding.get("confidence", "Worth reviewing"),
                      "evidence": finding.get("evidence") or {"bill_evidence": finding.get("bill_evidence", ""),
                                                                  "bill_lines": finding.get("bill_lines", []),
                                                                  "policy": finding.get("policy")},
                      "impact": finding.get("impact", 0), "human_outcome": None,
                      "reviewer_explanation": None,
                      "disclaimer": "An audit flag is not a fraud determination; human review is required."})
    return {"case_id": case.get("case_id"), "status": case.get("status"),
            "bill_name": case.get("bill_name", ""), "clean_bill": not bool(findings),
            "finding_count": len(items), "findings": items,
            "message": "No findings were produced for this bill." if not items else None}


def record_outcome(item: dict[str, Any], finding_id: str, decision: ReviewDecision,
                   reviewer_id: str, recorded_at: str) -> dict[str, Any]:
    if not any(f.get("finding_id") == finding_id for f in item.get("findings", [])):
        raise KeyError(finding_id)
    updated = dict(item)
    updated["findings"] = [dict(f) for f in item["findings"]]
    finding = next(f for f in updated["findings"] if f["finding_id"] == finding_id)
    history = list(finding.get("review_history", []))
    history.append({"outcome": decision.outcome.value, "explanation": decision.explanation,
                    "reviewer_id": reviewer_id, "reviewed_at": recorded_at})
    finding.update(human_outcome=decision.outcome.value, reviewer_explanation=decision.explanation,
                   reviewer_id=reviewer_id, reviewed_at=recorded_at, review_history=history,
                   disagreement=(len({entry["reviewer_id"] for entry in history}) > 1
                                 and len({entry["outcome"] for entry in history}) > 1))
    return updated


def confusion_matrix(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate issue/non-issue outcomes; inconclusive labels are counted separately."""
    matrix = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0,
              "inconclusive": 0}
    for row in labels:
        truth, flagged = row["label"], bool(row["flagged"])
        if truth == "inconclusive":
            matrix["inconclusive"] += 1
        elif truth == "confirmed_issue":
            matrix["true_positive" if flagged else "false_negative"] += 1
        else:
            matrix["false_positive" if flagged else "true_negative"] += 1
    total = sum(matrix[k] for k in ("true_positive", "false_positive", "true_negative", "false_negative"))
    matrix["evaluated"] = total
    matrix["false_positive_rate"] = matrix["false_positive"] / (matrix["false_positive"] + matrix["true_negative"]) if (matrix["false_positive"] + matrix["true_negative"]) else None
    matrix["accuracy"] = (matrix["true_positive"] + matrix["true_negative"]) / total if total else None
    return matrix


def outcome_metrics(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    disagreements = 0
    for row in outcomes:
        outcome = row.get("human_outcome")
        if outcome:
            counts[outcome] += 1
            by_type[row.get("type", "unknown")][outcome] += 1
        history = row.get("review_history", [])
        if (len({entry.get("reviewer_id") for entry in history}) > 1
                and len({entry.get("outcome") for entry in history}) > 1):
            disagreements += 1
    reviewed = sum(counts.values())
    false_positives = counts[ReviewOutcome.LEGITIMATE_CHARGE.value]
    return {"reviewed_findings": reviewed, "outcomes": dict(counts),
            "false_positive_count": false_positives,
            "false_positive_share_of_reviewed": false_positives / reviewed if reviewed else None,
            "reviewer_disagreements": disagreements,
            "by_finding_type": {key: dict(value) for key, value in by_type.items()},
            "note": "Reviewer outcomes are evaluation labels, not model retraining or fraud determinations."}
