"""
Symptom check: a patient describes their problem (by voice or typing) and the app suggests
what it MIGHT be and how urgently they should get care.

How it stays safe:
  1. Danger signs ("red flags") are found by fixed rules in data/red_flags.json, not by the AI,
     so they are never missed because the AI got something wrong.
  2. The AI may make the urgency HIGHER, but never lower than the red-flag rules.
  3. It never gives a diagnosis, prescription or medicine dose, and always says to see a doctor.
"""
import json
import re
import time
from functools import lru_cache

from . import coding, config
from .llm import LLMError, ask_json

URGENCY_ORDER = ["self_care", "see_doctor_soon", "urgent_today", "emergency"]
URGENCY_TEXT = {
    "emergency": "Get emergency help now",
    "urgent_today": "See a doctor today",
    "see_doctor_soon": "Book a doctor's appointment in the next few days",
    "self_care": "You can probably look after this at home for now",
}
NEGATIONS = re.compile(r"\b(no|not|never|without|denies|don'?t have|do not have|haven'?t had|isn'?t|aren'?t|wasn'?t)\b[^.,;]{0,25}$")
# "no fever BUT my chest is tight": a negation stops at words like "but"
SCOPE_BREAK = re.compile(r"\b(but|however|although|though|except|and now|now)\b|[.,;!?]")


# ---------------------------------------------------------------------------
# 1. Red flags (fixed rules)
# ---------------------------------------------------------------------------
@lru_cache
def load_rules() -> list[dict]:
    with open(config.DATA_DIR / "red_flags.json", encoding="utf-8") as f:
        rules = json.load(f)["rules"]
    for r in rules:
        r["_compiled"] = [re.compile(p, re.IGNORECASE) for p in r["patterns"]]
    return rules


def find_red_flags(text: str) -> list[dict]:
    """Return the danger-sign rules that match what the patient said (ignoring 'no chest pain' etc.)."""
    text = text.lower().replace("’", "'")
    found = []
    for rule in load_rules():
        for pattern in rule["_compiled"]:
            hit = None
            for m in pattern.finditer(text):
                before = SCOPE_BREAK.split(text[max(0, m.start() - 40):m.start()])[-1] or ""
                if not NEGATIONS.search(before):
                    hit = m
                    break
            if hit:
                found.append({"id": rule["id"], "name": rule["name"], "urgency": rule["urgency"],
                              "message": rule["message"], "matched": hit.group(0)})
                break
    return found


def highest(*levels: str) -> str:
    valid = [l for l in levels if l in URGENCY_ORDER]
    return max(valid, key=URGENCY_ORDER.index) if valid else "see_doctor_soon"


# ---------------------------------------------------------------------------
# 2. The AI's suggestions
# ---------------------------------------------------------------------------
CHECK_SYSTEM = """You help a patient understand their symptoms before they see a doctor.
You are NOT a doctor and you never give a diagnosis. You suggest what the problem MIGHT be,
in very simple language, and how urgently they should get care. Use only what the patient said.
When in doubt, choose the MORE urgent option. Reply ONLY with JSON."""

CHECK_TEMPLATE = """The patient said:
\"\"\"{text}\"\"\"

Fill in this JSON:
{{
  "symptoms": [{{"name": "standard medical name", "patient_words": "how they said it", "duration": "", "severity": "mild | moderate | severe | unknown"}}],
  "possible_issues": [{{"name": "standard medical name", "simple_name": "the same thing in everyday words",
                        "likelihood": "more likely | possible | less likely", "why": "one short sentence linking it to what the patient said"}}],
  "urgency": "emergency | urgent_today | see_doctor_soon | self_care",
  "urgency_reason": "one short sentence in simple words",
  "self_care": ["safe things they can do now"],
  "see_doctor_if": ["warning signs that mean they should get help sooner"],
  "doctor_type": "which kind of doctor to see, e.g. general physician, gynaecologist, child specialist",
  "questions_doctor_may_ask": ["2 to 4 questions"],
  "missing_info": ["important things the patient didn't say, e.g. age, how long, fever or not"]
}}

Rules:
- possible_issues: 1 to 4 items, most likely first. Only common, realistic possibilities. Never say it is definitely something.
- urgency: "emergency" for anything that could be life-threatening; "urgent_today" if a doctor should see them today;
  "see_doctor_soon" for problems that need a doctor but can wait a few days; "self_care" only for clearly mild problems.
- self_care: simple, safe steps (rest, fluids, etc.). Do NOT name prescription medicines or give any doses.
  For pain or fever you may say "a simple painkiller like paracetamol, following the instructions on the pack".
- Write everything for someone who is not a doctor: short sentences, no jargon (except in "name").
- If the text is not about a health problem, return empty lists and urgency "see_doctor_soon"."""


