"""
The web server. Start it with:
    uvicorn app.main:app --reload
then open http://localhost:8000 in your browser.
API documentation is automatically available at http://localhost:8000/docs
"""
import re
import shutil
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import billing, config, database, live, pipeline, symptom_check
from .llm import LLMError, ollama_status

app = FastAPI(title="MediClear", description="Doctor-patient conversations to notes, codes and bills")
database.init_db()

ALLOWED = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus",
           ".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp", ".txt"}


@app.get("/api/health")
def health():
    """Shows whether the AI model and Whisper are ready."""
    try:
        import faster_whisper  # noqa: F401
        whisper_ok = True
    except ImportError:
        whisper_ok = False
    return {"ollama": ollama_status(), "whisper_installed": whisper_ok, "whisper_model": config.WHISPER_MODEL}


@app.get("/api/samples")
def samples():
    folder = config.DATA_DIR / "samples"
    return [{"name": p.stem.split("_", 1)[-1].replace("_", " "), "text": p.read_text(encoding="utf-8")}
            for p in sorted(folder.glob("*.txt"))]


@app.get("/api/samples/patient")
def patient_samples():
    folder = config.DATA_DIR / "samples_patient"
    return [{"name": p.stem.split("_", 1)[-1].replace("_", " "), "text": p.read_text(encoding="utf-8").strip()}
            for p in sorted(folder.glob("*.txt"))]


def save_upload(file: UploadFile | None) -> tuple[Path | None, str | None]:
    """Save an uploaded recording to disk. Returns (path to the audio, text if it was a .txt file)."""
    if file is None or not file.filename:
        return None, None
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"File type {suffix} isn't supported.")
    if suffix == ".txt":
        return None, file.file.read().decode("utf-8", errors="ignore")
    path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    if path.stat().st_size < 1000:
        path.unlink()
        raise HTTPException(400, "The recording is empty or too short. Check that the microphone is working.")
    return path, None


@app.post("/api/live/{sid}")
async def live_audio(sid: str, request: Request, final: bool = False):
    """Live subtitles: the browser sends new microphone audio (16 kHz, 16-bit) every few seconds."""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid):
        raise HTTPException(400, "Bad session id")
    data = await request.body()
    if len(data) > 16000 * 2 * 120:
        raise HTTPException(413, "Send audio in smaller pieces (at most 2 minutes at a time).")
    try:
        return await run_in_threadpool(live.add_audio, sid, data, final)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Live subtitles failed: {type(e).__name__}: {e}")


@app.delete("/api/live/{sid}")
def live_discard(sid: str):
    live.discard(sid)
    return {"discarded": sid}


@app.post("/api/symptom-check")
def symptom_check_endpoint(file: UploadFile | None = File(None), text: str = Form(""), language: str = Form("")):
    """A patient describes their problem by voice or text; returns possible issues and how urgent it is."""
    saved_path = None
    try:
        saved_path, file_text = save_upload(file)
        if file_text is not None:
            text = file_text
        if saved_path is None and not text.strip():
            raise HTTPException(400, "Record your voice or type your symptoms first.")
        return symptom_check.run(text=text if saved_path is None else None,
                                 file_path=str(saved_path) if saved_path else None,
                                 language=language or None)
    except LLMError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Unexpected error: {type(e).__name__}: {e}. Full details are in the PowerShell window.")
    finally:
        if saved_path and saved_path.exists():
            saved_path.unlink()  # privacy: the recording is deleted once it has been turned into text


@app.post("/api/process")
def process(
    file: UploadFile | None = File(None),
    transcript: str = Form(""),
    patient_name: str = Form(""),
    label_speakers: bool = Form(True),
    source: str = Form(""),
):
    """Upload a recording/video (or paste a transcript) and run the full pipeline."""
    saved_path = None
    try:
        saved_path, file_text = save_upload(file)
        if file_text is not None:
            transcript = file_text
        source = file.filename if file is not None and file.filename else (source.strip() or "Pasted transcript")

        if saved_path is None and not transcript.strip():
            raise HTTPException(400, "Upload a recording or paste a transcript first.")

        result = pipeline.run(
            transcript_text=transcript if saved_path is None else None,
            file_path=str(saved_path) if saved_path else None,
            label_speakers=label_speakers,
        )
        return database.save_new(patient_name.strip(), source, result)

    except LLMError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        # Show the real reason on the web page instead of a bare "500".
        traceback.print_exc()
        raise HTTPException(500, f"Unexpected error: {type(e).__name__}: {e}. Full details are in the PowerShell window.")
    finally:
        # Privacy: delete the recording once it has been turned into text.
        if saved_path and saved_path.exists():
            saved_path.unlink()


@app.get("/api/consultations")
def list_consultations():
    return database.list_all()


@app.get("/api/consultations/{cid}")
def get_consultation(cid: int):
    c = database.get(cid)
    if not c:
        raise HTTPException(404, "Not found")
    return c


class Edits(BaseModel):
    patient_name: str | None = None
    soap: dict | None = None
    patient_summary: str | None = None
    codes: list[dict] | None = None
    bill_lines: list[dict] | None = None


@app.put("/api/consultations/{cid}")
def save_edits(cid: int, edits: Edits):
    """The doctor's corrections before approval."""
    c = get_consultation(cid)
    if c["status"] == "approved":
        raise HTTPException(409, "Already approved. Reopen it to make changes.")
    r = c["result"]
    if edits.soap is not None:
        r["notes"]["soap"] = {k: str(edits.soap.get(k, "")) for k in ("subjective", "objective", "assessment", "plan")}
    if edits.patient_summary is not None:
        r["notes"]["patient_summary"] = edits.patient_summary
    if edits.codes is not None:
        r["coding"]["codes"] = edits.codes
        r["bill"]["diagnosis_codes"] = [x.get("code") for x in edits.codes if x.get("code")]
    if edits.bill_lines is not None:
        for line in edits.bill_lines:
            line["price"] = float(line.get("price") or 0)
        r["bill"]["lines"] = edits.bill_lines
        billing.recalc_total(r["bill"])
    database.update_result(cid, r, edits.patient_name)
    return database.get(cid)


@app.post("/api/consultations/{cid}/approve")
def approve(cid: int):
    c = get_consultation(cid)
    c["result"]["bill"]["status"] = "approved"
    database.update_result(cid, c["result"])
    database.set_status(cid, "approved")
    return database.get(cid)


@app.post("/api/consultations/{cid}/reopen")
def reopen(cid: int):
    c = get_consultation(cid)
    c["result"]["bill"]["status"] = "draft"
    database.update_result(cid, c["result"])
    database.set_status(cid, "draft")
    return database.get(cid)


@app.delete("/api/consultations/{cid}")
def delete_consultation(cid: int):
    database.delete(cid)
    return {"deleted": cid}


# The web page (everything in the /static folder)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")
