#!/usr/bin/env python3
"""
BotKerek Manager — один процесс, который:
  1) запускает ботов всех клиентов из папки clients/;
  2) запускает ваш личный бот-менеджер, через который вы с телефона
     добавляете клиентов, меняете услуги, цены, данные, админов и смотрите записи.

Настройки в .env:
  ADMIN_BOT_TOKEN=токен вашего бота-менеджера (создаётся в @BotFather)
  OWNER_CHAT_IDS=ваш chat_id (узнать: напишите боту-менеджеру /id)

Запуск:  python3 manager.py
"""
import copy
import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def load_env(path: Path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("manager")

from tg import TG, esc  # noqa: E402
from client_bot import ClientBot, normalize_price, POLL_TIMEOUT  # noqa: E402

CLIENTS_DIR = Path(os.environ.get("CLIENTS_DIR", BASE / "clients"))
TEMPLATE = BASE / "template_config.json"


# ---------------------------------------------------------------------------
# Разбор того, что вы пишете боту-менеджеру
# ---------------------------------------------------------------------------
def parse_services(text: str):
    """Строки вида «Стрижка — 5000» или «Стрижка / Шаш қию — 5000»."""
    out = []
    for i, line in enumerate([l.strip(" •-\t") for l in text.splitlines() if l.strip()], 1):
        m = re.match(r"^(.+?)\s+[—–-]\s+(.+)$", line) or re.match(r"^(.+?)\s*[—–:]\s*(.+)$", line)
        if not m:
            return None, line
        names, price = m.group(1).strip(), m.group(2).strip()
        ru, _, kk = (p.strip() for p in names.partition("/"))
        out.append({"id": f"s{i}", "ru": ru, "kk": kk or ru, "price": normalize_price(price)})
    return (out, None) if out else (None, text)


def parse_hours(text: str):
    """«10:00-21:00» или «10-21» → (начало, конец, слоты по часу)."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*$", text)
    if not m:
        return None
    h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
    if not (0 <= h1 < 24 and 0 < h2 <= 24 and (h1, m1) < (h2, m2)):
        return None
    slots, h, mm = [], h1, m1
    while (h + 1, mm) <= (h2, m2):
        slots.append(f"{h:02d}:{mm:02d}")
        h += 1
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}", slots


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "-", name.lower()).strip("-")
    return s or "client"


FIELDS = {
    "name": ("Название", "business.name"),
    "phone": ("Телефон", "business.phone"),
    "address_ru": ("Адрес (рус)", "business.address_ru"),
    "address_kk": ("Адрес (каз)", "business.address_kk"),
    "hours": ("Часы работы", None),
    "instagram": ("Instagram", "business.instagram"),
    "map_url": ("Ссылка на 2ГИС", "business.map_url"),
}


# ---------------------------------------------------------------------------
class Manager:
    def __init__(self):
        self.tg = TG(os.environ.get("ADMIN_BOT_TOKEN", ""))
        self.owners = {int(x) for x in os.environ.get("OWNER_CHAT_IDS", "").replace(" ", "").split(",") if x}
        self.bots: dict[str, ClientBot] = {}
        self.state: dict[int, dict] = {}
        CLIENTS_DIR.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- clients
    def load_clients(self):
        for folder in sorted(CLIENTS_DIR.iterdir()):
            if (folder / "config.json").exists() and folder.name not in self.bots:
                bot = ClientBot(folder)
                self.bots[folder.name] = bot
                if bot.cfg.get("active", True) and bot.cfg.get("token"):
                    bot.start()

    def create_client(self, token, username, d):
        cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        cfg["token"] = token
        cfg["bot_username"] = username
        cfg["active"] = True
        cfg["admin_chat_ids"] = []
        b = cfg["business"]
        b.update({"name": d["name"], "phone": d["phone"], "address_ru": d["address"], "address_kk": d["address"],
                  "hours_ru": f"Ежедневно {d['hours'][0]}–{d['hours'][1]}",
                  "hours_kk": f"Күн сайын {d['hours'][0]}–{d['hours'][1]}",
                  "instagram": "", "map_url": ""})
        cfg["services"] = d["services"]
        cfg["booking"]["time_slots"] = d["hours"][2]
        cfg["created_at"] = dt.date.today().isoformat()
        slug = slugify(username)
        folder = CLIENTS_DIR / slug
        n = 2
        while folder.exists():
            folder = CLIENTS_DIR / f"{slug}-{n}"
            n += 1
        folder.mkdir(parents=True)
        (folder / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        bot = ClientBot(folder)
        self.bots[folder.name] = bot
        bot.start()
        return bot

    # -------------------------------------------------------------- keyboards
    @staticmethod
    def kb_main():
        return {"keyboard": [[{"text": "➕ Новый клиент"}, {"text": "📂 Клиенты"}],
                             [{"text": "📊 Статистика"}, {"text": "❓ Помощь"}]],
                "resize_keyboard": True}

    @staticmethod
    def kb_cancel():
        return {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True}

    def kb_clients(self):
        rows = []
        for slug, b in self.bots.items():
            mark = "🟢" if b.running else "⏸"
            rows.append([{"text": f"{mark} {b.cfg['business']['name']} (@{b.username or slug})",
                          "callback_data": f"c:{slug}"}])
        return {"inline_keyboard": rows}

    def kb_card(self, slug):
        b = self.bots[slug]
        return {"inline_keyboard": [
            [{"text": "📋 Записи", "callback_data": f"bk:{slug}"},
             {"text": "💰 Услуги и цены", "callback_data": f"sv:{slug}"}],
            [{"text": "✏️ Данные бизнеса", "callback_data": f"fd:{slug}"},
             {"text": "👤 Добавить владельца", "callback_data": f"ad:{slug}"}],
            [{"text": "⏸ Остановить бота" if b.running else "▶️ Запустить бота", "callback_data": f"tg:{slug}"}],
            [{"text": "« Все клиенты", "callback_data": "list"}],
        ]}

    def card_text(self, slug):
        b = self.bots[slug]
        total, week, users = b.stats()
        biz = b.cfg["business"]
        admins = ", ".join(str(a) for a in sorted(b.admins)) or "не добавлен ⚠️"
        return (f"<b>{esc(biz['name'])}</b> — @{esc(b.username or slug)}\n"
                f"Статус: {'🟢 работает' if b.running else '⏸ остановлен'}\n"
                f"Онлайн-запись: {'включена' if b.cfg.get('booking', {}).get('enabled', True) else 'выключена'}\n\n"
                f"Телефон: {esc(biz.get('phone'))}\nАдрес: {esc(biz.get('address_ru'))}\n"
                f"Часы: {esc(biz.get('hours_ru'))}\nУслуг: {len(b.services)}\n"
                f"Владелец (chat_id): {admins}\n\n"
                f"Записей всего: <b>{total}</b>, за 7 дней: <b>{week}</b>\nКлиентов в боте: {users}")

    # -------------------------------------------------------------- texts
    HELP = (
        "<b>Как подключить нового клиента</b>\n\n"
        "1. Создайте бота для клиента в @BotFather (/newbot) и скопируйте токен.\n"
        "2. Здесь нажмите «➕ Новый клиент» и ответьте на 5 вопросов.\n"
        "3. Попросите владельца бизнеса написать его новому боту /id и пришлите это число сюда: "
        "«📂 Клиенты» → клиент → «👤 Добавить владельца». Теперь записи приходят ему.\n"
        "4. Владелец сам меняет цены командой /admin в своём боте.\n\n"
        "<b>Формат услуг</b> (каждая с новой строки):\n"
        "<code>Мужская стрижка — 5000\nСтрижка + борода / Шаш қию + сақал — 7000</code>\n"
        "После «/» можно указать название на казахском."
    )

    def stats_text(self):
        if not self.bots:
            return "Клиентов пока нет. Нажмите «➕ Новый клиент»."
        lines = ["<b>Статистика</b>\n"]
        all_total = all_week = 0
        for slug, b in self.bots.items():
            total, week, _ = b.stats()
            all_total += total
            all_week += week
            lines.append(f"{'🟢' if b.running else '⏸'} {esc(b.cfg['business']['name'])}: {total} записей, "
                         f"{week} за неделю")
        running = sum(1 for b in self.bots.values() if b.running)
        lines += ["", f"Клиентов: <b>{len(self.bots)}</b>, работают: <b>{running}</b>",
                  f"Записей всего: <b>{all_total}</b>, за неделю: <b>{all_week}</b>"]
        return "\n".join(lines)

    # -------------------------------------------------------------- messages
    def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if text.startswith("/id"):
            self.tg.send(chat_id, f"Ваш chat_id: <code>{chat_id}</code>\n"
                                  f"Впишите его в .env как OWNER_CHAT_IDS и перезапустите менеджер.")
            return
        if chat_id not in self.owners:
            self.tg.send(chat_id, "Это служебный бот. Доступ закрыт.")
            return
        if text in ("/start", "/menu") or text == "❌ Отмена":
            self.state.pop(chat_id, None)
            self.tg.send(chat_id, "👋 <b>BotKerek — панель управления</b>\nВыберите действие:" if text != "❌ Отмена"
                         else "Отменено.", self.kb_main())
            return
        st = self.state.get(chat_id)
        if st:
            return self.on_step(chat_id, text, st)
        if text == "➕ Новый клиент":
            self.state[chat_id] = {"flow": "new", "step": "token", "d": {}}
            self.tg.send(chat_id, "Шаг 1/5. Пришлите <b>токен</b> бота клиента из @BotFather.", self.kb_cancel())
        elif text == "📂 Клиенты":
            if not self.bots:
                self.tg.send(chat_id, "Клиентов пока нет. Нажмите «➕ Новый клиент».", self.kb_main())
            else:
                self.tg.send(chat_id, "Ваши клиенты:", self.kb_clients())
        elif text == "📊 Статистика":
            self.tg.send(chat_id, self.stats_text(), self.kb_main())
        elif text == "❓ Помощь":
            self.tg.send(chat_id, self.HELP, self.kb_main())
        else:
            self.tg.send(chat_id, "Выберите действие в меню 👇", self.kb_main())

    def on_step(self, chat_id, text, st):
        d, step = st["d"], st["step"]
        if st["flow"] == "new":
            if step == "token":
                token = text.strip()
                if not re.fullmatch(r"\d+:[\w-]{20,}", token):
                    return self.tg.send(chat_id, "Это не похоже на токен. Он выглядит так: "
                                                 "<code>123456789:AAH…</code>. Пришлите ещё раз.")
                if any(b.cfg.get("token") == token for b in self.bots.values()):
                    return self.tg.send(chat_id, "Этот бот уже подключён.")
                me = TG(token).call("getMe", http_timeout=15)
                if not me:
                    return self.tg.send(chat_id, "Telegram не принял этот токен. Проверьте и пришлите снова.")
                d["token"], d["username"] = token, me.get("username", "")
                st["step"] = "name"
                return self.tg.send(chat_id, f"✅ Бот @{esc(d['username'])} найден.\n\n"
                                             f"Шаг 2/5. <b>Название бизнеса</b> (как увидят клиенты):")
            if step == "name":
                d["name"] = text[:80]
                st["step"] = "phone"
                return self.tg.send(chat_id, "Шаг 3/5. <b>Телефон</b> бизнеса для клиентов:")
            if step == "phone":
                d["phone"] = text[:40]
                st["step"] = "address"
                return self.tg.send(chat_id, "Шаг 4/5. <b>Адрес</b> (например: г. Алматы, пр. Абая, 10):")
            if step == "address":
                d["address"] = text[:150]
                st["step"] = "hours"
                return self.tg.send(chat_id, "Шаг 5/5. <b>Часы работы</b>, например <code>10:00-21:00</code>.\n"
                                             "По ним бот создаст время для записи с шагом 1 час.")
            if step == "hours":
                h = parse_hours(text)
                if not h or not h[2]:
                    return self.tg.send(chat_id, "Не понял время. Напишите так: <code>10:00-21:00</code>")
                d["hours"] = h
                st["step"] = "services"
                return self.tg.send(chat_id, "Последнее: <b>услуги и цены</b>, каждая с новой строки:\n"
                                             "<code>Мужская стрижка — 5000\nБорода / Сақал — 3000</code>")
            if step == "services":
                services, bad = parse_services(text)
                if not services:
                    return self.tg.send(chat_id, f"Не понял строку: «{esc(bad)}».\n"
                                                 f"Формат: <code>Название — цена</code>")
                d["services"] = services
                self.state.pop(chat_id, None)
                bot = self.create_client(d["token"], d["username"], d)
                ok = bot.running
                return self.tg.send(
                    chat_id,
                    (f"🎉 <b>Готово!</b> Бот @{esc(bot.username)} для «{esc(d['name'])}» "
                     f"{'запущен' if ok else 'создан, но не запустился — проверьте токен'}.\n\n"
                     f"Услуг: {len(services)}, время записи: {d['hours'][0]}–{d['hours'][1]}.\n\n"
                     f"Дальше: попросите владельца написать @{esc(bot.username)} команду /id и добавьте его "
                     f"через «📂 Клиенты» → «👤 Добавить владельца»."),
                    self.kb_main())

        slug = st.get("slug")
        b = self.bots.get(slug)
        if not b:
            self.state.pop(chat_id, None)
            return self.tg.send(chat_id, "Клиент не найден.", self.kb_main())
        if st["flow"] == "services":
            services, bad = parse_services(text)
            if not services:
                return self.tg.send(chat_id, f"Не понял строку: «{esc(bad)}». Формат: <code>Название — цена</code>")
            with b.lock:
                b.cfg["services"] = services
                b.save_config()
            self.state.pop(chat_id, None)
            self.tg.send(chat_id, f"✅ Услуги обновлены ({len(services)} шт.).", self.kb_main())
            return self.tg.send(chat_id, self.card_text(slug), self.kb_card(slug))
        if st["flow"] == "admin":
            if not re.fullmatch(r"-?\d{5,15}", text):
                return self.tg.send(chat_id, "Пришлите число — chat_id владельца (он получит его командой /id).")
            with b.lock:
                ids = set(b.cfg.get("admin_chat_ids", []))
                ids.add(int(text))
                b.cfg["admin_chat_ids"] = sorted(ids)
                b.save_config()
            self.state.pop(chat_id, None)
            b.tg.send(int(text), f"✅ Вы подключены как владелец бота «{esc(b.cfg['business']['name'])}».\n"
                                 f"Сюда будут приходить новые записи. Управление ботом: /admin")
            self.tg.send(chat_id, "✅ Владелец добавлен, ему отправлено приветствие.", self.kb_main())
            return self.tg.send(chat_id, self.card_text(slug), self.kb_card(slug))
        if st["flow"] == "field":
            key = st["field"]
            with b.lock:
                biz = b.cfg["business"]
                if key == "hours":
                    h = parse_hours(text)
                    if not h or not h[2]:
                        return self.tg.send(chat_id, "Не понял время. Напишите так: <code>10:00-21:00</code>")
                    biz["hours_ru"] = f"Ежедневно {h[0]}–{h[1]}"
                    biz["hours_kk"] = f"Күн сайын {h[0]}–{h[1]}"
                    b.cfg.setdefault("booking", {})["time_slots"] = h[2]
                else:
                    biz[FIELDS[key][1].split(".", 1)[1]] = text[:200]
                b.save_config()
            self.state.pop(chat_id, None)
            self.tg.send(chat_id, f"✅ «{FIELDS[key][0]}» обновлено.", self.kb_main())
            return self.tg.send(chat_id, self.card_text(slug), self.kb_card(slug))

    # -------------------------------------------------------------- callbacks
    def on_callback(self, cb):
        cid = cb["id"]
        m = cb.get("message") or {}
        chat_id = (m.get("chat") or {}).get("id")
        mid = m.get("message_id")
        data = cb.get("data", "")
        self.tg.answer(cid)
        if chat_id not in self.owners:
            return
        if data == "list":
            return self.tg.send(chat_id, "Ваши клиенты:", self.kb_clients())
        kind, _, rest = data.partition(":")
        slug, _, extra = rest.partition(":")
        b = self.bots.get(slug)
        if not b:
            return self.tg.send(chat_id, "Клиент не найден.")
        if kind == "c":
            self.tg.send(chat_id, self.card_text(slug), self.kb_card(slug))
        elif kind == "bk":
            self.tg.send(chat_id, b.bookings_text())
        elif kind == "sv":
            cur = "\n".join(f"{s['ru']}{' / ' + s['kk'] if s.get('kk') and s['kk'] != s['ru'] else ''} — "
                            f"{s['price']}" for s in b.services)
            self.state[chat_id] = {"flow": "services", "slug": slug, "step": "", "d": {}}
            self.tg.send(chat_id, f"Сейчас:\n<code>{esc(cur)}</code>\n\n"
                                  f"Пришлите <b>новый полный список</b> в том же формате "
                                  f"(можно скопировать и поправить):", self.kb_cancel())
        elif kind == "fd" and not extra:
            self.tg.send(chat_id, "Что изменить?", {"inline_keyboard": [
                [{"text": label, "callback_data": f"fd:{slug}:{key}"}] for key, (label, _) in FIELDS.items()]})
        elif kind == "fd":
            if extra not in FIELDS:
                return
            self.tg.drop_buttons(chat_id, mid)
            self.state[chat_id] = {"flow": "field", "slug": slug, "field": extra, "step": "", "d": {}}
            hint = " в формате <code>10:00-21:00</code>" if extra == "hours" else ""
            self.tg.send(chat_id, f"Пришлите новое значение: <b>{FIELDS[extra][0]}</b>{hint}", self.kb_cancel())
        elif kind == "ad":
            self.state[chat_id] = {"flow": "admin", "slug": slug, "step": "", "d": {}}
            self.tg.send(chat_id, f"Попросите владельца написать @{esc(b.username)} команду /id "
                                  f"и пришлите сюда число, которое он получит:", self.kb_cancel())
        elif kind == "tg":
            self.tg.drop_buttons(chat_id, mid)
            if b.running:
                b.stop()
                b.cfg["active"] = False
            else:
                b.cfg["active"] = True
                b.start()
            with b.lock:
                b.save_config()
            self.tg.send(chat_id, self.card_text(slug), self.kb_card(slug))

    def handle(self, upd):
        try:
            if "message" in upd:
                self.on_message(upd["message"])
            elif "callback_query" in upd:
                self.on_callback(upd["callback_query"])
        except Exception:
            log.exception("Ошибка менеджера в update %s", upd.get("update_id"))

    # -------------------------------------------------------------- run
    def run(self, stop_event: threading.Event | None = None):
        if not self.tg.token:
            sys.exit("Нет ADMIN_BOT_TOKEN в .env — создайте бота-менеджера в @BotFather.")
        me = self.tg.call("getMe", http_timeout=15)
        if not me:
            sys.exit("Токен бота-менеджера не принят Telegram.")
        self.load_clients()
        log.info("Менеджер @%s запущен. Клиентов: %d. Владельцы: %s", me.get("username"), len(self.bots),
                 sorted(self.owners) or "не заданы — напишите боту /id")
        offset = None
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                for upd in self.tg.call("getUpdates", http_timeout=POLL_TIMEOUT + 10, **params) or []:
                    offset = upd["update_id"] + 1
                    self.handle(upd)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                log.warning("Сеть: %s, повтор через 5 с", e)
                stop_event.wait(5)


if __name__ == "__main__":
    Manager().run()
