"""
Builds the SOAP note and the simple patient summary from the extracted facts,
using fixed templates (no AI).

Why a template instead of asking the AI again?
  - Faster: it takes milliseconds instead of a whole extra AI step.
  - Safer: the note can only contain facts that were extracted from the conversation,
    so the AI gets no second chance to add things that weren't said.
"""
from . import simplify

STATUS_TEXT = {
    "confirmed": "",
    "existing": " (known / existing)",
    "suspected": " (suspected, not confirmed)",
}


def _join(items, sep="; "):
    return sep.join(i for i in items if i)


def _medicine_line(m: dict) -> str:
    parts = [m.get("name", ""), m.get("dose", ""), m.get("frequency", "")]
    line = " ".join(p for p in parts if p)
    if m.get("duration"):
        line += f" for {m['duration']}"
    if m.get("instructions"):
        line += f" ({m['instructions']})"
    line += " [new]" if m.get("is_new", True) else " [continued]"
    return line


def _code_for(name: str, coding: dict) -> str:
    for c in coding.get("codes", []):
        if c.get("for", "").lower() == (name or "").lower():
            return f" [{c['code']}]"
    return ""


def build_soap(x: dict, coding: dict) -> dict:
    # S: what the patient reports
    s = []
    if x["chief_complaint"]:
        s.append(f"Chief complaint: {x['chief_complaint']}.")
    if x["symptoms"]:
        sym = [f"{y['name']}" + (f" ({y['duration']})" if y.get("duration") else "") for y in x["symptoms"]]
        s.append("Symptoms: " + _join(sym, ", ") + ".")
    if x["history"]:
        s.append("History: " + _join(x["history"]) + ".")
    s.append("Allergies: " + (_join(x["allergies"], ", ") if x["allergies"] else "none reported") + ".")

    # O: what was measured / found
    o = []
    if x["vitals"]:
        o.append("Vitals: " + _join([f"{v['name']} {v['value']}".strip() for v in x["vitals"]]) + ".")
    if x["exam_findings"]:
        o.append("Examination: " + _join(x["exam_findings"]) + ".")
    if not o:
        o.append("No vitals or examination findings recorded.")

    # A: diagnoses
    a, ruled_out = [], []
    for d in x["diagnoses"]:
        if d["status"] == "ruled_out":
            ruled_out.append(d["name"])
        else:
            a.append(f"{d['name']}{STATUS_TEXT.get(d['status'], '')}{_code_for(d['name'], coding)}")
    a = [f"{i}. {line}" for i, line in enumerate(a, 1)]
    if ruled_out:
        a.append("Ruled out: " + _join(ruled_out, ", ") + ".")
    if not a:
        a.append("No diagnosis stated. Symptoms under evaluation.")

    # P: what happens next
    p = []
    if x["medicines"]:
        p.append("Medicines:\n" + "\n".join(f"- {_medicine_line(m)}" for m in x["medicines"]))
    if x["tests_ordered"]:
        p.append("Tests: " + _join(x["tests_ordered"], ", ") + ".")
    procedures = [sv for sv in x["services_performed"] if sv.strip().lower() != "consultation"]
    if procedures:
        p.append("Done in clinic: " + _join(procedures, ", ") + ".")
    if x["advice"]:
        p.append("Advice: " + _join(x["advice"]) + ".")
    p.append("Follow-up: " + (x["follow_up"] or "not specified") + ".")

    return {
        "subjective": "\n".join(s),
        "objective": "\n".join(o),
        "assessment": "\n".join(a),
        "plan": "\n".join(p),
    }


def explain(text: str) -> str:
    """Add plain meanings after medical words: 'Hypertension' -> 'Hypertension (high blood pressure)'."""
    out = text
    for hit in simplify.find_terms(text):
        pattern = simplify._pattern(hit["term"])
        m = pattern.search(out)
        if m and hit["meaning"].lower() not in out.lower():
            out = out[:m.end()] + f" ({hit['meaning']})" + out[m.end():]
    return out


def build_patient_summary(x: dict, medicines_plain: list[dict]) -> str:
    lines = []
    if x["chief_complaint"]:
        lines.append(f"You came in because of: {x['chief_complaint'].rstrip('.')}.")

    confirmed = [d["name"] for d in x["diagnoses"] if d["status"] in ("confirmed", "existing")]
    suspected = [d["name"] for d in x["diagnoses"] if d["status"] == "suspected"]
    if confirmed:
        lines.append("What the doctor found: " + explain(_join(confirmed, ", ")) + ".")
    if suspected:
        lines.append("The doctor thinks it might be " + explain(_join(suspected, " or "))
                     + ", but this is not confirmed yet. The tests will help decide.")
    if not confirmed and not suspected:
        lines.append("The doctor has not given a final diagnosis yet.")

    if medicines_plain:
        lines.append("Your medicines:")
        for m in medicines_plain:
            lines.append(f"  • {m.get('name', '')}: {m.get('plain', '') or 'as the doctor told you'}")

    if x["tests_ordered"]:
        lines.append("Tests to get done: " + explain(_join(x["tests_ordered"], ", ")) + ".")
    if x["advice"]:
        lines.append("Things to do and warning signs:")
        for adv in x["advice"]:
            lines.append(f"  • {explain(adv)}")
    if x["follow_up"]:
        lines.append(f"Come back: {x['follow_up'].rstrip('.')}.")
    if x["allergies"]:
        lines.append("Remember your allergy: " + _join(x["allergies"], ", ") + ". Tell every doctor about it.")
    return "\n".join(lines)


def build_notes(extracted: dict, coding: dict, medicines_plain: list[dict]) -> dict:
    return {
        "soap": build_soap(extracted, coding),
        "patient_summary": build_patient_summary(extracted, medicines_plain),
    }
