"""Бот одного клиента (салон, барбершоп, клиника…): услуги, запись, вопросы, контакты.

Каждый клиент живёт в своей папке clients/<имя>/ с config.json и базой data.db.
Владелец бизнеса (его chat_id в admin_chat_ids) получает уведомления о записях
и свою админку: /admin — сменить цену, посмотреть записи, выключить запись.
"""
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
from pathlib import Path

from tg import TG, esc

LANGS = ("ru", "kk")
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "30"))
WEEKDAYS = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сн", "Жс"],
}


class ClientBot:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.slug = self.folder.name
        self.config_path = self.folder / "config.json"
        self.db_path = self.folder / "data.db"
        self.lock = threading.RLock()
        self.cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.tg = TG(self.cfg.get("token", ""))
        self.state = {}
        self.admin_state = {}
        self.stop_flag = threading.Event()
        self.thread = None
        self.username = self.cfg.get("bot_username", "")
        self.log = logging.getLogger(f"client.{self.slug}")
        self.init_db()

    # ------------------------------------------------------------------ config
    def save_config(self):
        with self.lock:
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.config_path)

    @property
    def services(self):
        return self.cfg["services"]

    @property
    def admins(self):
        return {int(x) for x in self.cfg.get("admin_chat_ids", []) if str(x).strip()}

    @property
    def default_lang(self):
        return self.cfg.get("default_language", "ru")

    def fill(self, text, lang, **kw):
        biz = self.cfg["business"]
        values = {
            "name": biz.get("name", ""),
            "address": biz.get(f"address_{lang}") or biz.get("address_ru", ""),
            "phone": biz.get("phone", ""),
            "hours": biz.get(f"hours_{lang}") or biz.get("hours_ru", ""),
            "instagram": biz.get("instagram", ""),
            "map_url": biz.get("map_url", ""),
        }
        values.update(kw)
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return text

    def t(self, lang, key, **kw):
        texts = self.cfg["texts"]
        text = texts.get(lang, {}).get(key) or texts[self.default_lang].get(key, key)
        return self.fill(text, lang, **kw)

    # ------------------------------------------------------------------ db
    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, lang TEXT, "
                      "username TEXT, first_seen TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "chat_id INTEGER, username TEXT, name TEXT, phone TEXT, service TEXT, price TEXT, "
                      "date TEXT, time TEXT, lang TEXT, created_at TEXT)")

    def get_lang(self, chat_id):
        with self.db() as c:
            r = c.execute("SELECT lang FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return r["lang"] if r else None

    def set_lang(self, chat_id, lang, username=""):
        with self.db() as c:
            c.execute("INSERT INTO users(chat_id, lang, username, first_seen) VALUES(?,?,?,?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang, username=excluded.username",
                      (chat_id, lang, username, dt.datetime.now().isoformat(timespec="seconds")))

    def taken(self, date_iso):
        with self.db() as c:
            return {r["time"] for r in c.execute("SELECT time FROM bookings WHERE date=?", (date_iso,))}

    def last_bookings(self, limit=15):
        with self.db() as c:
            return c.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def stats(self):
        week_ago = (dt.datetime.now() - dt.timedelta(days=7)).isoformat(timespec="seconds")
        with self.db() as c:
            total = c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
            week = c.execute("SELECT COUNT(*) FROM bookings WHERE created_at>=?", (week_ago,)).fetchone()[0]
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return total, week, users

    # ------------------------------------------------------------------ helpers
    def svc(self, sid):
        for s in self.services:
            if s["id"] == sid:
                return s
        return None

    def svc_name(self, sid, lang):
        s = self.svc(sid)
        return (s.get(lang) or s["ru"]) if s else sid

    def now_local(self):
        off = int(self.cfg.get("utc_offset_hours", 5))
        return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=off)

    def times_for(self, date_iso):
        b = self.cfg.get("booking", {})
        busy = self.taken(date_iso) if b.get("one_client_per_slot", True) else set()
        now = self.now_local()
        out = []
        for s in b.get("time_slots", []):
            if s in busy:
                continue
            if date_iso == now.date().isoformat():
                h, m = map(int, s.split(":"))
                if (h, m) <= (now.hour + 1, now.minute):
                    continue
            out.append(s)
        return out

    def dates(self):
        b = self.cfg.get("booking", {})
        n = int(b.get("days_ahead", 7))
        closed = set(b.get("closed_weekdays", []))
        today = self.now_local().date()
        out = []
        for i in range(n + 7):
            d = today + dt.timedelta(days=i)
            if d.weekday() in closed or not self.times_for(d.isoformat()):
                continue
            out.append(d)
            if len(out) >= n:
                break
        return out

    def fmt_date(self, date_iso, lang):
        d = dt.date.fromisoformat(date_iso)
        return f"{WEEKDAYS[lang][d.weekday()]}, {d.strftime('%d.%m.%Y')}"

    # ------------------------------------------------------------------ keyboards
    def kb_main(self, lang):
        return {"keyboard": [
            [{"text": self.t(lang, "btn_book")}],
            [{"text": self.t(lang, "btn_prices")}, {"text": self.t(lang, "btn_faq")}],
            [{"text": self.t(lang, "btn_contacts")}, {"text": self.t(lang, "btn_lang")}],
        ], "resize_keyboard": True}

    def kb_cancel(self, lang):
        return {"keyboard": [[{"text": self.t(lang, "btn_cancel")}]], "resize_keyboard": True}

    def kb_phone(self, lang):
        return {"keyboard": [[{"text": self.t(lang, "btn_send_phone"), "request_contact": True}],
                             [{"text": self.t(lang, "btn_cancel")}]],
                "resize_keyboard": True, "one_time_keyboard": True}

    @staticmethod
    def kb_lang():
        return {"inline_keyboard": [[{"text": "🇰🇿 Қазақша", "callback_data": "lang:kk"},
                                     {"text": "🇷🇺 Русский", "callback_data": "lang:ru"}]]}

    def kb_services(self, lang, prefix="svc"):
        return {"inline_keyboard": [[{"text": f"{s.get(lang) or s['ru']} — {s['price']}",
                                      "callback_data": f"{prefix}:{s['id']}"}] for s in self.services]}

    def kb_dates(self, lang):
        rows, row = [], []
        for d in self.dates():
            row.append({"text": f"{WEEKDAYS[lang][d.weekday()]} {d.strftime('%d.%m')}",
                        "callback_data": f"date:{d.isoformat()}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return {"inline_keyboard": rows}

    def kb_times(self, date_iso):
        rows, row = [], []
        for s in self.times_for(date_iso):
            row.append({"text": s, "callback_data": f"time:{s}"})
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return {"inline_keyboard": rows}

    def kb_confirm(self, lang):
        return {"inline_keyboard": [[{"text": self.t(lang, "btn_confirm"), "callback_data": "confirm:yes"},
                                     {"text": self.t(lang, "btn_cancel"), "callback_data": "confirm:no"}]]}

    def kb_faq(self, lang):
        return {"inline_keyboard": [[{"text": f["q"].get(lang) or f["q"]["ru"], "callback_data": f"faq:{i}"}]
                                    for i, f in enumerate(self.cfg.get("faq", []))]}

    def kb_owner(self):
        on = self.cfg.get("booking", {}).get("enabled", True)
        return {"inline_keyboard": [
            [{"text": "💰 Изменить цену", "callback_data": "own:price"}],
            [{"text": "📋 Последние записи", "callback_data": "own:list"}],
            [{"text": "⏸ Остановить онлайн-запись" if on else "▶️ Включить онлайн-запись",
              "callback_data": "own:toggle"}],
        ]}

    # ------------------------------------------------------------------ screens
    def menu(self, chat_id, lang, key="menu"):
        self.state.pop(chat_id, None)
        self.tg.send(chat_id, self.t(lang, key), self.kb_main(lang))

    def summary(self, d, lang):
        s = self.svc(d["service"])
        return self.t(lang, "confirm_text", service=esc(self.svc_name(d["service"], lang)),
                      price=esc(s["price"] if s else ""), date=esc(self.fmt_date(d["date"], lang)),
                      time=esc(d["time"]), client=esc(d["name"]), client_phone=esc(d["phone"]))

    def bookings_text(self):
        rows = self.last_bookings()
        if not rows:
            return "Записей пока нет."
        lines = ["<b>Последние записи:</b>"]
        for r in rows:
            lines.append(f"#{r['id']} · {esc(self.fmt_date(r['date'], 'ru'))} {esc(r['time'])} · "
                         f"{esc(r['service'])} · {esc(r['name'])} · {esc(r['phone'])}")
        return "\n".join(lines)

    def notify(self, bid, d, username, lang):
        s = self.svc(d["service"])
        text = (f"🆕 <b>Новая запись #{bid}</b>\n\n"
                f"Услуга: <b>{esc(s['ru'] if s else d['service'])}</b> ({esc(s['price'] if s else '')})\n"
                f"Дата: <b>{esc(self.fmt_date(d['date'], 'ru'))}</b>\nВремя: <b>{esc(d['time'])}</b>\n"
                f"Клиент: {esc(d['name'])}\nТелефон: {esc(d['phone'])}\n"
                f"Telegram: {esc('@' + username if username else '—')}\n"
                f"Язык: {'казахский' if lang == 'kk' else 'русский'}")
        if not self.admins:
            self.log.warning("Нет admin_chat_ids — запись #%s никому не отправлена", bid)
        for a in self.admins:
            self.tg.send(a, text)

    def which_button(self, text):
        for key in ("btn_book", "btn_prices", "btn_faq", "btn_contacts", "btn_lang", "btn_cancel"):
            for lg in LANGS:
                if text == self.t(lg, key):
                    return key
        return None

    # ------------------------------------------------------------------ messages
    def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        if msg["chat"].get("type") != "private":
            return
        username = (msg.get("from") or {}).get("username") or ""
        text = (msg.get("text") or "").strip()

        if text.startswith("/id"):
            self.tg.send(chat_id, f"Ваш chat_id: <code>{chat_id}</code>\n"
                                  f"Передайте это число разработчику бота, чтобы получать записи.")
            return
        if chat_id in self.admins:
            if text.startswith("/admin") or text.startswith("/bookings"):
                self.admin_state.pop(chat_id, None)
                if text.startswith("/bookings"):
                    self.tg.send(chat_id, self.bookings_text())
                else:
                    self.tg.send(chat_id, f"⚙️ <b>Управление ботом {esc(self.cfg['business']['name'])}</b>",
                                 self.kb_owner())
                return
            st = self.admin_state.get(chat_id)
            if st and st.get("step") == "price" and text and not text.startswith("/"):
                s = self.svc(st["sid"])
                self.admin_state.pop(chat_id, None)
                if s:
                    with self.lock:
                        s["price"] = normalize_price(text)
                        self.save_config()
                    self.tg.send(chat_id, f"✅ Цена обновлена: {esc(s['ru'])} — <b>{esc(s['price'])}</b>",
                                 self.kb_owner())
                return
        elif text.startswith("/admin") or text.startswith("/bookings"):
            self.tg.send(chat_id, "Эта команда только для владельца бизнеса.")
            return

        lang = self.get_lang(chat_id)
        if text.startswith("/start") or lang is None:
            if lang is None:
                self.tg.send(chat_id, self.cfg["texts"]["ru"]["choose_lang"] + "\n" +
                             self.cfg["texts"]["kk"]["choose_lang"], self.kb_lang())
            else:
                self.menu(chat_id, lang, "welcome")
            return
        if text.startswith("/menu"):
            self.menu(chat_id, lang)
            return

        action = self.which_button(text)
        st = self.state.get(chat_id)
        if action == "btn_cancel":
            self.menu(chat_id, lang, "cancelled")
            return
        if st and action is None:
            d = st["data"]
            if st["step"] == "name":
                if not 2 <= len(text) <= 60:
                    self.tg.send(chat_id, self.t(lang, "ask_name"))
                    return
                d["name"] = text
                st["step"] = "phone"
                self.tg.send(chat_id, self.t(lang, "ask_phone"), self.kb_phone(lang))
                return
            if st["step"] == "phone":
                phone = (msg.get("contact") or {}).get("phone_number")
                if not phone and text and 10 <= len(re.sub(r"\D", "", text)) <= 15:
                    phone = text
                if not phone:
                    self.tg.send(chat_id, self.t(lang, "bad_phone"), self.kb_phone(lang))
                    return
                d["phone"] = phone
                st["step"] = "confirm"
                self.tg.send(chat_id, self.t(lang, "almost_done"), self.kb_cancel(lang))
                self.tg.send(chat_id, self.summary(d, lang), self.kb_confirm(lang))
                return
            self.tg.send(chat_id, self.t(lang, "use_buttons"))
            return

        if action == "btn_book":
            if not self.cfg.get("booking", {}).get("enabled", True):
                self.tg.send(chat_id, self.t(lang, "booking_disabled"), self.kb_main(lang))
                return
            self.state[chat_id] = {"step": "service", "data": {}}
            self.tg.send(chat_id, self.t(lang, "book_intro"), self.kb_cancel(lang))
            self.tg.send(chat_id, self.t(lang, "ask_service"), self.kb_services(lang))
        elif action == "btn_prices":
            lines = [self.t(lang, "prices_title")]
            lines += [f"• {esc(s.get(lang) or s['ru'])} — <b>{esc(s['price'])}</b>" for s in self.services]
            lines += ["", self.t(lang, "prices_footer")]
            self.tg.send(chat_id, "\n".join(lines), self.kb_main(lang))
        elif action == "btn_faq" and self.cfg.get("faq"):
            self.tg.send(chat_id, self.t(lang, "faq_title"), self.kb_faq(lang))
        elif action in ("btn_contacts", "btn_faq"):
            self.tg.send(chat_id, self.t(lang, "contacts"), self.kb_main(lang))
        elif action == "btn_lang":
            self.tg.send(chat_id, self.cfg["texts"]["ru"]["choose_lang"] + "\n" +
                         self.cfg["texts"]["kk"]["choose_lang"], self.kb_lang())
        else:
            self.tg.send(chat_id, self.t(lang, "unknown"), self.kb_main(lang))

    # ------------------------------------------------------------------ callbacks
    def on_callback(self, cb):
        cid = cb["id"]
        m = cb.get("message") or {}
        chat_id = (m.get("chat") or {}).get("id")
        mid = m.get("message_id")
        data = cb.get("data", "")
        username = (cb.get("from") or {}).get("username") or ""
        if chat_id is None:
            self.tg.answer(cid)
            return

        if data.startswith("own:") or data.startswith("ownp:"):
            self.tg.answer(cid)
            if chat_id not in self.admins:
                return
            if data == "own:price":
                self.tg.send(chat_id, "Выберите услугу:", self.kb_services("ru", "ownp"))
            elif data == "own:list":
                self.tg.send(chat_id, self.bookings_text())
            elif data == "own:toggle":
                with self.lock:
                    b = self.cfg.setdefault("booking", {})
                    b["enabled"] = not b.get("enabled", True)
                    self.save_config()
                self.tg.drop_buttons(chat_id, mid)
                self.tg.send(chat_id, "✅ Онлайн-запись включена." if b["enabled"]
                             else "⏸ Онлайн-запись остановлена. Клиенты увидят ваш телефон.", self.kb_owner())
            elif data.startswith("ownp:"):
                sid = data.split(":", 1)[1]
                s = self.svc(sid)
                if s:
                    self.admin_state[chat_id] = {"step": "price", "sid": sid}
                    self.tg.drop_buttons(chat_id, mid)
                    self.tg.send(chat_id, f"{esc(s['ru'])}: сейчас <b>{esc(s['price'])}</b>.\n"
                                          f"Напишите новую цену, например: 6000")
            return

        if data.startswith("lang:"):
            lang = data.split(":", 1)[1]
            lang = lang if lang in LANGS else self.default_lang
            self.set_lang(chat_id, lang, username)
            self.tg.answer(cid)
            self.tg.drop_buttons(chat_id, mid)
            self.menu(chat_id, lang, "welcome")
            return

        lang = self.get_lang(chat_id) or self.default_lang
        if data.startswith("faq:"):
            self.tg.answer(cid)
            try:
                f = self.cfg.get("faq", [])[int(data.split(":", 1)[1])]
            except (ValueError, IndexError):
                return
            q = f["q"].get(lang) or f["q"]["ru"]
            a = self.fill(f["a"].get(lang) or f["a"]["ru"], lang)
            self.tg.send(chat_id, f"<b>{esc(q)}</b>\n\n{a}")
            return

        st = self.state.get(chat_id)
        if not st:
            self.tg.answer(cid, self.t(lang, "expired"))
            self.tg.drop_buttons(chat_id, mid)
            return
        d = st["data"]

        if data.startswith("svc:") and st["step"] == "service":
            sid = data.split(":", 1)[1]
            self.tg.answer(cid)
            if not self.svc(sid):
                return
            d["service"] = sid
            st["step"] = "date"
            self.tg.drop_buttons(chat_id, mid)
            kb = self.kb_dates(lang)
            if not kb["inline_keyboard"]:
                self.menu(chat_id, lang, "no_slots")
                return
            self.tg.send(chat_id, self.t(lang, "ask_date", service=esc(self.svc_name(sid, lang))), kb)
        elif data.startswith("date:") and st["step"] == "date":
            date_iso = data.split(":", 1)[1]
            self.tg.answer(cid)
            kb = self.kb_times(date_iso)
            if not kb["inline_keyboard"]:
                self.tg.send(chat_id, self.t(lang, "day_full"), self.kb_dates(lang))
                return
            d["date"] = date_iso
            st["step"] = "time"
            self.tg.drop_buttons(chat_id, mid)
            self.tg.send(chat_id, self.t(lang, "ask_time", date=esc(self.fmt_date(date_iso, lang))), kb)
        elif data.startswith("time:") and st["step"] == "time":
            slot = data.split(":", 1)[1]
            self.tg.answer(cid)
            if slot not in self.times_for(d["date"]):
                self.tg.send(chat_id, self.t(lang, "slot_taken"), self.kb_times(d["date"]))
                return
            d["time"] = slot
            st["step"] = "name"
            self.tg.drop_buttons(chat_id, mid)
            self.tg.send(chat_id, self.t(lang, "ask_name"), self.kb_cancel(lang))
        elif data.startswith("confirm:") and st["step"] == "confirm":
            self.tg.drop_buttons(chat_id, mid)
            if data == "confirm:no":
                self.tg.answer(cid)
                self.menu(chat_id, lang, "cancelled")
                return
            with self.lock:
                if d["time"] not in self.times_for(d["date"]):
                    self.tg.answer(cid)
                    st["step"] = "time"
                    self.tg.send(chat_id, self.t(lang, "slot_taken"), self.kb_times(d["date"]))
                    return
                s = self.svc(d["service"])
                with self.db() as c:
                    bid = c.execute(
                        "INSERT INTO bookings(chat_id, username, name, phone, service, price, date, time, lang, "
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (chat_id, username, d["name"], d["phone"], s["ru"] if s else d["service"],
                         s["price"] if s else "", d["date"], d["time"], lang,
                         dt.datetime.now().isoformat(timespec="seconds"))).lastrowid
            self.tg.answer(cid, "✅")
            self.notify(bid, d, username, lang)
            self.state.pop(chat_id, None)
            self.tg.send(chat_id, self.t(lang, "booked", id=bid), self.kb_main(lang))
        else:
            self.tg.answer(cid)

    def handle(self, upd):
        try:
            if "message" in upd:
                self.on_message(upd["message"])
            elif "callback_query" in upd:
                self.on_callback(upd["callback_query"])
        except Exception:
            self.log.exception("Ошибка в update %s", upd.get("update_id"))

    # ------------------------------------------------------------------ run
    def loop(self):
        offset = None
        while not self.stop_flag.is_set():
            try:
                params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                for upd in self.tg.call("getUpdates", http_timeout=POLL_TIMEOUT + 10, **params) or []:
                    offset = upd["update_id"] + 1
                    self.handle(upd)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                self.log.warning("Сеть: %s, повтор через 5 с", e)
                self.stop_flag.wait(5)

    def start(self):
        if self.thread and self.thread.is_alive():
            return True
        me = self.tg.call("getMe", http_timeout=15)
        if not me:
            self.log.error("Неверный токен или нет сети — бот %s не запущен", self.slug)
            return False
        self.username = me.get("username", "")
        self.tg.call("setMyCommands", commands=[
            {"command": "start", "description": "Главное меню / Басты мәзір"},
            {"command": "menu", "description": "Меню"}])
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self.loop, name=f"bot-{self.slug}", daemon=True)
        self.thread.start()
        self.log.info("Запущен @%s", self.username)
        return True

    def stop(self):
        self.stop_flag.set()

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive() and not self.stop_flag.is_set())


def normalize_price(text: str) -> str:
    """'6000' → '6 000 ₸'; 'от 5000' → 'от 5 000 ₸'; любой другой текст — как есть."""
    text = text.strip()
    m = re.fullmatch(r"(от\s+|до\s+)?(\d[\d\s]*)\s*(₸|тг|тенге|kzt)?", text, re.I)
    if not m:
        return text
    num = int(re.sub(r"\D", "", m.group(2)))
    return f"{m.group(1) or ''}{num:,}".replace(",", " ") + " ₸"
