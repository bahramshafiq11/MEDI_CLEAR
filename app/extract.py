"""
Uses the AI model to understand the conversation, in ONE step:
  extract_info() -> who said each line (Doctor / Patient) + symptoms, diagnoses, medicines, services, etc.

Reading the conversation once (instead of once for speakers and again for the medical facts)
roughly halves the AI time on a normal computer.
(The SOAP note and patient summary are built from these facts in notes.py, without AI.)
"""
import re

from . import billing
from .llm import ask_json

SPEAKER_PREFIX = re.compile(r"^\s*(doctor|dr\.?|patient|pt\.?|nurse|attendant|relative)\s*:\s*(.*)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Lines and speakers
# ---------------------------------------------------------------------------
def _normalize_speaker(s: str) -> str:
    s = (s or "").strip().lower().rstrip(".")
    if s in ("doctor", "dr"):
        return "Doctor"
    if s in ("patient", "pt"):
        return "Patient"
    if s in ("nurse", "attendant", "relative", "other"):
        return "Other"
    return "Unknown"


def split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_labelled(lines: list[str]) -> list[dict] | None:
    """If the transcript already says "Doctor:" / "Patient:", use that. Otherwise return None."""
    prefixed = [SPEAKER_PREFIX.match(l) for l in lines]
    if not lines or sum(1 for m in prefixed if m) < len(lines) * 0.6:
        return None
    dialogue = []
    for line, m in zip(lines, prefixed):
        if m:
            dialogue.append({"speaker": _normalize_speaker(m.group(1)), "text": m.group(2).strip()})
        elif dialogue:  # continuation of the previous speaker
            dialogue[-1]["text"] += " " + line
    return dialogue


def dialogue_to_text(dialogue: list[dict]) -> str:
    return "\n".join(f"{d['speaker']}: {d['text']}" for d in dialogue)


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = """You are a careful medical scribe. You read a doctor-patient conversation and extract
ONLY information that was actually said. Never invent anything. Ignore small talk (family, traffic, weather).
People may use everyday words ("sugar problem", "loose motions") or medical terms and abbreviations
("HTN", "DM", "c/o", "TDS"). Understand both. Reply ONLY with JSON."""

EXTRACT_TEMPLATE = """Extract the information from this consultation into this exact JSON shape:

{{
{speakers_field}  "visit_type": "new" or "follow_up",
  "visit_type_reason": "if follow_up: the words showing the patient is RETURNING today (e.g. 'welcome back', 'you brought the reports'); otherwise empty",
  "chief_complaint": "main problem in one short sentence",
  "symptoms": [{{"name": "standard medical name in English", "patient_words": "how it was said", "duration": "e.g. 5 days, or empty"}}],
  "history": ["past illnesses or long-term conditions mentioned"],
  "allergies": ["drug or other allergies; empty list if none or denied"],
  "vitals": [{{"name": "e.g. Blood pressure", "value": "e.g. 160/100 mmHg"}}],
  "exam_findings": ["what the doctor found on examination"],
  "diagnoses": [{{"name": "short standard name, e.g. Asthma, Hypertension, Urinary tract infection, Laceration of hand", "doctor_words": "exact words used", "status": "confirmed | suspected | ruled_out | existing"}}],
  "medicines": [{{"name": "generic drug name, correctly spelled", "dose": "e.g. 500 mg", "frequency": "as said, e.g. TDS or twice a day", "duration": "", "instructions": "", "is_new": true}}],
  "tests_ordered": ["tests the patient must get done LATER or at a lab"],
  "services_performed": ["items from the BILLABLE SERVICES list that were done RIGHT THERE during this visit"],
  "follow_up": "when to come back, or empty",
  "advice": ["lifestyle advice and warning signs given"]
}}

BILLABLE SERVICES (copy the names exactly):
{services}

Rules:
{speakers_rule}- visit_type: "follow_up" ONLY if the patient is coming back today about a problem the doctor already saw
  (e.g. bringing test reports, "welcome back", checking on earlier treatment).
  A follow-up PLANNED for the future ("come back in two weeks") does NOT make today's visit a follow-up.
  Being a known patient with a long-term illness does NOT make it a follow-up either. Default: "new".
- diagnoses.status: "confirmed" only if the doctor clearly states it. "suspected" for maybe / could be / likely / let's test.
  "ruled_out" if the doctor says it's not that. "existing" for an already known long-term condition (e.g. known diabetic).
  Use the plain disease name: write "Asthma", not "Acute asthma exacerbation"; details go in doctor_words.
  If the doctor treats an injury or problem without naming it (e.g. stitches a cut), still add it as a confirmed
  diagnosis, e.g. "Laceration of hand".
- services_performed: tests and procedures done in the clinic during this visit ARE performed, for example
  a glucometer sugar check, a urine dipstick, an ECG, a nebulization, an injection, stitches or a dressing.
  Do NOT add consultation, examination, history taking or vitals (they are part of the consultation fee).
  A test the patient must get done later or at a lab goes in tests_ordered, NOT here.
- medicines: the speech-to-text may misspell drug names (e.g. "pridness alone" = prednisolone). Write the real drug name.
- Use empty lists or empty strings when something wasn't mentioned.

Conversation:
{transcript}"""

SPEAKERS_FIELD = '  "speakers": ["Doctor", "Patient" or "Other" for EACH numbered line, in order: exactly {n} items],\n'
SPEAKERS_RULE = ("- speakers: label every numbered line. Doctors ask questions, examine, explain and prescribe;\n"
                 "  patients describe their problems and answer questions.\n")


def billable_services() -> list[str]:
    """Names from the fee schedule the AI may choose from (consultations and free items are handled separately)."""
    return [f["service_name"] for f in billing.load_fees()
            if not f["service_code"].startswith("CONS") and f["service_code"] != "INCLUDED"]


# ---------------------------------------------------------------------------
# The answer shape (Ollama forces the model to follow it: "structured output")
# ---------------------------------------------------------------------------
def _str_list():
    return {"type": "array", "items": {"type": "string"}}


def _obj_list(props: dict, required: list[str]):
    return {"type": "array", "items": {"type": "object", "properties": props, "required": required}}


def extraction_schema(n_speakers: int = 0) -> dict:
    s = {"type": "string"}
    props = {
        "visit_type": {"type": "string", "enum": ["new", "follow_up"]},
        "visit_type_reason": s,
        "chief_complaint": s,
        "symptoms": _obj_list({"name": s, "patient_words": s, "duration": s}, ["name"]),
        "history": _str_list(),
        "allergies": _str_list(),
        "vitals": _obj_list({"name": s, "value": s}, ["name", "value"]),
        "exam_findings": _str_list(),
        "diagnoses": _obj_list({"name": s, "doctor_words": s,
                                "status": {"type": "string", "enum": ["confirmed", "suspected", "ruled_out", "existing"]}},
                               ["name", "doctor_words", "status"]),
        "medicines": _obj_list({"name": s, "dose": s, "frequency": s, "duration": s, "instructions": s,
                                "is_new": {"type": "boolean"}}, ["name", "dose", "frequency"]),
        "tests_ordered": _str_list(),
        "services_performed": {"type": "array", "items": {"type": "string", "enum": billable_services()}},
        "follow_up": s,
        "advice": _str_list(),
    }
    required = list(props)
    if n_speakers:
        props = {"speakers": {"type": "array", "items": {"type": "string", "enum": ["Doctor", "Patient", "Other"]},
                              "minItems": n_speakers, "maxItems": n_speakers}, **props}
        required = ["speakers"] + required
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# Tidy up the answer
# ---------------------------------------------------------------------------
def _as_list(v):
    return v if isinstance(v, list) else ([] if v in (None, "") else [v])


def _as_str(v):
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def _dict_list(v, keys):
    """Make sure every item is a dict that has all the expected keys."""
    out = []
    for item in _as_list(v):
        if isinstance(item, str):
            item = {keys[0]: item}
        if isinstance(item, dict):
            out.append({k: item.get(k, "") if k != "is_new" else bool(item.get(k, True)) for k in keys})
    return out


def clean_extraction(raw: dict) -> dict:
    """Small AI models sometimes return slightly wrong shapes; fix them up."""
    diagnoses = _dict_list(raw.get("diagnoses"), ["name", "doctor_words", "status"])
    for d in diagnoses:
        status = _as_str(d["status"]).lower().replace("-", "_").replace(" ", "_")
        d["status"] = status if status in ("confirmed", "suspected", "ruled_out", "existing") else "suspected"
    visit = _as_str(raw.get("visit_type")).lower().replace("-", "_")
    reason = _as_str(raw.get("visit_type_reason"))
    is_follow_up = "follow" in visit and bool(reason)  # no evidence of a return visit -> treat as new
    return {
        "visit_type": "follow_up" if is_follow_up else "new",
        "visit_type_reason": reason if is_follow_up else "",
        "chief_complaint": _as_str(raw.get("chief_complaint")),
        "symptoms": _dict_list(raw.get("symptoms"), ["name", "patient_words", "duration"]),
        "history": [_as_str(x) for x in _as_list(raw.get("history")) if _as_str(x)],
        "allergies": [_as_str(x) for x in _as_list(raw.get("allergies")) if _as_str(x)],
        "vitals": _dict_list(raw.get("vitals"), ["name", "value"]),
        "exam_findings": [_as_str(x) for x in _as_list(raw.get("exam_findings")) if _as_str(x)],
        "diagnoses": diagnoses,
        "medicines": _dict_list(raw.get("medicines"), ["name", "dose", "frequency", "duration", "instructions", "is_new"]),
        "tests_ordered": [_as_str(x) for x in _as_list(raw.get("tests_ordered")) if _as_str(x)],
        "services_performed": [_as_str(x) for x in _as_list(raw.get("services_performed")) if _as_str(x)],
        "follow_up": _as_str(raw.get("follow_up")),
        "advice": [_as_str(x) for x in _as_list(raw.get("advice")) if _as_str(x)],
    }


# ---------------------------------------------------------------------------
# The one AI step
# ---------------------------------------------------------------------------
def extract_info(lines: list[str], label_speakers: bool = True) -> tuple[dict, list[dict]]:
    """
    Returns (extracted facts, dialogue).
    dialogue = [{"speaker": "Doctor", "text": "..."}, ...]
    """
    services = "\n".join(f"- {name}" for name in billable_services())
    dialogue = parse_labelled(lines)

    if dialogue is not None:
        # Speakers are already written in the transcript: nothing to label.
        transcript, n = dialogue_to_text(dialogue), 0
    elif label_speakers:
        transcript, n = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines)), len(lines)
    else:
        transcript, n = "\n".join(lines), 0

    prompt = EXTRACT_TEMPLATE.format(
        transcript=transcript, services=services,
        speakers_field=SPEAKERS_FIELD.format(n=n) if n else "",
        speakers_rule=SPEAKERS_RULE if n else "",
    )
    raw = ask_json(EXTRACT_SYSTEM, prompt, schema=extraction_schema(n))

    if dialogue is None:
        labels = raw.get("speakers") if n else None
        labels = labels if isinstance(labels, list) else []
        dialogue = [{"speaker": _normalize_speaker(labels[i]) if i < len(labels) and isinstance(labels[i], str) else "Unknown",
                     "text": t} for i, t in enumerate(lines)]
    return clean_extraction(raw), dialogue
