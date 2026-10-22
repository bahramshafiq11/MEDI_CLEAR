"""
Builds a DRAFT bill from what was actually done in the visit.

Important rules (these are what make the billing trustworthy):
- The bill is based on SERVICES PERFORMED, not on the diagnosis.
- Prices come only from data/fee_schedule.csv (the AI never sets a price).
- Tests that were only ORDERED (to be done later / at a lab) are not billed here.
- Anything the system can't match gets price 0 and is flagged for the doctor or billing staff.
- Every bill starts as "draft" and must be approved by the doctor.
"""
import csv
import re
from functools import lru_cache

from . import config


@lru_cache
def load_fees() -> list[dict]:
    rows = []
    with open(config.DATA_DIR / "fee_schedule.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "service_code": row["service_code"],
                "service_name": row["service_name"],
                "keywords": [k.strip().lower() for k in row["keywords"].split(";") if k.strip()],
                "price": float(row["price"]),
            })
    return rows


def fee_by_code(code: str) -> dict:
    return next(r for r in load_fees() if r["service_code"] == code)


def _kw_pattern(kw: str) -> re.Pattern:
    # Short keywords (ecg, neb, bsr) must be whole words; longer ones may have endings ("stitch" -> "stitches").
    end = r"\b" if len(kw) <= 4 else ""
    return re.compile(r"\b" + re.escape(kw) + end)


def match_services(text: str) -> list[dict]:
    """One phrase can mention several services: 'cleaned, stitched and dressed the wound'."""
    t = text.lower()
    exact = [r for r in load_fees() if r["service_name"].lower() == t.strip()]
    if exact:
        return exact  # the AI picked a name straight from the fee list
    hits = {}
    for row in load_fees():
        for kw in row["keywords"]:
            if _kw_pattern(kw).search(t):
                hits[row["service_code"]] = row
                break
    procedures = [r for c, r in hits.items() if not c.startswith("CONS") and c != "INCLUDED"]
    if procedures:
        return procedures  # a real procedure wins over generic words like "examination"
    return list(hits.values())


def build_bill(extracted: dict, coding: dict) -> dict:
    lines, included, not_billed, seen = [], [], [], set()

    def add_line(fee: dict, source: str, needs_review=False, note=""):
        if fee["service_code"] in seen:
            return
        seen.add(fee["service_code"])
        lines.append({
            "service_code": fee["service_code"],
            "description": fee["service_name"],
            "price": fee["price"],
            "source": source,
            "needs_review": needs_review,
            "note": note,
            "include": True,
        })

    # 1. Consultation fee (one per visit)
    cons_code = "CONS-FU" if extracted.get("visit_type") == "follow_up" else "CONS-NEW"
    add_line(fee_by_code(cons_code), "Visit type: " + extracted.get("visit_type", "new").replace("_", "-"))

    # 2. Services / procedures performed during the visit
    for service in extracted.get("services_performed", []):
        matches = match_services(service)
        if not matches:
            lines.append({
                "service_code": "MANUAL", "description": service, "price": 0.0, "source": service,
                "needs_review": True, "note": "Not in the fee schedule. Enter the price manually.", "include": True,
            })
            continue
        for fee in matches:
            if fee["service_code"].startswith("CONS"):
                continue  # consultation already added
            if fee["service_code"] == "INCLUDED":
                included.append(service)
            else:
                add_line(fee, service)

    # 3. Tests ordered for later are listed but not charged
    for test in extracted.get("tests_ordered", []):
        not_billed.append({"item": test, "reason": "Ordered for later. Billed by the lab when it's done."})

    # 4. Warnings for the reviewer
    warnings = []
    codes = coding.get("codes", [])
    if not any(c["kind"] == "Primary diagnosis" for c in codes):
        warnings.append("No confirmed diagnosis. Only symptom codes are attached; insurance claims may need a final diagnosis.")
    if any(c["needs_review"] for c in codes):
        warnings.append("Some diagnosis codes are uncertain matches. Check them on the Codes tab.")
    if any(l["needs_review"] for l in lines):
        warnings.append("Some services have no price in the fee schedule.")

    total = sum(l["price"] for l in lines if l["include"])
    return {
        "currency": config.CURRENCY,
        "lines": lines,
        "included_in_consultation": included,
        "not_billed": not_billed,
        "diagnosis_codes": [c["code"] for c in codes],
        "total": total,
        "warnings": warnings,
        "status": "draft",
    }


def recalc_total(bill: dict) -> dict:
    bill["total"] = sum(float(l.get("price") or 0) for l in bill.get("lines", []) if l.get("include", True))
    return bill


# ---------------------------------------------------------------------------
# Safety net: tests that can only be done in the clinic, mentioned but missed by the AI
# ---------------------------------------------------------------------------
POINT_OF_CARE = {
    "TEST-BSR": ("glucometer",),
    "TEST-UDIP": ("dipstick",),
    "PROC-NEB": ("nebulization", "nebulisation", "nebuliser", "nebulizer"),
    "PROC-SUT": ("stitches", "sutures", "stitched", "sutured"),
    "PROC-DRS": ("dressing",),
}
LATER_WORDS = re.compile(r"\b(lab|laboratory|tomorrow|later|outside|next time|at home|get (it|them|this) done"
                         r"|remove|removed|removal|come back|change the dressing|in \d+ days)\b")


def add_missed_services(bill: dict, transcript_text: str) -> dict:
    billed = {l["service_code"] for l in bill["lines"]}
    sentences = re.split(r"(?<=[.!?])\s+|\n", transcript_text)
    for code, words in POINT_OF_CARE.items():
        if code in billed:
            continue
        for sentence in sentences:
            low = sentence.lower()
            if any(w in low for w in words) and not LATER_WORDS.search(low):
                fee = fee_by_code(code)
                bill["lines"].append({
                    "service_code": code, "description": fee["service_name"], "price": fee["price"],
                    "source": sentence.strip()[:120], "needs_review": True, "include": True,
                    "note": "Found in the conversation but not listed by the AI. Confirm it was done.",
                })
                bill["warnings"].append(f"Added \"{fee['service_name']}\" because it was mentioned in the conversation. Please confirm.")
                break
    return recalc_total(bill)
