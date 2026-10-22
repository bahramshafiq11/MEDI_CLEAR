"""
All settings in one place.
You can change any of these by editing the default value below,
or by setting an environment variable with the same name.
"""
import os
from pathlib import Path

# Folders
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "mediclear.db"

# --- Local AI model (Ollama) ---
# Ollama runs on your computer at this address after you install it.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Which model to use (must appear in `ollama list`).
# On a weaker PC use a smaller one, e.g. "qwen2.5:3b" (faster, a bit less accurate).
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
# How long to wait for the AI (seconds). Local models can be slow.
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))

# --- Speech to text (Whisper) ---
# Model size: "tiny", "base", "small", "medium", "large-v3".
# Bigger = more accurate but slower. "small" is a good balance.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
# "auto": English recordings are written down as they are; any other language
#         is translated into English automatically (recommended).
# "transcribe": always keep the original language.
# "translate": always turn the speech into English text.
WHISPER_TASK = os.getenv("WHISPER_TASK", "auto")
# Leave empty to auto-detect the language, or set e.g. "en" or "ur".
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "") or None
# 1 = fastest (about twice as fast). 5 = slightly more accurate, slower.
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))
# Medical words Whisper should expect (helps it spell drug names correctly). Edit the file to add more.
WHISPER_VOCABULARY_FILE = DATA_DIR / "whisper_vocabulary.txt"

# --- Coding and billing ---
# Match score (0-100) needed to accept an ICD-10 code automatically.
CODE_AUTO_ACCEPT = 85
# Below this score we don't suggest a code at all.
CODE_MIN_SCORE = 75
CURRENCY = os.getenv("CURRENCY", "Rs.")

UPLOAD_DIR.mkdir(exist_ok=True)
