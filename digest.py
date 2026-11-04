"""Morning message (09:00 IST): promises due today or late, and people you met yesterday to follow up.

Also writes today's date to state/state.json. The workflow commits it, which keeps the repo active,
so GitHub never pauses the 10-minute schedule (it pauses public-repo schedules after 60 quiet days).
"""
from __future__ import annotations

import html
import re
import sys

from common import add_days, env, load_config, load_dotenv, load_state, log, nice_date, save_state, today_local
from bot import late_tag
from notion_client import prop

esc = html.escape


def build(store, today: str, cfg: dict | None = None) -> str | None:
    cfg = cfg or store.cfg
    people = store.people()
    names = {p["id"]: p["name"] for p in people}
    due = sorted((r for r in store.open_promises() if r["who"] == "Me" and r["due"] and r["due"][:10] <= today),
                 key=lambda r: r["due"])
    met = []
    for row in store.interactions("Saved", on=add_days(today, -1)):
        for pid in prop(row, "People") or []:
            if pid in names and names[pid] not in met:
                met.append(names[pid])
    order = {t: i for i, t in enumerate(["Ally", "Tier 1", "Tier 2", "Tier 3"])}
    pings = sorted((p for p in people if p["next"] and p["next"][:10] <= today and p["tier"] in order and p["name"] not in met),
                   key=lambda p: (order[p["tier"]], p["next"]))[:cfg["daily_pings"]]
    if not due and not met and not pings:
        return None
    lines = [f"<b>Today · {nice_date(today)}</b>"]
    if due:
        lines.append("\n<b>Promises due</b>")
        for r in due[:10]:
            who = ", ".join(names.get(i, "?") for i in r["people"]) or "No one linked"
            lines.append(f"• {esc(who)}: {esc(r['what'])}, by {nice_date(r['due'])}{late_tag(r['due'], today)}")
    if met:
        lines.append("\n<b>Met yesterday, so send each a follow-up today</b>")
        lines.append("• " + ", ".join(esc(n) for n in met[:15]))
    if pings:
        lines.append("\n<b>Ping today</b>")
        for p in pings:
            why = re.sub(r"^\[[^\]]*\]\s*", "", (p["cares_about"].splitlines() or [""])[-1])[:90]
            lines.append(f"• {esc(p['name'])} · {esc(p['tier'])} · last contact {nice_date(p['last']) or 'none'}"
                         + (f": {esc(why)}" if why else ""))
    return "\n".join(lines)


def main() -> int:
    load_dotenv()
    cfg = load_config()
    from notion_client import Notion
    from store import Store
    from telegram import Telegram
    today = today_local(cfg["timezone"])
    text = build(Store(Notion(env("NOTION_TOKEN")), cfg), today)
    owner = env("TELEGRAM_OWNER_ID", required=False)
    if text and owner:
        Telegram(env("TELEGRAM_BOT_TOKEN")).send(int(owner), text)
    state = load_state()
    state["last_digest"] = today
    save_state(state)
    log("digest:", "sent" if text and owner else "nothing to send")
    return 0


if __name__ == "__main__":
    sys.exit(main())
