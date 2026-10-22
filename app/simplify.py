"""
Turns medical words into plain language.
- find_terms(): which glossary words appear in the transcript (the web page highlights them)
- plain_medicines(): "500 mg TDS PRN" -> "500 mg, three times a day, only when needed"
These are fixed rules (no AI), so they are always correct for the words in the glossary.
"""
import json
import re
from functools import lru_cache

from . import config


@lru_cache
def load_glossary() -> dict:
    with open(config.DATA_DIR / "glossary.json", encoding="utf-8") as f:
        return json.load(f)["terms"]


def _pattern(term: str) -> re.Pattern:
    # Short ALL-CAPS abbreviations (OD, BD, MI) must match exactly, so "od" in normal text isn't caught.
    case_sensitive = term.isupper() and len(term) <= 4
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(r"(?<![\w/])" + re.escape(term) + r"(?![\w/])", flags)


def find_terms(text: str) -> list[dict]:
    """Return [{"term": "HTN", "meaning": "high blood pressure"}, ...] for terms found in text."""
    found = []
    for term, meaning in load_glossary().items():
        if _pattern(term).search(text):
            found.append({"term": term, "meaning": meaning})
    # Longest first, so "known case of" is highlighted before "case".
    found.sort(key=lambda x: -len(x["term"]))
    return found


FREQUENCY_WORDS = {
    "OD": "once a day", "QD": "once a day", "BD": "twice a day", "BID": "twice a day",
    "TDS": "three times a day", "TID": "three times a day", "QID": "four times a day",
    "HS": "at bedtime", "PRN": "only when needed", "SOS": "only when needed", "STAT": "right away",
    "AC": "before meals", "PC": "after meals", "PO": "by mouth",
}


def plain_frequency(freq: str) -> str:
    if not freq:
        return ""
    words = re.split(r"[\s,/]+", freq.strip())
    translated = [FREQUENCY_WORDS.get(w.upper().strip("."), None) for w in words]
    if all(t is None for t in translated):
        return freq  # already in normal words
    return ", ".join(t if t else w for t, w in zip(translated, words))


def plain_medicines(medicines: list[dict]) -> list[dict]:
    out = []
    for m in medicines:
        parts = [p for p in [m.get("dose", ""), plain_frequency(m.get("frequency", "")),
                             f"for {m['duration']}" if m.get("duration") else "", m.get("instructions", "")] if p]
        out.append({**m, "plain": ", ".join(parts)})
    return out
