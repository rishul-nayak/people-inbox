"""Thin wrapper over the Notion REST API (plain requests).

API version 2026-03-11 (latest at build time). Databases contain "data sources";
pages are created under a data source, and rows are queried via /data_sources/{id}/query.
"""
from __future__ import annotations

import time

import requests

from common import APIError, http

NOTION_VERSION = "2026-03-11"
BASE = "https://api.notion.com/v1"
MAX_TEXT = 1900   # Notion limit is 2000 chars per rich_text item


class Notion:
    def __init__(self, token: str, retries: int = 3):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.retries = retries

    def req(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        time.sleep(0.34)  # stay under ~3 requests/second
        return http(self.s, method, BASE + path, retries=self.retries, json=body, params=params)

    # ---- databases / data sources ----
    def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        return self.req("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "is_inline": False,
            "initial_data_source": {"properties": properties},
        })

    def data_source_id(self, database_id: str) -> str:
        db = self.req("GET", f"/databases/{database_id}")
        sources = db.get("data_sources") or []
        if not sources:
            raise APIError(0, "Database has no data source (wrong NOTION_DATABASE_ID?)")
        return sources[0]["id"]

    def get_data_source(self, ds_id: str) -> dict:
        return self.req("GET", f"/data_sources/{ds_id}")

    def update_data_source(self, ds_id: str, properties: dict) -> dict:
        return self.req("PATCH", f"/data_sources/{ds_id}", {"properties": properties})

    def get_page(self, page_id: str) -> dict:
        return self.req("GET", f"/pages/{page_id}")

    def query(self, ds_id: str, filter: dict | None = None, sorts: list | None = None, limit: int | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            body: dict = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            r = self.req("POST", f"/data_sources/{ds_id}/query", body)
            out.extend(r.get("results", []))
            if limit and len(out) >= limit:
                return out[:limit]
            if not r.get("has_more"):
                return out
            cursor = r.get("next_cursor")

    # ---- pages ----
    def create_page(self, ds_id: str, properties: dict, children: list | None = None) -> dict:
        body = {"parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": properties}
        if children:
            body["children"] = children[:100]
        return self.req("POST", "/pages", body)

    def create_child_page(self, parent_page_id: str, title: str, children: list) -> dict:
        page = self.req("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            "children": children[:100],
        })
        if len(children) > 100:
            self.append_children(page["id"], children[100:])
        return page

    def update_page(self, page_id: str, properties: dict | None = None, in_trash: bool | None = None) -> dict:
        body: dict = {}
        if properties:
            body["properties"] = properties
        if in_trash is not None:
            body["in_trash"] = in_trash
        return self.req("PATCH", f"/pages/{page_id}", body)

    def append_children(self, block_id: str, children: list) -> None:
        for i in range(0, len(children), 100):
            self.req("PATCH", f"/blocks/{block_id}/children", {"children": children[i:i + 100]})

    def list_children(self, block_id: str) -> list[dict]:
        r = self.req("GET", f"/blocks/{block_id}/children", params={"page_size": 100})
        return r.get("results", [])

    def delete_block(self, block_id: str) -> None:
        self.req("DELETE", f"/blocks/{block_id}")

    # ---- views ----
    def list_views(self, database_id: str) -> list[dict]:
        r = self.req("GET", "/views", params={"database_id": database_id})
        views = []
        for v in r.get("results", []):
            if "name" not in v:
                v = self.req("GET", f"/views/{v['id']}")
            views.append(v)
        return views

    def create_view(self, database_id: str, ds_id: str, name: str, filter: dict | None = None,
                    sorts: list | None = None, view_type: str = "table") -> dict:
        body = {"database_id": database_id, "data_source_id": ds_id, "name": name, "type": view_type}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        return self.req("POST", "/views", body)

    def update_view(self, view_id: str, **fields) -> dict:
        return self.req("PATCH", f"/views/{view_id}", fields)


# ---------- property builders ----------
def _chunks(s: str) -> list[dict]:
    s = s or ""
    return [{"type": "text", "text": {"content": s[i:i + MAX_TEXT]}} for i in range(0, len(s), MAX_TEXT)][:20]


class P:
    @staticmethod
    def title(s): return {"title": _chunks(s)[:1] or []}
    @staticmethod
    def text(s): return {"rich_text": _chunks(s)}
    @staticmethod
    def select(name): return {"select": {"name": clean_option(name)} if name else None}
    @staticmethod
    def multi(names): return {"multi_select": [{"name": clean_option(n)} for n in dict.fromkeys(n for n in (names or []) if n)]}
    @staticmethod
    def number(x): return {"number": x}
    @staticmethod
    def check(b): return {"checkbox": bool(b)}
    @staticmethod
    def date(iso): return {"date": {"start": iso} if iso else None}
    @staticmethod
    def url(u): return {"url": u or None}
    @staticmethod
    def email(e): return {"email": e or None}
    @staticmethod
    def phone(x): return {"phone_number": x or None}
    @staticmethod
    def relation(ids): return {"relation": [{"id": i} for i in dict.fromkeys(i for i in (ids or []) if i)]}


def clean_option(name: str) -> str:
    """Select option names can't contain commas and are capped at 100 chars."""
    return str(name).replace(",", " ").strip()[:100]


# ---------- property readers ----------
def prop(page: dict, name: str):
    p = page.get("properties", {}).get(name)
    if not p:
        return None
    t = p["type"]
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v or [])
    if t == "select":
        return v["name"] if v else None
    if t == "multi_select":
        return [x["name"] for x in v or []]
    if t == "date":
        return v["start"] if v else None
    if t == "relation":
        return [x["id"] for x in v or []]
    return v  # number, checkbox, url, email, phone_number


# ---------- block builders ----------
def heading(s): return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _chunks(s)}}
def para(s): return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _chunks(s)}}
def bullet(s): return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _chunks(s)}}
def quote(s): return {"object": "block", "type": "quote", "quote": {"rich_text": _chunks(s)}}
def todo(s, checked=False): return {"object": "block", "type": "to_do", "to_do": {"rich_text": _chunks(s), "checked": checked}}
def divider(): return {"object": "block", "type": "divider", "divider": {}}
