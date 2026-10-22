"""
Maps diagnoses and symptoms to ICD-10 codes.

How it handles "every doctor says it differently":
  1. The AI rewrites what was said into a standard medical name ("sugar problem" -> "diabetes").
  2. This file compares that name (and the doctor's own words) with every code's description
     and synonyms in data/icd10_common.csv, using fuzzy matching (rapidfuzz).
  3. The best match wins. Weak matches are flagged for review instead of being guessed.

The code itself comes from the CSV file, never from the AI, so the AI can't invent codes.
"""
import csv
import re
from functools import lru_cache

from rapidfuzz import fuzz

from . import config


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache
def load_codes() -> list[dict]:
    rows = []
    with open(config.DATA_DIR / "icd10_common.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            terms = [row["description"]] + [s for s in row["synonyms"].split(";") if s.strip()]
            rows.append({"code": row["code"], "description": row["description"],
                         "terms": [_norm(t) for t in terms]})
    return rows


STOPWORDS = {"of", "the", "a", "an", "and", "in", "on", "with", "to", "for", "is", "has", "patient"}
# Describing words that don't change the code much. Ignored unless the code's own text uses them.
MODIFIERS = {"acute", "chronic", "mild", "moderate", "severe", "right", "left", "bilateral",
             "recurrent", "early", "known", "uncontrolled", "poorly", "controlled", "unspecified",
             "attack", "exacerbation", "flare", "flareup", "episode", "worsening", "suspected", "probable"}


def _tokens(text: str) -> list[str]:
    return [t for t in text.split() if t not in STOPWORDS]


def _token_score(phrase: str, term: str) -> float:
    """
    Word-overlap score (0-100). Mostly asks "are all the words the doctor used covered?"
    (so "type 2 diabetes mellitus" prefers the type-2 code over plain "diabetes"),
    and a little "does the code have extra words?". Small spelling differences count as matches.
    """
    t = _tokens(term)
    if all(w in MODIFIERS for w in _tokens(phrase)):
        return 0.0
    p = [w for w in _tokens(phrase) if w not in MODIFIERS or w in t]
    if not p or not t:
        return 0.0
    matched = sum(1 for a in p if any(a == b or (len(a) > 4 and fuzz.ratio(a, b) >= 85) for b in t))
    recall = matched / len(p)
    precision = min(matched / len(t), 1.0)
    return 100 * (0.7 * recall + 0.3 * precision)


def match_code(*phrases: str) -> dict | None:
    """Find the best ICD-10 code for one or more ways of saying the same thing."""
    best = None
    for phrase in phrases:
        p = _norm(phrase)
        if not p:
            continue
        for row in load_codes():
            for term in row["terms"]:
                if p == term:
                    score = 100
                elif len(term) <= 4 or len(p) <= 4:
                    continue  # short abbreviations only count as exact matches
                else:
                    score = _token_score(p, term)
                    # whole-phrase spelling similarity only counts when it's nearly identical
                    # (otherwise "chest infection" would look like "ear infection")
                    whole = fuzz.ratio(p, term)
                    if whole >= 90:
                        score = max(score, whole)
                if best is None or score > best["score"]:
                    best = {"code": row["code"], "description": row["description"],
                            "score": int(score), "matched_on": phrase}
    if best is None or best["score"] < config.CODE_MIN_SCORE:
        return None
    return best


def assign_codes(extracted: dict) -> dict:
    """
    Returns {"codes": [...], "not_coded": [...]}
    - confirmed or existing diagnoses get coded
    - suspected / ruled-out diagnoses are NOT coded (listed in not_coded with the reason)
    - if no diagnosis is confirmed, the main symptoms are coded instead (normal coding practice)
    """
    codes, not_coded, seen = [], [], set()

    def add(source, label, said, kind):
        m = match_code(label, said)
        if m is None:
            not_coded.append({"item": label or said, "reason": "No matching code in the code list. A coder should assign it."})
            return
        if m["code"] in seen:
            return
        seen.add(m["code"])
        codes.append({
            **m,
            "for": label or said,
            "source": source,
            "kind": kind,
            "needs_review": m["score"] < config.CODE_AUTO_ACCEPT,
        })

    confirmed = 0
    for d in extracted.get("diagnoses", []):
        if d["status"] in ("confirmed", "existing"):
            add("diagnosis", d["name"], d.get("doctor_words", ""),
                "Primary diagnosis" if d["status"] == "confirmed" else "Existing condition")
            confirmed += d["status"] == "confirmed"
        elif d["status"] == "suspected":
            not_coded.append({"item": d["name"], "reason": "Only suspected. Not coded until confirmed (e.g. by test results)."})
        else:
            not_coded.append({"item": d["name"], "reason": "Ruled out by the doctor."})

    if confirmed == 0:
        for s in extracted.get("symptoms", []):
            add("symptom", s["name"], s.get("patient_words", ""), "Symptom (no confirmed diagnosis)")

    return {"codes": codes, "not_coded": not_coded}
