"""One polling run: read new Telegram messages, save people to Notion, answer, then retry anything pending.

GitHub Actions runs this every 10 minutes. Logs are public, so they only show counts and error types.
"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timedelta, timezone

from common import env, load_config, load_dotenv, load_state, log, nice_date, save_state, today_local
from extract import ExtractError, QuotaExhausted
from match import name_score

HELP = """<b>People Inbox</b>
Send me anything about someone you met:
• a <b>voice note</b> ("Met Neha Sharma from Razorpay at the AI meetup. She's hiring PMs. I'll send her my teardown.")
• a <b>photo</b> of a visiting card or a profile screenshot
• a <b>LinkedIn link</b> (share it from the LinkedIn app)
• a <b>forwarded</b> message

Commands:
/who Neha — everything you know about Neha
/promises — what you owe people and what they owe you
/event AI meetup — tag the next 4 hours of notes with this event (/event off to stop)

I check for messages every 10 minutes."""

esc = html.escape


def card_text(c: dict) -> str:
    head = f"✅ <b>Saved</b> · {esc(c['type'])} · {nice_date(c['date'])}" + (f" · {esc(c['event'])}" if c.get("event") else "")
    lines = [head]
    maybe_ks = {m["k"]: m for m in c.get("maybe", [])}
    for k, p in enumerate(c["people"]):
        name = f'<a href="{esc(p["url"])}">{esc(p["name"])}</a>' if p.get("url") else esc(p["name"])
        if k in maybe_ks:
            status = "new · is this " + " or ".join(esc(n) for n in maybe_ks[k]["names"]) + "?"
        elif p["status"] == "new":
            status = f"new · {esc(p.get('tier') or '')}" + (f" · next {nice_date(p['next'])}" if p.get("next") else "")
        else:
            status = "updated" + (f" · next {nice_date(p['next'])}" if p.get("next") else "")
        lines.append(f"👤 {name} — {status}")
    for p in c["people"][:3]:
        if p.get("idea"):
            lines.append(f"🎁 For {esc(p['name'])}: {esc(p['idea'])}")
    for pr in c["promises"]:
        if pr["who"] == "me":
            lines.append(f"📌 You → {esc(pr['person'] or 'them')}: {esc(pr['what'])} · due {nice_date(pr['due'])}")
        else:
            lines.append(f"📌 {esc(pr['person'] or 'They')} → you: {esc(pr['what'])} · check {nice_date(pr['due'])}")
    if c.get("question"):
        lines.append(f"❓ {esc(c['question'])} <i>(reply to this message)</i>")
    if c.get("draft"):
        lines.append(f"\n✉️ <b>Your 24-hour follow-up</b> (tap to copy, edit, send yourself)\n<pre>{esc(c['draft'])}</pre>")
    return "\n".join(lines)


def card_buttons(c: dict) -> list[list[tuple[str, str]]]:
    iid = c["id"].replace("-", "")
    rows = []
    for m in c.get("maybe", []):
        row = [(f"Same as {n[:20]}", f"m:{iid}:{m['k']}:{j}") for j, n in enumerate(m["names"][:2])]
        row.append(("Keep separate", f"k:{iid}:{m['k']}"))
        rows.append(row)
    rows.append([("↩️ Undo", f"u:{iid}")])
    return rows


class Bot:
    def __init__(self, tg, store, extractor, cfg: dict, owner_id: str | None, state: dict):
        self.tg, self.store, self.ex, self.cfg, self.owner, self.state = tg, store, extractor, cfg, owner_id, state
        self.counts = {"updates": 0, "saved": 0, "pending": 0, "errors": 0}
        self.fresh = set()   # pending rows made in this run: Gemini just failed on them, so don't retry yet

    def today(self) -> str:
        return today_local(self.cfg["timezone"])

    # ---------- run ----------
    def run(self) -> dict:
        self.fresh = set()
        for u in self.tg.updates():
            self.counts["updates"] += 1
            try:
                if "callback_query" in u:
                    self.on_button(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Exception as e:   # one bad message never stops the run
                self.counts["errors"] += 1
                log(f"update failed: {type(e).__name__}")
                chat = (u.get("message") or {}).get("chat", {}).get("id")
                if chat and self.is_owner((u.get("message") or {}).get("from", {})):
                    self.tg.send(chat, f"⚠️ Couldn't save that ({esc(type(e).__name__)}). Please send it again.")
            finally:
                self.tg.ack(u["update_id"])
        if self.owner:
            self.retry_pending()
        return self.counts

    def is_owner(self, user: dict) -> bool:
        return bool(self.owner) and str(user.get("id")) == str(self.owner)

    # ---------- messages ----------
    def on_message(self, msg: dict) -> None:
        if msg.get("chat", {}).get("type") != "private":
            return
        user, chat = msg.get("from", {}), msg["chat"]["id"]
        if not self.owner:
            self.tg.send(chat, f"Your Telegram ID is <code>{user.get('id')}</code>. Add it as the "
                               f"<b>TELEGRAM_OWNER_ID</b> secret in GitHub, then send me a note.")
            return
        if not self.is_owner(user):
            return   # strangers get no reply
        text = msg.get("text") or msg.get("caption") or ""
        if text.startswith("/"):
            return self.command(chat, text)
        payload = self.payload(msg, text)
        if not payload["text"] and not payload["file_id"]:
            self.tg.send(chat, "Send me text, a voice note or a photo.")
            return
        self.capture(chat, payload, reply_to=msg.get("message_id"))

    @staticmethod
    def payload(msg: dict, text: str) -> dict:
        file_id, mime, source = None, None, "Text"
        if msg.get("voice") or msg.get("audio"):
            a = msg.get("voice") or msg["audio"]
            file_id, mime, source = a["file_id"], a.get("mime_type") or "audio/ogg", "Voice"
        elif msg.get("photo"):
            file_id, mime, source = msg["photo"][-1]["file_id"], "image/jpeg", "Photo"
        elif msg.get("document") and str(msg["document"].get("mime_type", "")).startswith(("image/", "audio/")):
            d = msg["document"]
            file_id, mime = d["file_id"], d["mime_type"]
            source = "Photo" if mime.startswith("image/") else "Voice"
        origin = msg.get("forward_origin")
        if origin:
            who = (origin.get("sender_user") or {}).get("first_name") or origin.get("sender_user_name") or "someone"
            text = f"(Forwarded message from {who})\n{text}"
            source = "Forward"
        context = (msg.get("reply_to_message") or {}).get("text") or ""
        return {"text": text, "file_id": file_id, "mime": mime, "source": source, "context": context[:1500]}

    def event(self) -> str | None:
        ev = self.state.get("event")
        if ev and datetime.fromisoformat(ev["until"]) > datetime.now(timezone.utc):
            return ev["name"]
        return None

    def capture(self, chat: int, payload: dict, reply_to: int | None = None, pending_id: str | None = None) -> str:
        media = [(self.tg.download(payload["file_id"]), payload["mime"])] if payload.get("file_id") else []
        text = payload["text"]
        if payload.get("context"):
            text = f"He is replying to this earlier note from you, so it is about the same people:\n{payload['context']}\n\nHis reply:\n{text}"
        try:
            ex = self.ex.extract(text, media, self.today(), self.event())
        except (QuotaExhausted, ExtractError) as e:
            if pending_id:
                raise
            self.fresh.add(self.store.save_pending(payload, self.today(), type(e).__name__))
            self.counts["pending"] += 1
            self.tg.send(chat, "⏳ Got it. Gemini is busy, so I'll save it on a later run.", reply_to=reply_to)
            return "pending"
        if not ex["people"]:
            if pending_id:
                self.store.mark(pending_id, "Error")
            self.tg.send(chat, "I couldn't find a person in that. Say their full name, for example: "
                               "<i>\"Met Neha Sharma from Razorpay at the AI meetup\"</i>.", reply_to=reply_to)
            return "skipped"
        c = self.store.save_capture(ex, payload["source"], self.event(), self.today(), existing_id=pending_id)
        self.counts["saved"] += 1
        self.tg.send(chat, card_text(c), buttons=card_buttons(c), reply_to=reply_to)
        return "saved"

    def retry_pending(self) -> None:
        tries_max = self.cfg["pending_max_tries"]
        for row in [r for r in self.store.interactions("Pending") if r["id"] not in self.fresh][:5]:
            d = self.store.data(row["id"])
            try:
                self.capture(int(self.owner), d["payload"], pending_id=row["id"])
            except (QuotaExhausted, ExtractError):
                give_up = d.get("tries", 0) + 1 >= tries_max
                self.store.bump_pending(row["id"], give_up)
                if give_up:
                    self.tg.send(int(self.owner), "⚠️ I couldn't process one note after several tries. "
                                                  "It's in Notion → Interactions with status Error. Please resend it.")
                break   # Gemini is still busy: try again next run

    # ---------- buttons ----------
    def on_button(self, cq: dict) -> None:
        if not self.is_owner(cq.get("from", {})):
            return self.tg.answer(cq["id"])
        msg = cq.get("message") or {}
        parts = (cq.get("data") or "").split(":")
        action, iid = parts[0], _uuid(parts[1]) if len(parts) > 1 else ""
        if action == "u":
            done = self.store.undo(iid)
            self.tg.edit(msg["chat"]["id"], msg["message_id"], "↩️ <b>Undone.</b> Nothing from this note is in Notion now.")
            self.tg.answer(cq["id"], "Undone" if done else "Already undone")
            return
        if action == "m":
            c = self.store.merge(iid, int(parts[2]), int(parts[3]))
        elif action == "k":
            c = self.store.keep_separate(iid, int(parts[2]))
        else:
            return self.tg.answer(cq["id"])
        self.tg.edit(msg["chat"]["id"], msg["message_id"], card_text(c), card_buttons(c))
        self.tg.answer(cq["id"], "Done")

    # ---------- commands ----------
    def command(self, chat: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        if cmd == "/who":
            self.tg.send(chat, self.who(arg) if arg else "Usage: /who Neha")
        elif cmd == "/promises":
            self.tg.send(chat, self.promises())
        elif cmd == "/event":
            if not arg or arg.lower() == "off":
                self.state.pop("event", None)
                self.tg.send(chat, "Event mode off.")
            else:
                until = datetime.now(timezone.utc) + timedelta(hours=self.cfg["event_hours"])
                self.state["event"] = {"name": arg[:80], "until": until.isoformat(timespec="minutes")}
                self.tg.send(chat, f"🎟 Event mode on: <b>{esc(arg[:80])}</b> for {self.cfg['event_hours']} hours. "
                                   f"Every note you send gets this tag.")
        elif cmd == "/id":
            self.tg.send(chat, f"Your Telegram ID: <code>{chat}</code>")
        else:
            self.tg.send(chat, HELP)

    def who(self, q: str) -> str:
        people = self.store.people()
        ql = q.lower()
        scored = sorted(((max(name_score(q, p["name"]), 0.75 if ql in p["name"].lower() else 0), p) for p in people),
                        key=lambda x: -x[0])
        hits = [p for s, p in scored[:3] if s >= 0.72]
        if not hits:
            return f"No one called {esc(q)} yet."
        promises = self.store.open_promises()
        out = []
        for p in hits:
            job = " · ".join(x for x in (p["role"], p["company"]) if x)
            lines = [f'<b><a href="{esc(p["url"])}">{esc(p["name"])}</a></b> — {esc(p["tier"] or "no tier")}' + (f" · {esc(job)}" if job else "")]
            lines.append(f"Last {nice_date(p['last']) or '—'} · next {nice_date(p['next']) or '—'}")
            for label, k in (("Cares about", "cares_about"), ("Personal", "personal_notes"), ("How you met", "how_met"),
                             ("Common ground", "common_ground"), ("Mutual contacts", "mutual_contacts"),
                             ("Ideas to help", "ideas_to_help")):
                if p[k]:
                    lines.append(f"<b>{label}:</b> {esc(p[k][-400:])}")
            for pr in promises:
                if p["id"] in pr["people"]:
                    arrow = "You owe" if pr["who"] == "Me" else "They owe"
                    lines.append(f"📌 {arrow}: {esc(pr['what'])} · {nice_date(pr['due'])}")
            out.append("\n".join(lines))
        return "\n\n".join(out)

    def promises(self) -> str:
        names = {p["id"]: p["name"] for p in self.store.people()}
        rows = sorted(self.store.open_promises(), key=lambda r: r["due"] or "9999")
        if not rows:
            return "No open promises."
        today = self.today()
        out = []
        for who, title in (("Me", "<b>You owe</b>"), ("Them", "<b>They owe you</b>")):
            mine = [r for r in rows if r["who"] == who]
            if mine:
                out.append(title)
                for r in mine[:15]:
                    person = ", ".join(names.get(i, "?") for i in r["people"]) or "—"
                    late = " (late)" if r["due"] and r["due"][:10] < today else ""
                    out.append(f"• {esc(person)}: {esc(r['what'])} · {nice_date(r['due'])}{late}")
        return "\n".join(out)


def _uuid(s: str) -> str:
    s = s.replace("-", "")
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}" if len(s) == 32 else s


def main() -> int:
    load_dotenv()
    cfg = load_config()
    from extract import Extractor
    from notion_client import Notion
    from store import Store
    from telegram import Telegram
    state = load_state()
    before = json.dumps(state, sort_keys=True)
    bot = Bot(Telegram(env("TELEGRAM_BOT_TOKEN")), Store(Notion(env("NOTION_TOKEN")), cfg),
              Extractor(env("GEMINI_API_KEY"), cfg), cfg, env("TELEGRAM_OWNER_ID", required=False), state)
    counts = bot.run()
    if json.dumps(state, sort_keys=True) != before:
        save_state(state)
    log("run:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
