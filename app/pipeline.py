"""
The full journey of one consultation:

  recording / transcript
      -> speech to text (Whisper; other languages are translated to English)
      -> who said what + medical information (ONE AI step, in a fixed answer format)
      -> fix misheard drug names + check nothing was invented (hallucination check)
      -> ICD-10 codes (code list + fuzzy matching)
      -> SOAP note + simple patient summary (template, no AI)
      -> highlight medical words with plain meanings (glossary)
      -> draft bill (fee schedule)
"""
import time

from . import billing, checks, coding, extract, llm, medicines, notes, simplify


def run(transcript_text: str | None = None, file_path: str | None = None, label_speakers: bool = True) -> dict:
    timings = {}
    warnings = []  # notes for the doctor, shown at the top of the result
    t0 = time.time()

    # 1. Speech to text
    if file_path:
        from .transcribe import transcribe_file  # imported here so the app starts fast
        tr = transcribe_file(file_path)
        lines = [s["text"] for s in tr["segments"]]
        transcript = {"text": tr["text"], "language": tr["language"], "language_name": tr["language_name"],
                      "translated": tr["translated"], "segments": tr["segments"]}
        if tr["warning"]:
            warnings.append(tr["warning"])
        timings["transcription"] = round(time.time() - t0, 1)
    else:
        lines = extract.split_lines(transcript_text or "")
        transcript = {"text": "\n".join(lines), "language": None, "segments": []}

    if not lines:
        raise ValueError("The transcript is empty. Nothing was heard in the recording, or no text was pasted.")

    # 2-3. Who said what + medical information, in ONE AI step
    t = time.time()
    extracted, dialogue = extract.extract_info(lines, label_speakers=label_speakers)
    timings["extraction"] = round(time.time() - t, 1)
    timings["speakers"] = 0  # now part of the extraction step
    timings["ai"] = dict(llm.last_stats)  # tokens read/written and speed, from Ollama

    # Safety: fix misheard drug names, and check nothing was invented
    extracted["medicines"] = medicines.correct_medicines(extracted["medicines"])
    warnings += checks.correction_warnings(extracted["medicines"])
    warnings += checks.grounding_warnings(extracted, transcript["text"])
    note = checks.confirm_visit_type(extracted, transcript["text"])
    if note:
        warnings.append(note)

    # 4-7. Codes, notes, plain language, bill (fixed rules, no AI, takes milliseconds)
    codes = coding.assign_codes(extracted)
    medicines_plain = simplify.plain_medicines(extracted["medicines"])
    written = notes.build_notes(extracted, codes, medicines_plain)
    soap_text = " ".join(written["soap"].values())
    glossary_hits = simplify.find_terms(transcript["text"] + "\n" + soap_text)
    bill = billing.build_bill(extracted, codes)
    bill = billing.add_missed_services(bill, transcript["text"])
    timings["total"] = round(time.time() - t0, 1)

    return {
        "transcript": transcript,
        "dialogue": dialogue,
        "extracted": extracted,
        "medicines_plain": medicines_plain,
        "notes": written,
        "glossary_hits": glossary_hits,
        "coding": codes,
        "bill": bill,
        "timings": timings,
        "warnings": warnings,
    }
