"""
Live subtitles: turns speech into text WHILE the person is still talking.

How it works:
  - Every few seconds the browser sends the newest bit of microphone audio (16 kHz, 16-bit).
  - The server keeps the audio that hasn't been turned into finished text yet.
  - Whisper reads only that unfinished part. Lines that ended a few seconds ago are "finished"
    (committed) and their audio is thrown away; the last line may still change ("tentative").
  So each update only processes a few seconds of audio, however long the recording is.

Everything stays in memory and is deleted when the recording stops (or after 30 minutes).
"""
import threading
import time

import numpy as np

from . import config
from .transcribe import MODEL_LOCK, SAMPLE_RATE, _get_model, language_name, vocabulary_hint

SESSIONS: dict[str, "Session"] = {}
SESSIONS_LOCK = threading.Lock()
EXPIRE_AFTER = 30 * 60   # seconds of inactivity before a session is deleted
SETTLE = 2.5             # a line is finished when it ended this many seconds before the newest audio
MIN_AUDIO = 1.0          # don't bother transcribing less than this many seconds


class Session:
    def __init__(self):
        self.pcm = np.zeros(0, dtype=np.float32)  # audio not yet finished
        self.offset = 0.0                         # seconds of audio already finished and dropped
        self.received = 0.0                       # total seconds received
        self.committed: list[str] = []            # finished lines
        self.tentative = ""                       # last line, may still change
        self.language = config.WHISPER_LANGUAGE
        self.language_checked = config.WHISPER_LANGUAGE is not None
        self.lock = threading.Lock()
        self.touched = time.time()

    def state(self, final=False) -> dict:
        lines = self.committed + ([self.tentative] if self.tentative else [])
        return {
            "committed": self.committed,
            "tentative": self.tentative,
            "text": "\n".join(lines),
            "language": self.language,
            "language_name": language_name(self.language) if self.language else "",
            "seconds": round(self.received, 1),
            "final": final,
        }


def _cleanup():
    now = time.time()
    with SESSIONS_LOCK:
        for sid in [k for k, s in SESSIONS.items() if now - s.touched > EXPIRE_AFTER]:
            SESSIONS.pop(sid, None)


def discard(sid: str):
    with SESSIONS_LOCK:
        SESSIONS.pop(sid, None)


def add_audio(sid: str, data: bytes, final: bool = False) -> dict:
    """Add new audio for a recording and return the subtitles so far."""
    _cleanup()
    with SESSIONS_LOCK:
        s = SESSIONS.setdefault(sid, Session())
    samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    with s.lock:
        s.pcm = np.concatenate([s.pcm, samples])
        s.received += len(samples) / SAMPLE_RATE
        s.touched = time.time()

    # While recording, skip this update if Whisper is busy (the next one will catch up).
    # When the recording stops ("final"), wait so that nothing is lost.
    if not MODEL_LOCK.acquire(blocking=final):
        return s.state()
    try:
        _transcribe_pending(s, final)
    finally:
        MODEL_LOCK.release()

    result = s.state(final)
    if final:
        discard(sid)
    return result


def _transcribe_pending(s: Session, final: bool):
    with s.lock:
        audio = s.pcm.copy()
    length = len(audio) / SAMPLE_RATE
    if length < MIN_AUDIO and not final:
        return
    if length < 0.3:
        s.tentative = ""
        return
    model = _get_model()

    # Find out the language once, as soon as there are a few seconds of speech
    if not s.language_checked and (length >= 3 or final):
        try:
            lang, prob, _ = model.detect_language(audio=audio[: 30 * SAMPLE_RATE])
        except Exception:
            lang, prob = None, 0
        if lang and (prob >= 0.5 or length >= 8 or final):
            s.language, s.language_checked = lang, True

    segments, _ = model.transcribe(
        audio,
        task="transcribe",              # subtitles show exactly what was said, in its own language
        language=s.language,
        beam_size=1,                    # fastest
        vad_filter=True,                # skip silence
        condition_on_previous_text=False,
        initial_prompt=vocabulary_hint() if s.language in (None, "en") else None,
    )
    segments = list(segments)

    finished, pending, finished_until = [], [], 0.0
    for seg in segments:
        text = seg.text.strip()
        if final or seg.end < length - SETTLE:
            if text:
                finished.append(text)
            finished_until = seg.end
        elif text:
            pending.append(text)
    if not segments and length > 10 and not final:
        finished_until = length - SETTLE  # long silence: drop it

    with s.lock:
        cut = int(finished_until * SAMPLE_RATE)
        s.pcm = s.pcm[cut:]  # new audio may have arrived meanwhile; it stays at the end
        s.offset += finished_until
        s.committed.extend(finished)
        s.tentative = "" if final else " ".join(pending)