def _schema() -> dict:
    s = {"type": "string"}
    lst = {"type": "array", "items": s}
    return {
        "type": "object",
        "properties": {
            "symptoms": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {
                "name": s, "patient_words": s, "duration": s,
                "severity": {"type": "string", "enum": ["mild", "moderate", "severe", "unknown"]}}}},
            "possible_issues": {"type": "array", "maxItems": 4, "items": {"type": "object",
                "required": ["name", "simple_name", "likelihood", "why"], "properties": {
                    "name": s, "simple_name": s, "why": s,
                    "likelihood": {"type": "string", "enum": ["more likely", "possible", "less likely"]}}}},
            "urgency": {"type": "string", "enum": URGENCY_ORDER},
            "urgency_reason": s,
            "self_care": lst, "see_doctor_if": lst, "doctor_type": s,
            "questions_doctor_may_ask": lst, "missing_info": lst,
        },
        "required": ["symptoms", "possible_issues", "urgency", "urgency_reason", "self_care",
                     "see_doctor_if", "doctor_type", "questions_doctor_may_ask", "missing_info"],
    }


def _strs(v) -> list[str]:
    return [str(x).strip() for x in (v if isinstance(v, list) else []) if str(x).strip()]


DOSE = re.compile(r"\b\d+(\.\d+)?\s?(mg|ml|mcg|g|tablets?|tabs?|capsules?|puffs?)\b", re.IGNORECASE)


def _clean(raw: dict) -> dict:
    issues = []
    for i in raw.get("possible_issues") or []:
        if isinstance(i, dict) and str(i.get("name", "")).strip():
            issue = {k: str(i.get(k, "")).strip() for k in ("name", "simple_name", "likelihood", "why")}
            if issue["likelihood"] not in ("more likely", "possible", "less likely"):
                issue["likelihood"] = "possible"
            m = coding.match_code(issue["name"], issue["simple_name"])
            issue["icd10"] = m["code"] if m else ""
            issues.append(issue)
    symptoms = [{k: str(s.get(k, "")).strip() for k in ("name", "patient_words", "duration", "severity")}
                for s in (raw.get("symptoms") or []) if isinstance(s, dict) and str(s.get("name", "")).strip()]
    # Safety: drop any self-care line that slipped in a medicine dose
    self_care = [x for x in _strs(raw.get("self_care")) if not DOSE.search(x)]
    return {
        "symptoms": symptoms,
        "possible_issues": issues[:4],
        "ai_urgency": raw.get("urgency") if raw.get("urgency") in URGENCY_ORDER else "see_doctor_soon",
        "urgency_reason": str(raw.get("urgency_reason", "")).strip(),
        "self_care": self_care,
        "see_doctor_if": _strs(raw.get("see_doctor_if")),
        "doctor_type": str(raw.get("doctor_type", "")).strip(),
        "questions_doctor_may_ask": _strs(raw.get("questions_doctor_may_ask")),
        "missing_info": _strs(raw.get("missing_info")),
    }


# ---------------------------------------------------------------------------
# 3. Put it together
# ---------------------------------------------------------------------------
def run(text: str | None = None, file_path: str | None = None, language: str | None = None) -> dict:
    t0 = time.time()
    transcript = {"text": "", "language": None, "language_name": "", "translated": False}
    warnings = []

    if file_path:
        from .transcribe import transcribe_file
        tr = transcribe_file(file_path)
        transcript.update({k: tr[k] for k in ("text", "language", "language_name", "translated")})
        if tr["warning"]:
            warnings.append(tr["warning"])
    else:
        transcript["text"] = (text or "").strip()
        if language:  # text that came from the live subtitles
            from .transcribe import language_name
            transcript.update({"language": language, "language_name": language_name(language)})

    said = transcript["text"]
    if not said:
        raise ValueError("Nothing was heard. Try again closer to the microphone, or type your symptoms.")

    red_flags = find_red_flags(said)
    rule_urgency = highest(*(f["urgency"] for f in red_flags)) if red_flags else None

    try:
        ai = _clean(ask_json(CHECK_SYSTEM, CHECK_TEMPLATE.format(text=said), schema=_schema()))
    except LLMError as e:
        if not red_flags:
            raise
        # The danger signs don't need the AI, so still show them.
        ai = _clean({})
        warnings.append(f"The AI couldn't analyse the details ({e}), but danger signs were found.")

    # Danger signs also checked in the AI's own words for each symptom (e.g. a translated recording)
    extra = find_red_flags(" . ".join(s["name"] + " . " + s["patient_words"] for s in ai["symptoms"]))
    for f in extra:
        if f["id"] not in {r["id"] for r in red_flags}:
            red_flags.append(f)
    rule_urgency = highest(*(f["urgency"] for f in red_flags)) if red_flags else None

    urgency = highest(ai["ai_urgency"], rule_urgency) if rule_urgency else ai["ai_urgency"]
    if rule_urgency and URGENCY_ORDER.index(rule_urgency) > URGENCY_ORDER.index(ai["ai_urgency"]):
        warnings.append("Urgency was raised because of the danger signs in what you said.")

    return {
        "transcript": transcript,
        **ai,
        "red_flags": red_flags,
        "urgency": urgency,
        "urgency_text": URGENCY_TEXT[urgency],
        "warnings": warnings,
        "seconds": round(time.time() - t0, 1),
    }
