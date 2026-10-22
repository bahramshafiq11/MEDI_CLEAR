"""
Safety checks after the AI has extracted the information.

Hallucination check: every medicine and diagnosis the AI reported should actually appear
in the conversation. If it doesn't, the doctor is warned, because the AI may have made it up.
"""
import re

from rapidfuzz import fuzz


def _found(phrase: str, transcript: str, min_score: int = 85) -> bool:
    phrase = (phrase or "").lower().strip()
    if len(phrase) < 3:
        return True  # too short to check reliably
    return fuzz.partial_ratio(phrase, transcript) >= min_score


def grounding_warnings(extracted: dict, transcript_text: str) -> list[str]:
    t = transcript_text.lower()
    warnings = []
    for m in extracted.get("medicines", []):
        said = [m.get("name", ""), m.get("heard_as", "")]
        # also accept the first word only ("Salbutamol" for "Salbutamol inhaler")
        said += [s.split()[0] for s in said if s.strip()]
        if not any(_found(s, t) for s in said if s):
            warnings.append(f"Medicine \"{m.get('name')}\" was not found in the conversation. "
                            "Check that the AI didn't add it by mistake.")
    for d in extracted.get("diagnoses", []):
        words, name = (d.get("doctor_words") or "").strip(), (d.get("name") or "").strip()
        if not ((words and _found(words, t, 75)) or (name and _found(name, t, 80))):
            warnings.append(f"Diagnosis \"{d.get('name')}\" was not found in the conversation. "
                            "Check that the AI didn't add it by mistake.")
    return warnings


def correction_warnings(medicines: list[dict]) -> list[str]:
    return [f"Medicine name corrected: heard \"{m['heard_as']}\", changed to \"{m['name']}\". Please confirm."
            for m in medicines if m.get("heard_as")]


# ---------------------------------------------------------------------------
# Visit type: a "follow-up" needs real signs that the patient is returning today
# ---------------------------------------------------------------------------
RETURN_CUES = ("welcome back", "last time", "last visit", "since i saw you", "since we last", "since your last",
               "report", "results", "came back", "back again", "previous visit", "the medicine i gave",
               "the medicines i gave", "i asked for", "how is the", "is it better now")
FUTURE_WORDS = ("come back", "follow up in", "follow-up in", "next week", "next visit", "next time", "return in",
                "see you in", "after three months", "in two weeks", "in one week", "in a week")


def confirm_visit_type(extracted: dict, transcript_text: str) -> str | None:
    """Turns a follow-up back into a new visit when there is no sign of a return visit. Returns a note or None."""
    if extracted.get("visit_type") != "follow_up":
        return None
    reason = (extracted.get("visit_type_reason") or "").lower()
    text = transcript_text.lower()
    # whole words only: "welcome back" must not count as "come back"
    about_future = any(re.search(r"\b" + re.escape(w) + r"\b", reason) for w in FUTURE_WORDS)
    has_cue = any(re.search(r"\b" + re.escape(c), text) for c in RETURN_CUES)
    if about_future or not has_cue:
        extracted["visit_type"] = "new"
        extracted["visit_type_reason"] = ""
        return ("Visit set to 'new': the conversation doesn't show the patient returning today "
                "(a follow-up planned for later doesn't count).")
    return None
