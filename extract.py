"""Gemini turns a voice note, photo or text into people, one interaction and promises (JSON schema)."""
from __future__ import annotations

import json
import re
import time
from datetime import date

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from common import redact

TYPES = ["Meeting", "Call", "Event", "Message", "Note", "Contact card"]

PERSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "world": {"type": "string"},
        "how_met": {"type": "string"},
        "cares_about": {"type": "string"},
        "personal_notes": {"type": "string"},
        "ideas_to_help": {"type": "string"},
        "common_ground": {"type": "string"},
        "mutual_contacts": {"type": "string"},
        "linkedin_url": {"type": "string"},
        "email": {"type": "string"},
        "phone": {"type": "string"},
    },
    "required": ["name"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "people": {"type": "array", "items": PERSON},
        "summary": {"type": "string"},
        "type": {"type": "string", "enum": TYPES},
        "date": {"type": "string"},
        "promises": {"type": "array", "items": {
            "type": "object",
            "properties": {"who": {"type": "string", "enum": ["me", "them"]},
                           "person": {"type": "string"}, "what": {"type": "string"}, "due": {"type": "string"}},
            "required": ["who", "what"]}},
        "question": {"type": "string"},
        "transcript": {"type": "string"},
        "follow_up_draft": {"type": "string"},
    },
    "required": ["people", "summary", "type", "promises"],
}

PROMPT = """You keep {owner}'s personal network notes. {owner} sends you a voice note, a photo (visiting card,
profile screenshot) or text about people he met or talked to. Today is {today} ({weekday}).{event}

How {owner} networks: be useful first, keep every promise, follow up within 24 hours, and remember
what matters most to people (their goals, problems, health, wealth, children).

Return JSON:
- people: everyone he met or talked about as a contact. Never include {owner} himself.
  Fill a field only if the input says it. Never guess a company, role, email or phone.
  world: one of {worlds}.
  how_met: where or through whom they met, in a few words.
  cares_about: their goals, problems, what they are working on.
  personal_notes: family, hobbies, hometown, life events. Write respectfully, as if they might read it.
  common_ground: rare things they share with {owner} (his links: {links}). Only if said.
  mutual_contacts: people they both know, or who introduced them. Only if said.
  ideas_to_help: up to 2 small, useful things {owner} could do for them (an intro, an article, feedback,
    a five-minute favour) that follow from what was said. Never invent facts. One sentence each.
- summary: one plain line about this interaction, e.g. "Coffee at Third Wave, talked about AI pricing".
- type: Meeting, Call, Event, Message, Note, or Contact card (just a card or profile, no conversation).
- date: YYYY-MM-DD of the interaction. "yesterday" means the day before today. Empty if not said.
- promises: things {owner} promised ("who": "me") or they promised him ("who": "them").
  person = that contact's name. due = YYYY-MM-DD if a time was said ("by Friday"), else empty.
- question: if a key fact is missing (a full name, or how they met), ONE short question to ask {owner}. Else empty.
- transcript: for a voice note, what he said, word for word. Empty for text and photos.
- follow_up_draft: if he met or talked to someone new or important, his 24-hour follow-up message to the
  main person, ending "— {owner}". Empty for a contact card, a forwarded message or a plain note.
Writing rules for every text you write (notes, summary, ideas, the draft):
- Normal sentences, the way he would say them to a friend. No clever, poetic or salesy phrases,
  no buzzwords, no filler ("great to connect", "hope you're doing well"), no emoji.
- Keep every concrete detail: names, companies, roles, numbers, dates, places.
- Short: 1 to 2 sentences per field. Say each thing once. Never repeat the summary in a note.
- The draft: thank them for one specific thing, deliver or restate the promise with its date,
  end with one clear next step. 3 to 4 short sentences.
He may speak English or Hinglish. Write the output in English."""


class QuotaExhausted(Exception):
    """Gemini daily or per-minute quota is used up."""


