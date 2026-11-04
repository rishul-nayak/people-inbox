"""In-memory fakes of Notion, Telegram and Gemini, so the whole bot runs without the internet."""
import copy
import uuid

from common import load_config
from extract import QuotaExhausted, normalise


def cfg():
    c = copy.deepcopy(load_config())
    c["ids"].update({k: k.lower() for k in ["PEOPLE_DB_ID", "PEOPLE_DS_ID", "INTERACTIONS_DB_ID",
                                            "INTERACTIONS_DS_ID", "PROMISES_DB_ID", "PROMISES_DS_ID"]})
    return c


def _shape(v):
    t = next(iter(v))
    val = v[t]
    if t in ("title", "rich_text"):
        val = [dict(x, plain_text=x["text"]["content"]) for x in val]
    return {"type": t, t: val}


def _value(page, name):
    p = page["properties"].get(name)
    if not p:
        return None
    v = p[p["type"]]
    if p["type"] == "select":
        return v["name"] if v else None
    if p["type"] == "date":
        return v["start"] if v else None
    return v


def _match(page, f):
    if not f:
        return True
    if "and" in f:
        return all(_match(page, x) for x in f["and"])
    if "or" in f:
        return any(_match(page, x) for x in f["or"])
    v = _value(page, f["property"])
    for kind in ("select", "checkbox", "date"):
        if kind in f:
            cond = f[kind]
            if "equals" in cond:
                return v == cond["equals"]
    raise NotImplementedError(f)


class FakeNotion:
    def __init__(self):
        self.pages = {}

    def query(self, ds, filter=None, sorts=None, limit=None):
        return [p for p in self.pages.values() if p["ds"] == ds and not p.get("in_trash") and _match(p, filter)]

    def create_page(self, ds, props, children=None):
        pid = str(uuid.uuid4())
        self.pages[pid] = {"id": pid, "ds": ds, "url": f"https://notion.so/{pid.replace('-', '')}",
                           "properties": {k: _shape(v) for k, v in props.items()}}
        return self.pages[pid]

    def get_page(self, pid):
        return self.pages[pid]

    def update_page(self, pid, props=None, in_trash=None):
        if in_trash is not None:
            self.pages[pid]["in_trash"] = in_trash
        for k, v in (props or {}).items():
            self.pages[pid]["properties"][k] = _shape(v)

    def rows(self, ds):
        return self.query(ds)


class FakeTelegram:
    def __init__(self, updates=None):
        self.queue = list(updates or [])
        self.sent, self.edits, self.acked, self.answers = [], [], [], []

    def updates(self):
        return list(self.queue)

    def ack(self, uid):
        self.acked.append(uid)
        self.queue = [u for u in self.queue if u["update_id"] > uid]

    def send(self, chat, text, buttons=None, reply_to=None):
        self.sent.append({"chat": chat, "text": text, "buttons": buttons or []})
        return {"message_id": len(self.sent)}

    def edit(self, chat, mid, text, buttons=None):
        self.edits.append({"text": text, "buttons": buttons or []})

    def answer(self, cid, text=""):
        self.answers.append(text)

    def download(self, file_id):
        return b"OggS-fake-audio"


class FakeExtractor:
    """Returns the next scripted answer; an Exception in the script is raised instead."""
    def __init__(self, cfg, script):
        self.cfg, self.script, self.calls = cfg, list(script), []

    def extract(self, text, media, today, event=None):
        self.calls.append({"text": text, "media": media, "event": event})
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return normalise(nxt, self.cfg, today)


OWNER = 111


def msg(uid, text="", **extra):
    m = {"message_id": uid, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "text": text}
    m.update(extra)
    return {"update_id": uid, "message": m}


def button(uid, data, mid=1):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "from": {"id": OWNER}, "data": data,
                                                 "message": {"message_id": mid, "chat": {"id": OWNER}}}}


__all__ = ["cfg", "FakeNotion", "FakeTelegram", "FakeExtractor", "QuotaExhausted", "msg", "button", "OWNER"]
