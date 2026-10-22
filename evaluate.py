"""
MediClear evaluation: runs every test case and scores the results.

Usage (with the venv active and Ollama running; the web server does NOT need to be running):
    python evaluate.py                 run all test cases
    python evaluate.py asthma          run only cases whose folder name contains "asthma"
    python evaluate.py --no-speakers   skip the "who is speaking" step (faster)

Each test case is a folder in tests/cases/ with:
    transcript.txt  OR  recording.mp3 / .mp4 / .wav ...
    answer_key.txt  (optional: what was really said, to measure transcription accuracy)
    expected.json   (the correct codes, bill, medicines, etc.)

Results are printed and saved to evaluation_results/ as a Markdown table (for your report) and a CSV.
"""
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from app import pipeline
from app.llm import LLMError, ollama_status

ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "tests" / "cases"
OUT_DIR = ROOT / "evaluation_results"
MEDIA = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".mp4", ".mkv", ".mov", ".webm", ".avi", ".3gp"}


# ---------------------------------------------------------------------------
# Transcription accuracy (how many words Whisper got right)
# ---------------------------------------------------------------------------
def _words(text: str) -> list[str]:
    text = re.sub(r"^\s*(doctor|patient)\s*:", " ", text, flags=re.IGNORECASE | re.MULTILINE)
    text = text.lower().replace("-", " ")
    return re.findall(r"[a-z0-9]+", text)


def word_accuracy(reference: str, hypothesis: str) -> float:
    """100% minus the word error rate (substitutions + deletions + insertions)."""
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return 0.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    wer = prev[-1] / len(ref)
    return max(0.0, 1 - wer) * 100


