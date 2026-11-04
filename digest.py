"""Morning message (09:00 IST): promises due today or late, and people you met yesterday to follow up.

Also writes today's date to state/state.json. The workflow commits it, which keeps the repo active,
so GitHub never pauses the 10-minute schedule (it pauses public-repo schedules after 60 quiet days).
"""
from __future__ import annotations

import html
import sys

from common import add_days, env, load_config, load_dotenv, load_state, log, nice_date, save_state, today_local
from notion_client import prop

esc = html.escape


def build(store, today: str) -> str | None:
    names = {p["id"]: p["name"] for p in store.people()}
    due = sorted((r for r in store.open_promises() if r["who"] == "Me" and r["due"] and r["due"][:10] <= today),
                 key=lambda r: r["due"])
    met = []
    for row in store.interactions("Saved", on=add_days(today, -1)):
        for pid in prop(row, "People") or []:
            if pid in names and names[pid] not in met:
                met.append(names[pid])
    if not due and not met:
        return None
    lines = ["☀️ <b>Today</b>"]
    if due:
        lines.append("\n<b>You promised</b>")
        for r in due[:10]:
            who = ", ".join(names.get(i, "?") for i in r["people"]) or "—"
            late = " (late)" if r["due"][:10] < today else ""
            lines.append(f"• {esc(who)}: {esc(r['what'])} · {nice_date(r['due'])}{late}")
    if met:
        lines.append("\n<b>Met yesterday: send each a follow-up today</b>")
        lines.append("• " + ", ".join(esc(n) for n in met[:15]))
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
