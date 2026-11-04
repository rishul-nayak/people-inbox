"""Everything People Inbox reads and writes in Notion: People, Interactions, Promises.

Every capture creates one Interaction row. Its "Data" field (JSON) remembers what the capture
created and the old value of everything it changed, so Undo and Merge are exact.
"""
from __future__ import annotations

import json

from common import add_days, setting
from match import find, name_score
from notion_client import P, prop

# person dict key -> People property
TEXT = {"company": "Company", "role": "Role", "how_met": "How we met",
        "cares_about": "Cares about", "personal_notes": "Personal notes", "ideas_to_help": "Ideas to help"}
APPEND = {"cares_about", "personal_notes", "ideas_to_help"}       # existing person: add a dated line
FILL = {"company", "role", "how_met"}                              # existing person: only if empty
OTHER = {"world": ("World", P.select), "linkedin_url": ("LinkedIn", P.url),
         "email": ("Email", P.email), "phone": ("Phone", P.phone)}


def _next(props: dict, fallback: str | None) -> str | None:
    d = (props.get("Next contact") or {}).get("date")
    return d["start"] if d else fallback


def person_row(page: dict) -> dict:
    r = {"id": page["id"], "url": page.get("url", ""), "name": prop(page, "Name") or "",
         "tier": prop(page, "Tier") or "", "last": prop(page, "Last contact"), "next": prop(page, "Next contact")}
    for k, name in TEXT.items():
        r[k] = prop(page, name) or ""
    for k, (name, _) in OTHER.items():
        r[k] = prop(page, name) or ""
    return r


