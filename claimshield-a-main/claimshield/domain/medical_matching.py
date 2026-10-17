"""Conservative description matching; aliases belong to a reviewed source record."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


def normalize_description(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    # Preserve digits, strength and measurement words; do not strip clinical qualifiers.
    return ' '.join(re.findall(r'[^\W_]+(?:[./][^\W_]+)*|[%+]', text, re.UNICODE))


@dataclass(frozen=True)
class MedicalMatch:
    kind: str
    confidence: float
    matched_name: str | None = None


def match_description(description: str, canonical: str, aliases=()) -> MedicalMatch:
    text = normalize_description(description)
    if not text:
        return MedicalMatch('none', 0.0)
    names = [(canonical, 'exact'), *((alias, 'alias') for alias in aliases)]
    for name, kind in names:
        if text == normalize_description(name):
            return MedicalMatch(kind, 1.0 if kind == 'exact' else .98, name)
    ranked = [(SequenceMatcher(None, text, normalize_description(name)).ratio(), name)
              for name, _ in names if normalize_description(name)]
    score, name = max(ranked, default=(0.0, None))
    # Fuzzy similarity always remains a candidate, including very similar drug strengths.
    return MedicalMatch('possible', min(score, .89), name) if score >= .78 else MedicalMatch('none', score)
