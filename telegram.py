"""Minimal Telegram Bot API client (plain requests). Polling only: no webhook, no server."""
from __future__ import annotations

import requests

from common import APIError, http

MAX_FILE = 19 * 1024 * 1024   # Bot API download limit is 20 MB


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.s = requests.Session()

    def call(self, method: str, **params) -> dict | list | bool:
        r = http(self.s, "POST", f"{self.base}/{method}", json=params)
        if not r.get("ok"):
            raise APIError(0, r.get("description", "telegram error"))
        return r["result"]

    def updates(self) -> list[dict]:
        """Everything not yet confirmed. Telegram keeps updates for 24 hours."""
        return self.call("getUpdates", timeout=0, limit=100, allowed_updates=["message", "callback_query"])

    def ack(self, update_id: int) -> None:
        """Confirm every update up to and including update_id, so it is never delivered again."""
        self.call("getUpdates", offset=update_id + 1, timeout=0, limit=1)

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None,
             reply_to: int | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = _keyboard(buttons)
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}, "reply_markup": _keyboard(buttons or [])}
        try:
            self.call("editMessageText", **params)
        except APIError as e:
            if "not modified" not in str(e):
                raise

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except APIError:
            pass   # old button presses expire after a while; harmless

    def download(self, file_id: str) -> bytes:
        f = self.call("getFile", file_id=file_id)
        if (f.get("file_size") or 0) > MAX_FILE:
            raise APIError(413, "file is larger than 20 MB")
        r = self.s.get(f"{self.file_base}/{f['file_path']}", timeout=60)
        if r.status_code != 200:
            raise APIError(r.status_code, "file download failed")
        return r.content


def _keyboard(buttons: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
