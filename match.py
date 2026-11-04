"""Is this the same person as someone already in Notion? Pure functions, no API calls."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "shri", "smt", "ji", "bhai", "sir"}
SAME, MAYBE = 0.92, 0.72


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z]+", s) if w and w not in HONORIFICS]
    return " ".join(words)


def norm_linkedin(u: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#\s]+)", (u or "").lower())
    return m.group(1).rstrip("/") if m else ""


def norm_phone(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def name_score(a: str, b: str) -> float:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    if set(ta) == set(tb):
        return 0.98                       # "Sharma Neha" vs "Neha Sharma"
    if len(ta) == 1 or len(tb) == 1:      # "Neha" vs "Neha Sharma": possible, never certain
        short, long_ = (ta, tb) if len(ta) == 1 else (tb, ta)
        return 0.8 if short[0] == long_[0] else 0.0
    if ta[0] != tb[0] and ta[-1] != tb[-1]:
        return SequenceMatcher(None, a, b).ratio() * 0.6   # different first AND last name
    return SequenceMatcher(None, a, b).ratio()


def find(person: dict, rows: list[dict]) -> tuple[str, list[dict]]:
    """Returns ("same", [row]) | ("maybe", [up to 2 rows]) | ("new", [])."""
    li, email, phone = norm_linkedin(person.get("linkedin_url")), (person.get("email") or "").lower(), norm_phone(person.get("phone"))
    for r in rows:
        if (li and li == norm_linkedin(r.get("linkedin_url"))) or (email and email == (r.get("email") or "").lower()) \
                or (phone and phone == norm_phone(r.get("phone"))):
            return "same", [r]
    scored = sorted(((name_score(person["name"], r["name"]), r) for r in rows), key=lambda x: -x[0])
    if not scored or scored[0][0] < MAYBE:
        return "new", []
    best, row = scored[0]
    company_clash = person.get("company") and row.get("company") and \
        norm_name(person["company"]) not in norm_name(row["company"]) and norm_name(row["company"]) not in norm_name(person["company"])
    if best >= SAME and not company_clash:
        if len(scored) > 1 and scored[1][0] >= SAME:      # two people with the same name: ask
            return "maybe", [scored[0][1], scored[1][1]]
        return "same", [row]
    return "maybe", [r for s, r in scored[:2] if s >= MAYBE]