class Store:
    def __init__(self, notion, cfg: dict):
        self.n = notion
        self.cfg = cfg
        self.people_ds = setting("PEOPLE_DS_ID", cfg)
        self.inter_ds = setting("INTERACTIONS_DS_ID", cfg)
        self.prom_ds = setting("PROMISES_DS_ID", cfg)

    # ---------- reads ----------
    def people(self) -> list[dict]:
        return [person_row(p) for p in self.n.query(self.people_ds) if not p.get("in_trash")]

    def open_promises(self) -> list[dict]:
        rows = self.n.query(self.prom_ds, filter={"property": "Done", "checkbox": {"equals": False}})
        return [{"id": p["id"], "what": prop(p, "What") or "", "who": prop(p, "Who") or "Me",
                 "due": prop(p, "Due"), "people": prop(p, "Person") or []} for p in rows]

    def interactions(self, status: str, on: str | None = None) -> list[dict]:
        f = {"property": "Status", "select": {"equals": status}}
        if on:
            f = {"and": [f, {"property": "Date", "date": {"equals": on}}]}
        return self.n.query(self.inter_ds, filter=f)

    def data(self, interaction_id: str) -> dict:
        return json.loads(prop(self.n.get_page(interaction_id), "Data") or "{}")

    def _set_data(self, interaction_id: str, data: dict) -> None:
        self.n.update_page(interaction_id, {"Data": P.text(json.dumps(data, separators=(",", ":")))})

    # ---------- capture ----------
    def save_pending(self, payload: dict, today: str, reason: str) -> str:
        page = self.n.create_page(self.inter_ds, {
            "Summary": P.title("Waiting for Gemini"), "Date": P.date(today), "Type": P.select("Note"),
            "Status": P.select("Pending"), "Source": P.select(payload["source"]), "Raw": P.text(payload.get("text", "")),
            "Data": P.text(json.dumps({"payload": payload, "tries": 0, "reason": reason[:100]})),
        })
        return page["id"]

    def bump_pending(self, interaction_id: str, give_up: bool) -> None:
        d = self.data(interaction_id)
        d["tries"] = d.get("tries", 0) + 1
        props = {"Data": P.text(json.dumps(d))}
        if give_up:
            props["Status"] = P.select("Error")
        self.n.update_page(interaction_id, props)

    def mark(self, interaction_id: str, status: str) -> None:
        self.n.update_page(interaction_id, {"Status": P.select(status)})

    def save_capture(self, ex: dict, source: str, event: str | None, today: str, existing_id: str | None = None) -> dict:
        rows = self.people()
        tiers, default_tier = self.cfg["tiers"], self.cfg["new_person_tier"]
        data = {"created": [], "snap": {}, "promises": [], "maybe": [], "card": {}}
        card_people, ids = [], []

        for p in ex["people"]:
            kind, hits = find(p, rows)
            if kind == "same":
                row = hits[0]
                props, snap = self._update_props(row, p, ex["date"])
                if props:
                    data["snap"][row["id"]] = snap
                    self.n.update_page(row["id"], props)
                card_people.append({"name": row["name"], "url": row["url"], "status": "updated",
                                    "next": _next(props, row["next"])})
                ids.append(row["id"])
                continue
            days = tiers.get(default_tier, 30)
            nxt = add_days(ex["date"], days) if days else None
            page = self.n.create_page(self.people_ds, self._new_props(p, default_tier, ex["date"], nxt))
            row = person_row(page)
            rows.append(row)
            data["created"].append(page["id"])
            ids.append(page["id"])
            card_people.append({"name": p["name"], "url": page.get("url", ""), "status": "new", "tier": default_tier, "next": nxt})
            if kind == "maybe":
                data["maybe"].append({"k": len(card_people) - 1, "new": page["id"],
                                      "cands": [h["id"] for h in hits], "names": [h["name"] for h in hits]})

        props = {"Summary": P.title(ex["summary"]), "Date": P.date(ex["date"]), "Type": P.select(ex["type"]),
                 "People": P.relation(ids), "Event": P.text(event or ""), "Status": P.select("Saved"),
                 "Source": P.select(source), "Raw": P.text(ex.get("transcript") or "")}
        if existing_id:
            self.n.update_page(existing_id, props)
            iid = existing_id
        else:
            iid = self.n.create_page(self.inter_ds, props)["id"]

        card_promises = []
        for pr in ex["promises"]:
            pid = self._promise_person(pr, ex["people"], ids)
            due = pr["due"] or add_days(today, self.cfg["promise_due_days"][pr["who"]])
            page = self.n.create_page(self.prom_ds, {
                "What": P.title(pr["what"]), "Who": P.select("Me" if pr["who"] == "me" else "Them"),
                "Person": P.relation([pid] if pid else []), "Due": P.date(due), "Done": P.check(False),
                "Interaction": P.relation([iid])})
            data["promises"].append(page["id"])
            who_name = next((c["name"] for c, i in zip(card_people, ids) if i == pid), pr["person"])
            card_promises.append({"who": pr["who"], "person": who_name, "what": pr["what"], "due": due})

        data["card"] = {"type": ex["type"], "date": ex["date"], "event": event or "", "people": card_people,
                        "promises": card_promises, "question": ex["question"]}
        self._set_data(iid, data)
        return {"id": iid, **data["card"], "maybe": data["maybe"]}

    def _new_props(self, p: dict, tier: str, day: str, nxt: str | None) -> dict:
        props = {"Name": P.title(p["name"]), "Tier": P.select(tier), "Last contact": P.date(day), "Next contact": P.date(nxt)}
        for k, name in TEXT.items():
            if p.get(k):
                props[name] = P.text(p[k])
        for k, (name, build) in OTHER.items():
            if p.get(k):
                props[name] = build(p[k])
        return props

    def _update_props(self, row: dict, p: dict, day: str) -> tuple[dict, dict]:
        """New values for an existing person, plus a snapshot of the old values (for Undo)."""
        props, snap = {}, {}
        stamp = f"[{day}] "
        for k, name in TEXT.items():
            new = p.get(k)
            if not new or new in row[k]:
                continue
            if k in APPEND:
                props[name] = P.text((row[k] + "\n" if row[k] else "") + stamp + new)
            elif not row[k]:
                props[name] = P.text(new)
            else:
                continue
            snap[name] = P.text(row[k])
        for k, (name, build) in OTHER.items():
            if p.get(k) and not row[k]:
                props[name] = build(p[k])
                snap[name] = build(None)
        if not row["last"] or day >= row["last"][:10]:
            props["Last contact"] = P.date(day)
            snap["Last contact"] = P.date(row["last"])
            days = self.cfg["tiers"].get(row["tier"] or self.cfg["new_person_tier"], 0)
            if days:
                props["Next contact"] = P.date(add_days(day, days))
                snap["Next contact"] = P.date(row["next"])
        return props, snap

    @staticmethod
    def _promise_person(pr: dict, people: list[dict], ids: list[str]) -> str | None:
        if not ids:
            return None
        if pr["person"]:
            scored = sorted(((name_score(pr["person"], p["name"]), i) for p, i in zip(people, ids)), key=lambda x: -x[0])
            if scored and scored[0][0] >= 0.72:
                return scored[0][1]
        return ids[0]

    # ---------- buttons ----------
    def undo(self, interaction_id: str) -> bool:
        page = self.n.get_page(interaction_id)
        if prop(page, "Status") == "Undone":
            return False
        d = json.loads(prop(page, "Data") or "{}")
        for pid in d.get("promises", []) + d.get("created", []):
            self.n.update_page(pid, in_trash=True)
        for pid, old in d.get("snap", {}).items():
            self.n.update_page(pid, old)
        self.n.update_page(interaction_id, {"Status": P.select("Undone"), "People": P.relation([])})
        return True

    def merge(self, interaction_id: str, k: int, j: int) -> dict:
        """The new person from maybe[k] is really candidate j: move everything over, delete the duplicate."""
        d = self.data(interaction_id)
        m = next((x for x in d["maybe"] if x["k"] == k), None)
        if not m:
            return {"id": interaction_id, **d["card"], "maybe": d["maybe"]}
        new_id, cand_id = m["new"], m["cands"][j]
        new = person_row(self.n.get_page(new_id))
        cand = person_row(self.n.get_page(cand_id))
        props, snap = self._update_props(cand, new, d["card"]["date"])
        if props:
            d["snap"].setdefault(cand_id, snap)
            self.n.update_page(cand_id, props)
        page = self.n.get_page(interaction_id)
        self.n.update_page(interaction_id, {"People": P.relation([cand_id if i == new_id else i for i in prop(page, "People") or []])})
        for pid in d["promises"]:
            pr = self.n.get_page(pid)
            if new_id in (prop(pr, "Person") or []):
                self.n.update_page(pid, {"Person": P.relation([cand_id])})
        self.n.update_page(new_id, in_trash=True)
        d["created"] = [x for x in d["created"] if x != new_id]
        d["maybe"] = [x for x in d["maybe"] if x["k"] != k]
        old_name = d["card"]["people"][k]["name"]
        d["card"]["people"][k] = {"name": cand["name"], "url": cand["url"], "status": "updated",
                                  "next": _next(props, cand["next"])}
        for pr in d["card"]["promises"]:
            if pr["person"] == old_name:
                pr["person"] = cand["name"]
        self._set_data(interaction_id, d)
        return {"id": interaction_id, **d["card"], "maybe": d["maybe"]}

    def keep_separate(self, interaction_id: str, k: int) -> dict:
        d = self.data(interaction_id)
        d["maybe"] = [x for x in d["maybe"] if x["k"] != k]
        self._set_data(interaction_id, d)
        return {"id": interaction_id, **d["card"], "maybe": d["maybe"]}
