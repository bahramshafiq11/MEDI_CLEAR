"""
Talks to the local AI model through Ollama.
Ollama must be running on your computer (it starts automatically after
installing it on Windows/Mac; on Linux run `ollama serve`).
"""
import json
import re

import requests

from . import config


class LLMError(Exception):
    """Raised when the AI model can't be reached or gives a broken answer."""


def ollama_status() -> dict:
    """Check whether Ollama is running and the model is downloaded."""
    try:
        r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        wanted = config.OLLAMA_MODEL
        has_model = any(n == wanted or n.split(":")[0] == wanted for n in names) or wanted in names
        return {"running": True, "model": wanted, "model_downloaded": has_model, "installed_models": names}
    except requests.RequestException:
        return {"running": False, "model": config.OLLAMA_MODEL, "model_downloaded": False, "installed_models": []}


def ask_json(system_prompt: str, user_prompt: str, schema: dict | None = None) -> dict:
    """
    Send a prompt to the model and get a Python dict back.
    If a schema is given, Ollama forces the answer into exactly that shape ("structured output"),
    so fields can't go missing and choices like visit_type can only be one of the allowed values.
    """
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": schema or "json",  # forces the model to answer in JSON (in the exact shape, if a schema is given)
        "stream": False,
        "think": False,            # skip "thinking" mode on models that have it (much faster)
        "keep_alive": "30m",       # keep the model loaded for 30 min, so the next request doesn't wait for loading
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=config.OLLAMA_TIMEOUT)
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think")   # older models/Ollama versions don't know this option
            r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=config.OLLAMA_TIMEOUT)
        if r.status_code == 400 and schema is not None:
            payload["format"] = "json"  # very old Ollama versions: fall back to plain JSON mode
            r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=config.OLLAMA_TIMEOUT)
    except requests.ConnectionError:
        raise LLMError(
            "Can't reach Ollama. Make sure it is installed and running, "
            f"then run: ollama pull {config.OLLAMA_MODEL}"
        )
    except requests.Timeout:
        raise LLMError("The AI model took too long. Try a smaller model (e.g. qwen2.5:3b) or a shorter recording.")

    if r.status_code == 404:
        raise LLMError(f"Model '{config.OLLAMA_MODEL}' isn't downloaded. Run: ollama pull {config.OLLAMA_MODEL}")
    if not r.ok:
        raise LLMError(f"Ollama error {r.status_code}: {r.text[:200]}")

    data = r.json()
    _record_stats(data)
    content = data.get("message", {}).get("content", "")
    return parse_json(content)


# Speed numbers from the last AI call (shown in the test report)
last_stats: dict = {}


def _record_stats(data: dict):
    """Ollama reports how many tokens it read and wrote, and how long each took (in nanoseconds)."""
    read_n, read_ns = data.get("prompt_eval_count", 0), data.get("prompt_eval_duration", 0)
    out_n, out_ns = data.get("eval_count", 0), data.get("eval_duration", 0)
    last_stats.clear()
    last_stats.update({
        "prompt_tokens": read_n,
        "prompt_seconds": round(read_ns / 1e9, 1),
        "output_tokens": out_n,
        "output_seconds": round(out_ns / 1e9, 1),
        "output_tokens_per_sec": round(out_n / (out_ns / 1e9), 1) if out_ns else 0,
        "load_seconds": round(data.get("load_duration", 0) / 1e9, 1),
    })


def parse_json(text: str) -> dict:
    """Turn the model's reply into a dict, even if it added extra text around the JSON."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise LLMError("The AI model returned something that isn't valid JSON. Try again or use a bigger model.")