def read_answer_key(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(l for l in lines if re.match(r"^\s*(doctor|patient)\s*:", l, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_case(result: dict, exp: dict) -> list[tuple[str, bool, str]]:
    """Return a list of (check name, passed?, details)."""
    x = result["extracted"]
    codes = {c["code"] for c in result["coding"]["codes"]}
    billed = {l["service_code"] for l in result["bill"]["lines"] if l.get("include", True)}
    checks = []

    for want in exp.get("codes_required", []):
        options = want.split("|")
        ok = any(o in codes for o in options)
        said = ", ".join(f"{d['name']} ({d['status']})" for d in x["diagnoses"]) or "no diagnosis"
        checks.append((f"Code {want}", ok, f"got {sorted(codes) or 'none'}; AI diagnoses: {said}"))
    for bad in exp.get("codes_forbidden", []):
        ok = bad not in codes
        checks.append((f"Not coded: {bad}", ok, "correctly left out" if ok else "wrongly coded"))

    want_services = set(exp.get("bill_services", []))
    checks.append(("Bill services", billed == want_services,
                   f"expected {sorted(want_services)}, got {sorted(billed)}"))
    if "bill_total" in exp:
        total = result["bill"]["total"]
        checks.append(("Bill total", abs(total - exp["bill_total"]) < 0.01, f"expected {exp['bill_total']}, got {total:g}"))

    med_text = " ".join(m.get("name", "") for m in x["medicines"]).lower()
    for med in exp.get("medicines", []):
        checks.append((f"Medicine: {med}", med.lower() in med_text, f"found: {med_text or 'none'}"))

    allergy_text = " ".join(x["allergies"]).lower()
    if exp.get("allergies"):
        for a in exp["allergies"]:
            checks.append((f"Allergy: {a}", a.lower() in allergy_text, f"found: {allergy_text or 'none'}"))
    else:
        none_words = ("none", "no known", "nkda", "denies", "no allerg", "nil")
        ok = not allergy_text or any(w in allergy_text for w in none_words)
        checks.append(("No allergy invented", ok, f"found: {allergy_text or 'none'}"))

    tests_text = " ".join(x["tests_ordered"]).lower().replace(" ", "")
    for t in exp.get("tests_ordered", []):
        checks.append((f"Test ordered: {t}", t.lower().replace(" ", "") in tests_text, f"found: {tests_text or 'none'}"))

    if "visit_type" in exp:
        checks.append(("Visit type", x["visit_type"] == exp["visit_type"],
                       f"expected {exp['visit_type']}, got {x['visit_type']}"
                       + (f" (AI reason: {x['visit_type_reason']})" if x.get("visit_type_reason") else "")))
    return checks


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def find_input(folder: Path):
    media = [p for p in folder.iterdir() if p.suffix.lower() in MEDIA]
    if media:
        return "file", media[0]
    t = folder / "transcript.txt"
    if t.exists():
        return "text", t
    return None, None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    label_speakers = "--no-speakers" not in sys.argv

    status = ollama_status()
    if not status["running"]:
        sys.exit("Ollama isn't running. Start it first (open the Ollama app or run `ollama serve`).")
    if not status["model_downloaded"]:
        sys.exit(f"Model {status['model']} isn't downloaded. Run: ollama pull {status['model']}")

    folders = sorted(p for p in CASES_DIR.iterdir() if p.is_dir())
    if args:
        folders = [f for f in folders if any(a.lower() in f.name.lower() for a in args)]
    if not folders:
        sys.exit("No test cases found.")

    print(f"\nMediClear evaluation · model {status['model']} · {len(folders)} case(s)"
          f"{'' if label_speakers else ' · speakers skipped'}\n")
    rows, details = [], []
    for folder in folders:
        kind, path = find_input(folder)
        exp = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        print(f"▶ {folder.name} ({kind}) … ", end="", flush=True)
        t0 = time.time()
        try:
            if kind == "file":
                result = pipeline.run(file_path=str(path), label_speakers=label_speakers)
            else:
                result = pipeline.run(transcript_text=path.read_text(encoding="utf-8"), label_speakers=label_speakers)
        except (LLMError, ValueError, Exception) as e:  # keep going with the other cases
            print(f"FAILED: {type(e).__name__}: {e}")
            rows.append({"case": folder.name, "passed": 0, "total": 0, "score": 0, "word_acc": "",
                         "time": round(time.time() - t0, 1), "error": str(e)})
            continue

        checks = check_case(result, exp)
        passed = sum(ok for _, ok, _ in checks)
        word_acc = ""
        key = folder / "answer_key.txt"
        if kind == "file" and key.exists():
            word_acc = round(word_accuracy(read_answer_key(key), result["transcript"]["text"]), 1)
        tm = result["timings"]
        row = {
            "case": folder.name, "passed": passed, "total": len(checks),
            "score": round(100 * passed / len(checks)), "word_acc": word_acc,
            "t_transcribe": tm.get("transcription", ""), "t_speakers": tm.get("speakers", ""),
            "t_extract": tm.get("extraction", ""), "time": tm.get("total", round(time.time() - t0, 1)),
            "language": result["transcript"].get("language_name", ""), "error": "",
        }
        rows.append(row)
        details.append((folder.name, exp.get("description", ""), checks))
        ai = tm.get("ai") or {}
        row.update({"prompt_tokens": ai.get("prompt_tokens", ""), "prompt_s": ai.get("prompt_seconds", ""),
                    "output_tokens": ai.get("output_tokens", ""), "output_s": ai.get("output_seconds", ""),
                    "tok_per_s": ai.get("output_tokens_per_sec", ""), "load_s": ai.get("load_seconds", "")})
        print(f"{passed}/{len(checks)} checks · {row['time']}s"
              + (f" · words {word_acc}%" if word_acc != "" else ""))
        if ai:
            print(f"     AI: read {ai.get('prompt_tokens')} tokens in {ai.get('prompt_seconds')}s, "
                  f"wrote {ai.get('output_tokens')} tokens in {ai.get('output_seconds')}s "
                  f"({ai.get('output_tokens_per_sec')}/s), model loading {ai.get('load_seconds')}s")
        for name, ok, info in checks:
            if not ok:
                print(f"     ✗ {name}: {info}")

    # ---- summary
    ok_rows = [r for r in rows if not r["error"]]
    total_pass = sum(r["passed"] for r in ok_rows)
    total_checks = sum(r["total"] for r in ok_rows)
    overall = round(100 * total_pass / total_checks) if total_checks else 0
    avg_time = round(sum(r["time"] for r in ok_rows) / len(ok_rows), 1) if ok_rows else 0
    accs = [r["word_acc"] for r in ok_rows if r["word_acc"] != ""]
    avg_acc = round(sum(accs) / len(accs), 1) if accs else None

    print("\n" + "=" * 60)
    print(f"OVERALL: {total_pass}/{total_checks} checks passed ({overall}%) · average {avg_time}s per case"
          + (f" · transcription word accuracy {avg_acc}%" if avg_acc is not None else ""))
    print("=" * 60)

    # ---- save
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    md = OUT_DIR / f"results_{stamp}.md"
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# MediClear evaluation, {stamp.replace('_', ' ')}\n\n")
        f.write(f"Model: `{status['model']}` · Speakers step: {'on' if label_speakers else 'off'}\n\n")
        f.write(f"**Overall: {total_pass}/{total_checks} checks passed ({overall}%) · average {avg_time} s per case")
        f.write(f" · transcription word accuracy {avg_acc}%**\n\n" if avg_acc is not None else "**\n\n")
        f.write("| Case | Checks passed | Score | Word accuracy | Transcribe (s) | AI steps (s) | Total (s) | AI tokens written | AI speed (tokens/s) |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            if r["error"]:
                f.write(f"| {r['case']} | error | 0% | | | | {r['time']} |\n")
                continue
            ai = round((r["t_speakers"] or 0) + (r["t_extract"] or 0), 1)
            f.write(f"| {r['case']} | {r['passed']}/{r['total']} | {r['score']}% | "
                    f"{str(r['word_acc']) + '%' if r['word_acc'] != '' else '(text input)'} | "
                    f"{r['t_transcribe'] or '-'} | {ai} | {r['time']} | {r.get('output_tokens', '')} | {r.get('tok_per_s', '')} |\n")
        f.write("\n## Details\n")
        for name, desc, checks in details:
            f.write(f"\n### {name}\n{desc}\n\n")
            for cname, ok, info in checks:
                f.write(f"- {'✅' if ok else '❌'} {cname}" + ("" if ok else f" ({info})") + "\n")
        for r in rows:
            if r["error"]:
                f.write(f"\n### {r['case']}\n❌ Error: {r['error']}\n")
    with open(OUT_DIR / f"results_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        fields = ["case", "passed", "total", "score", "word_acc", "t_transcribe", "t_speakers",
                  "t_extract", "time", "prompt_tokens", "prompt_s", "output_tokens", "output_s", "tok_per_s",
                  "load_s", "language", "error"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved: {md.relative_to(ROOT)} (and .csv)\n")


if __name__ == "__main__":
    main()
