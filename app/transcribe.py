"""
Speech to text with faster-whisper (runs on your computer, free).
Works with audio (mp3, wav, m4a, ogg...) and video (mp4, mkv, mov, webm...).
The first time you run it, the Whisper model is downloaded once (internet needed);
after that it works offline.
"""
import os
import threading

from . import config

# Hide a harmless Windows warning from the model downloader.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

SAMPLE_RATE = 16000  # Whisper expects 16 kHz mono audio
_model = None  # loaded once, then reused
# Only one transcription at a time (live subtitles and full recordings share the same model).
MODEL_LOCK = threading.RLock()


def _get_model():
    global _model
    if _model is None:
        with MODEL_LOCK:
            if _model is None:
                from faster_whisper import WhisperModel
                # int8 on CPU works on any normal computer without a graphics card.
                _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def load_audio(path: str):
    """
    Read the sound from any audio or video file and turn it into 16 kHz mono numbers.
    We do this ourselves (instead of letting faster-whisper do it) so the project keeps
    working with newer versions of the PyAV library.
    """
    import av
    import numpy as np

    chunks = []
    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError("This file has no sound track. Upload a recording or a video with audio.")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush the last bit
            chunks.append(out.to_ndarray().reshape(-1))

    if not chunks:
        raise ValueError("Couldn't read any sound from this file.")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


LANGUAGE_NAMES = {
    "en": "English", "ur": "Urdu", "hi": "Hindi", "pa": "Punjabi", "ps": "Pashto", "sd": "Sindhi",
    "fa": "Persian", "ar": "Arabic", "bn": "Bengali", "tr": "Turkish", "zh": "Chinese",
    "es": "Spanish", "fr": "French", "de": "German", "ru": "Russian", "pt": "Portuguese",
}


def language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get(code or "", code or "unknown")


def _can_translate() -> bool:
    # English-only models (".en") and "turbo" models were not trained to translate.
    name = config.WHISPER_MODEL.lower()
    return not name.endswith(".en") and "turbo" not in name


def vocabulary_hint() -> str | None:
    """Medical words to prime Whisper with, so it expects them (e.g. 'prednisolone', not 'pridness alone')."""
    try:
        text = config.WHISPER_VOCABULARY_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def detect_language(model, audio) -> str:
    """Listen to the first 30 seconds only, to find out which language is spoken."""
    _, info = model.transcribe(audio[: 30 * SAMPLE_RATE], beam_size=1, vad_filter=True)
    return info.language  # the text itself is never generated here, so this is quick


def transcribe_file(path: str) -> dict:
    """
    Returns:
      {"text": full transcript (English when translated),
       "segments": [{"start": 0.0, "end": 3.2, "text": "..."}, ...],
       "language": "ur", "language_name": "Urdu",
       "translated": True/False, "warning": "" or a note for the reviewer}
    """
    audio = load_audio(path)
    with MODEL_LOCK:
        return _transcribe_audio(audio)


def _transcribe_audio(audio) -> dict:
    model = _get_model()

    language = config.WHISPER_LANGUAGE
    task = config.WHISPER_TASK
    warning = ""

    if task == "auto":
        if language is None:
            language = detect_language(model, audio)
        task = "transcribe" if language == "en" else "translate"

    if task == "translate" and not _can_translate():
        task = "transcribe"
        warning = (f"The recording is in {language_name(language)}, but the Whisper model "
                   f"'{config.WHISPER_MODEL}' can't translate. Use 'small' or 'medium' in config.py.")

    segments, info = model.transcribe(
        audio,
        task=task,
        language=language,
        beam_size=config.WHISPER_BEAM_SIZE,
        vad_filter=True,  # skips silence, makes it faster
        initial_prompt=vocabulary_hint() if language in (None, "en") else None,
    )
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append({"start": round(s.start, 1), "end": round(s.end, 1), "text": text})

    translated = task == "translate" and info.language != "en"
    if translated and not warning:
        warning = (f"The recording was in {language_name(info.language)} and was translated to English "
                   "automatically. Check medicine names and numbers against the recording.")
    return {
        "text": "\n".join(s["text"] for s in out),
        "segments": out,
        "language": info.language,
        "language_name": language_name(info.language),
        "translated": translated,
        "warning": warning,
    }
