"""
Fixes medicine names that speech-to-text misheard, using a list of real drug names
(data/medicines.txt).  Example: "pridness alone" -> "prednisolone".

Every correction is recorded ("heard_as") so the doctor can see what was changed.
"""
import re
from functools import lru_cache

from rapidfuzz import fuzz

from . import config

MIN_SCORE = 82  # how similar a misheard word must be to a real drug name to be corrected


@lru_cache
def load_medicines() -> list[str]:
    names = []
    with open(config.DATA_DIR / "medicines.txt", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lower()
            if line and not line.startswith("#"):
                names.append(line)
    return names


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _sound(text: str) -> str:
    """
    A rough "sound skeleton": similar-sounding letters made equal and vowels dropped,
    so misheard words still match. "pridnessalone" and "prednisolone" both become "prdnsln".
    """
    t = _squash(text)
    for a, b in (("ph", "f"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("z", "s"), ("c", "k"), ("y", "i"), ("h", "")):
        t = t.replace(a, b)
    if not t:
        return ""
    t = t[0] + re.sub(r"[aeiou]", "", t[1:])
    return re.sub(r"(.)\1+", r"\1", t)  # "ss" -> "s"


def _similarity(piece: str, drug: str) -> float:
    spelled = fuzz.ratio(piece, _squash(drug))
    if spelled < 60:
        return spelled
    sounds = fuzz.ratio(_sound(piece), _sound(drug))
    # sounds-alike only counts when the spelling is also somewhat close
    return max(spelled, sounds - 3 if sounds >= 85 else 0)


def correct_name(name: str) -> tuple[str, str | None, int]:
    """
    Returns (corrected name, what it was heard as or None, match score).
    Names already in the list, or with no close match, are left unchanged.
    """
    original = (name or "").strip()
    low = original.lower()
    if not low:
        return original, None, 0
    meds = load_medicines()
    words = re.findall(r"[a-z0-9-]+", low)

    # Already a known drug (whole name, or one word of it like "salbutamol inhaler")?
    if low in meds or any(w in meds for w in words):
        return original, None, 100

    # Try the whole name and every run of 1-3 neighbouring words, with spaces removed:
    # "pridness alone" -> "pridnessalone" ~ "prednisolone"
    pieces = {_squash(low)}
    for size in (1, 2, 3):
        for i in range(len(words) - size + 1):
            pieces.add(_squash("".join(words[i:i + size])))
    best_drug, best_score, best_piece = None, 0, ""
    for piece in pieces:
        if len(piece) < 5:
            continue
        for drug in meds:
            score = _similarity(piece, drug)
            if score > best_score:
                best_drug, best_score, best_piece = drug, score, piece
    if best_drug and best_score >= MIN_SCORE:
        # Keep any extra words such as "inhaler" or "syrup"
        extras = [w for w in words if _squash(w) not in best_piece]
        corrected = " ".join([best_drug.capitalize()] + extras)
        return corrected, original, int(best_score)
    return original, None, int(best_score)


def correct_medicines(medicines: list[dict]) -> list[dict]:
    out = []
    for m in medicines:
        name, heard_as, score = correct_name(m.get("name", ""))
        item = {**m, "name": name}
        if heard_as:
            item["heard_as"] = heard_as
        elif score < MIN_SCORE:
            item["unknown_name"] = True  # not in the medicine list: worth a check
        out.append(item)
    return out
