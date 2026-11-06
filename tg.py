"""Минимальный клиент Telegram Bot API на стандартной библиотеке Python."""
import json
import logging
import os
import urllib.error
import urllib.request

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
log = logging.getLogger("tg")


class TG:
    def __init__(self, token: str):
        self.token = token.strip()

    def call(self, method: str, http_timeout: int = 40, **params):
        url = f"{API_BASE}/bot{self.token}/{method}"
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.warning("%s HTTP %s: %s", method, e.code, e.read().decode("utf-8", "replace")[:300])
            return None
        if not data.get("ok"):
            log.warning("%s error: %s", method, data)
            return None
        return data.get("result")

    def send(self, chat_id, text, markup=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if markup is not None:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)

    def answer(self, callback_id, text=""):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def drop_buttons(self, chat_id, message_id):
        if message_id:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                      reply_markup={"inline_keyboard": []})


def esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
