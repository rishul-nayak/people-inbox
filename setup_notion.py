"""One-time: create a "People Inbox" page with 3 linked databases (People, Interactions, Promises),
then write their IDs into config.yaml. The workflow commits config.yaml.

Run it from GitHub: Actions -> "Setup Notion (one time)" -> Run workflow.
Safe to re-run: if the IDs are already in config.yaml, it only (re)creates missing views.
"""
from __future__ import annotations

import sys

from common import env, load_config, load_dotenv, log, setting, write_ids
from notion_client import Notion, para

SEL = lambda *names: {"select": {"options": [{"name": n} for n in names]}}  # noqa: E731
TXT = {"rich_text": {}}
DATE = {"date": {}}


def people_props(cfg: dict) -> dict:
    return {
        "Name": {"title": {}},
        "Tier": SEL(*cfg["tiers"].keys()),
        "World": SEL(*cfg["worlds"]),
        "Company": TXT, "Role": TXT, "How we met": TXT,
        "Cares about": TXT, "Personal notes": TXT, "Ideas to help": TXT, "Common ground": TXT, "Mutual contacts": TXT,
        "LinkedIn": {"url": {}}, "Email": {"email": {}}, "Phone": {"phone_number": {}},
        "Last contact": DATE, "Next contact": DATE,
    }


def rel(ds_id: str, two_way: bool) -> dict:
    kind = "dual_property" if two_way else "single_property"
    return {"relation": {"data_source_id": ds_id, "type": kind, kind: {}}}


def create(n: Notion, parent: str, title: str, props: dict) -> tuple[str, str]:
    db = n.create_database(parent, title, props)
    ds = (db.get("data_sources") or [{}])[0].get("id") or n.data_source_id(db["id"])
    log(f"Created database '{title}'.")
    return db["id"], ds


def rename_synced(n: Notion, people_ds: str, target_ds: str, name: str) -> None:
    """A two-way relation adds a property to People with an automatic name; give it a clear one."""
    try:
        props = n.get_data_source(people_ds).get("properties", {})
        for old, p in props.items():
            if p.get("type") == "relation" and p["relation"].get("data_source_id") == target_ds and old != name:
                n.update_data_source(people_ds, {old: {"name": name}})
                log(f"  People: '{old}' renamed to '{name}'")
    except Exception as e:
        log(f"  (could not rename the {name} column: {str(e)[:120]}; rename it by hand if you like)")


def views(ids: dict) -> list[tuple[str, str, str, dict | None, list | None]]:
    open_ = {"property": "Done", "checkbox": {"equals": False}}
    return [
        (ids["PEOPLE_DB_ID"], ids["PEOPLE_DS_ID"], "Next to contact", None,
         [{"property": "Next contact", "direction": "ascending"}]),
        (ids["PEOPLE_DB_ID"], ids["PEOPLE_DS_ID"], "Newest", None, [{"timestamp": "created_time", "direction": "descending"}]),
        (ids["PROMISES_DB_ID"], ids["PROMISES_DS_ID"], "I owe",
         {"and": [open_, {"property": "Who", "select": {"equals": "Me"}}]}, [{"property": "Due", "direction": "ascending"}]),
        (ids["PROMISES_DB_ID"], ids["PROMISES_DS_ID"], "They owe me",
         {"and": [open_, {"property": "Who", "select": {"equals": "Them"}}]}, [{"property": "Due", "direction": "ascending"}]),
        (ids["INTERACTIONS_DB_ID"], ids["INTERACTIONS_DS_ID"], "Recent", None, [{"property": "Date", "direction": "descending"}]),
        (ids["INTERACTIONS_DB_ID"], ids["INTERACTIONS_DS_ID"], "Not saved",
         {"or": [{"property": "Status", "select": {"equals": "Pending"}}, {"property": "Status", "select": {"equals": "Error"}}]}, None),
    ]


def main() -> int:
    load_dotenv()
    cfg = load_config()
    n = Notion(env("NOTION_TOKEN"))
    keys = ["PEOPLE_DB_ID", "PEOPLE_DS_ID", "INTERACTIONS_DB_ID", "INTERACTIONS_DS_ID", "PROMISES_DB_ID", "PROMISES_DS_ID"]
    ids = {k: str((cfg.get("ids") or {}).get(k) or "") for k in keys}

    if all(ids.values()):
        log("IDs already in config.yaml; checking views only.")
    else:
        parent = setting("NOTION_PARENT_PAGE_ID", cfg)
        home = n.create_child_page(parent, "People Inbox", [
            para("Everyone you meet lands here from the Telegram bot. Edit anything by hand; the bot never overwrites your edits.")])
        ids["PEOPLE_DB_ID"], ids["PEOPLE_DS_ID"] = create(n, home["id"], "People", people_props(cfg))
        ids["INTERACTIONS_DB_ID"], ids["INTERACTIONS_DS_ID"] = create(n, home["id"], "Interactions", {
            "Summary": {"title": {}}, "Date": DATE,
            "Type": SEL("Meeting", "Call", "Event", "Message", "Note", "Contact card"),
            "People": rel(ids["PEOPLE_DS_ID"], two_way=True), "Event": TXT,
            "Source": SEL("Voice", "Text", "Photo", "Forward"),
            "Status": SEL("Saved", "Pending", "Undone", "Error"), "Raw": TXT, "Data": TXT,
        })
        rename_synced(n, ids["PEOPLE_DS_ID"], ids["INTERACTIONS_DS_ID"], "Interactions")
        ids["PROMISES_DB_ID"], ids["PROMISES_DS_ID"] = create(n, home["id"], "Promises", {
            "What": {"title": {}}, "Who": SEL("Me", "Them"),
            "Person": rel(ids["PEOPLE_DS_ID"], two_way=True), "Due": DATE, "Done": {"checkbox": {}},
            "Interaction": rel(ids["INTERACTIONS_DS_ID"], two_way=False),
        })
        rename_synced(n, ids["PEOPLE_DS_ID"], ids["PROMISES_DS_ID"], "Promises")
        write_ids(ids)
        log("IDs written to config.yaml (the workflow commits it).")

    failed = 0
    for db_id, ds_id, name, flt, sorts in views(ids):
        try:
            existing = {v.get("name") for v in n.list_views(db_id)}
            if name not in existing:
                n.create_view(db_id, ds_id, name, flt, sorts)
                log(f"  view created: {name}")
        except Exception as e:
            failed += 1
            log(f"  view skipped: {name}: {str(e)[:150]}")
    if failed:
        log(f"{failed} views could not be created by the API. The databases work without them.")
    log("Done. Next: add the TELEGRAM_OWNER_ID secret, then send your bot a voice note.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