class ExtractError(Exception):
    pass


def build_prompt(cfg: dict, today: str, event: str | None) -> str:
    d = date.fromisoformat(today)
    return PROMPT.format(owner=cfg["owner_name"], today=today, weekday=d.strftime("%A"),
                         worlds=", ".join(cfg["worlds"]), links=", ".join(cfg.get("owner_links") or []),
                         event=f"\nHe is at this event right now: {event}." if event else "")


# ---------------- parsing (pure functions, tested) ----------------
def _s(x, n: int = 1900) -> str:
    return re.sub(r"\s+", " ", str(x or "")).strip()[:n]


def _iso(x: str) -> str:
    x = _s(x, 10)
    try:
        return date.fromisoformat(x).isoformat()
    except ValueError:
        return ""


def normalise(data: dict, cfg: dict, today: str) -> dict:
    owner = cfg["owner_name"].lower()
    people = []
    for p in data.get("people") or []:
        name = _s(p.get("name"), 100)
        if not name or name.lower() in (owner, "me", "i", "myself") or name.lower().split()[0] == owner and len(name.split()) == 1:
            continue
        person = {k: _s(p.get(k)) for k in PERSON["properties"]}
        person["name"] = name
        if person["world"] not in cfg["worlds"]:
            person["world"] = ""
        if person["linkedin_url"] and "linkedin.com" not in person["linkedin_url"]:
            person["linkedin_url"] = ""
        if person["email"] and "@" not in person["email"]:
            person["email"] = ""
        people.append(person)
    promises = []
    for pr in data.get("promises") or []:
        what = _s(pr.get("what"), 300)
        if what and pr.get("who") in ("me", "them"):
            promises.append({"who": pr["who"], "person": _s(pr.get("person"), 100), "what": what, "due": _iso(pr.get("due"))})
    d = _iso(data.get("date")) or today
    if d > today:
        d = today
    return {
        "people": people,
        "summary": _s(data.get("summary"), 300) or "Note",
        "type": data.get("type") if data.get("type") in TYPES else "Note",
        "date": d,
        "promises": promises,
        "question": _s(data.get("question"), 200),
        "transcript": _s(data.get("transcript"), 5000),
        "follow_up_draft": str(data.get("follow_up_draft") or "").strip()[:1000],
    }


# ---------------- the call ----------------
class Extractor:
    def __init__(self, api_key: str, cfg: dict, retries: int = 2):
        self.client = genai.Client(api_key=api_key)
        self.cfg = cfg
        self.retries = retries

    def extract(self, text: str, media: list[tuple[bytes, str]], today: str, event: str | None = None) -> dict:
        parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in media]
        parts.append(types.Part(text=text or "(no text, see the attachment)"))
        config = types.GenerateContentConfig(
            system_instruction=build_prompt(self.cfg, today, event),
            response_mime_type="application/json",
            response_json_schema=SCHEMA,
            temperature=0.1,
        )
        g = self.cfg["gemini"]
        try:
            raw = self._call(g["model"], parts, config)
        except ExtractError:
            raw = self._call(g["fallback_model"], parts, config)
        try:
            data = json.loads(raw)
        except ValueError:
            raise ExtractError("Gemini returned invalid JSON")
        return normalise(data if isinstance(data, dict) else {}, self.cfg, today)

    def _call(self, model: str, parts: list, config) -> str:
        last = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.models.generate_content(
                    model=model, contents=types.Content(role="user", parts=parts), config=config)
                if not resp.text:
                    raise ExtractError("empty response")
                return resp.text
            except genai_errors.APIError as e:
                code = getattr(e, "code", 0) or 0
                if code == 429:
                    raise QuotaExhausted(redact(str(e))[:200])
                last = ExtractError(f"Gemini {code}")
                if code >= 500 and attempt < self.retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise last
        raise last or ExtractError("unknown error")
