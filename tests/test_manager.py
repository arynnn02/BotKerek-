"""Сквозной тест BotKerek Manager: менеджер + клиентские боты в потоках на поддельном Telegram."""
import json, os, sys, threading, tempfile, time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SENT = defaultdict(list)          # token -> [(method, body)]
QUEUES = defaultdict(list)        # token -> [update]
VALID = {"111:" + "A" * 30: "botkerek_admin_bot", "222:" + "B" * 30: "salon_aru_bot"}
LOCK = threading.Lock()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        _, tok, method = self.path.split("/", 2)
        tok = tok[3:]
        if tok not in VALID:
            out = {"ok": False, "description": "Unauthorized"}
        elif method == "getMe":
            out = {"ok": True, "result": {"username": VALID[tok]}}
        elif method == "getUpdates":
            for _ in range(20):
                with LOCK:
                    if QUEUES[tok]:
                        res = QUEUES[tok][:]; QUEUES[tok].clear(); break
                time.sleep(0.02)
            else:
                res = []
            out = {"ok": True, "result": res}
        else:
            with LOCK:
                SENT[tok].append((method, body))
            out = {"ok": True, "result": {"message_id": 1} if method == "sendMessage" else True}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tmp = tempfile.mkdtemp()
ADMIN, CLIENT = list(VALID)
OWNER, SALON_OWNER, CUSTOMER = 501, 7770001, 900
os.environ.update(TELEGRAM_API_BASE=f"http://127.0.0.1:{srv.server_port}", POLL_TIMEOUT="1",
                  ADMIN_BOT_TOKEN=ADMIN, OWNER_CHAT_IDS=str(OWNER), CLIENTS_DIR=os.path.join(tmp, "clients"))
sys.path.insert(0, APP)
import manager as M

mgr = M.Manager()
stop = threading.Event()
threading.Thread(target=mgr.run, args=(stop,), daemon=True).start()
time.sleep(0.5)

uid = [0]
def push(tok, chat, text=None, data=None, contact=None):
    uid[0] += 1
    if data:
        upd = {"update_id": uid[0], "callback_query": {"id": str(uid[0]), "from": {"id": chat, "username": "u"},
               "data": data, "message": {"message_id": 1, "chat": {"id": chat}}}}
    else:
        m = {"message_id": uid[0], "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "username": "u"}, "text": text}
        if contact: m["contact"] = contact
        upd = {"update_id": uid[0], "message": m}
    with LOCK:
        QUEUES[tok].append(upd)
    time.sleep(0.35)

def texts(tok, chat):
    with LOCK:
        return [b["text"] for m, b in SENT[tok] if m == "sendMessage" and b["chat_id"] == chat]
def last(tok, chat): return texts(tok, chat)[-1]
def last_kb(tok, chat):
    with LOCK:
        return [b.get("reply_markup") for m, b in SENT[tok] if m == "sendMessage" and b["chat_id"] == chat][-1]

fail = []
def check(c, label):
    print(("OK  " if c else "FAIL") + " " + label)
    if not c: fail.append(label)

# --- доступ
push(ADMIN, 12345, "/start"); check("Доступ закрыт" in last(ADMIN, 12345), "stranger blocked")
push(ADMIN, 12345, "/id"); check("12345" in last(ADMIN, 12345), "/id works for stranger")
push(ADMIN, OWNER, "/start"); check("панель управления" in last(ADMIN, OWNER), "owner menu")
push(ADMIN, OWNER, "📂 Клиенты"); check("пока нет" in last(ADMIN, OWNER), "no clients yet")
# --- мастер нового клиента
push(ADMIN, OWNER, "➕ Новый клиент"); check("токен" in last(ADMIN, OWNER), "wizard asks token")
push(ADMIN, OWNER, "abc"); check("не похоже" in last(ADMIN, OWNER), "bad token format rejected")
push(ADMIN, OWNER, "999:" + "Z" * 30); check("не принял" in last(ADMIN, OWNER), "invalid token rejected")
push(ADMIN, OWNER, CLIENT); check("@salon_aru_bot найден" in last(ADMIN, OWNER), "valid token accepted")
push(ADMIN, OWNER, "Салон Ару"); push(ADMIN, OWNER, "+7 701 111 22 33"); push(ADMIN, OWNER, "г. Астана, ул. Кенесары, 5")
push(ADMIN, OWNER, "9-18"); check("услуги и цены" in last(ADMIN, OWNER), "hours parsed")
push(ADMIN, OWNER, "Маникюр / Маникюр — 6000\nПедикюр — от 8000\nкривая строка")
check("Не понял строку" in last(ADMIN, OWNER), "bad service line rejected")
push(ADMIN, OWNER, "Маникюр / Маникюр — 6000\nПедикюр — от 8000")
time.sleep(0.5)
check("Готово" in last(ADMIN, OWNER) and "запущен" in last(ADMIN, OWNER), "client created and started")
slug = next(iter(mgr.bots)); bot = mgr.bots[slug]
check(bot.running and bot.cfg["booking"]["time_slots"][0] == "09:00" and bot.cfg["booking"]["time_slots"][-1] == "17:00", "slots 09:00..17:00")
check(bot.services[1]["price"] == "от 8 000 ₸", "price normalized")
# --- клиент записывается
push(CLIENT, CUSTOMER, "/start"); push(CLIENT, CUSTOMER, data="lang:ru")
check("Салон Ару" in last(CLIENT, CUSTOMER), "client bot greets with new name")
push(CLIENT, CUSTOMER, "📝 Записаться"); push(CLIENT, CUSTOMER, data="svc:s1")
day = last_kb(CLIENT, CUSTOMER)["inline_keyboard"][1][0]["callback_data"]
push(CLIENT, CUSTOMER, data=day); slot = last_kb(CLIENT, CUSTOMER)["inline_keyboard"][0][0]["callback_data"]
push(CLIENT, CUSTOMER, data=slot); push(CLIENT, CUSTOMER, "Айгерим")
push(CLIENT, CUSTOMER, contact={"phone_number": "+77012223344"}); push(CLIENT, CUSTOMER, data="confirm:yes")
check("Вы записаны" in last(CLIENT, CUSTOMER), "customer booked")
# --- менеджер добавляет владельца салона
push(ADMIN, OWNER, data=f"ad:{slug}"); push(ADMIN, OWNER, str(SALON_OWNER))
check("Владелец добавлен" in " ".join(texts(ADMIN, OWNER)[-2:]), "owner of salon added")
check("подключены как владелец" in last(CLIENT, SALON_OWNER), "salon owner greeted by client bot")
# --- карточка и записи
push(ADMIN, OWNER, data=f"c:{slug}"); check("Записей всего: <b>1</b>" in last(ADMIN, OWNER), "card shows 1 booking")
push(ADMIN, OWNER, data=f"bk:{slug}"); check("Айгерим" in last(ADMIN, OWNER), "manager sees bookings")
# --- менеджер меняет услуги и адрес
push(ADMIN, OWNER, data=f"sv:{slug}"); push(ADMIN, OWNER, "Маникюр — 7000\nНаращивание — 12000")
check(len(bot.services) == 2 and bot.services[1]["ru"] == "Наращивание", "services replaced")
push(ADMIN, OWNER, data=f"fd:{slug}:address_ru"); push(ADMIN, OWNER, "г. Астана, пр. Туран, 1")
check(bot.cfg["business"]["address_ru"] == "г. Астана, пр. Туран, 1", "address updated")
saved = json.load(open(os.path.join(tmp, "clients", slug, "config.json"), encoding="utf-8"))
check(saved["business"]["address_ru"] == "г. Астана, пр. Туран, 1", "config saved to disk")
# --- владелец салона сам меняет цену
push(CLIENT, SALON_OWNER, "/admin"); check("Управление ботом" in last(CLIENT, SALON_OWNER), "salon owner /admin")
push(CLIENT, SALON_OWNER, data="own:price"); push(CLIENT, SALON_OWNER, data="ownp:s1"); push(CLIENT, SALON_OWNER, "7500")
check(bot.services[0]["price"] == "7 500 ₸", "salon owner changed price")
push(CLIENT, CUSTOMER, "💰 Услуги и цены"); check("7 500 ₸" in last(CLIENT, CUSTOMER), "customer sees new price")
push(CLIENT, CUSTOMER, "/admin"); check("только для владельца" in last(CLIENT, CUSTOMER), "customer cannot /admin")
# --- выключить запись и бота
push(CLIENT, SALON_OWNER, data="own:toggle"); push(CLIENT, CUSTOMER, "📝 Записаться")
check("недоступна" in last(CLIENT, CUSTOMER), "booking disabled by owner")
push(ADMIN, OWNER, data=f"tg:{slug}"); time.sleep(1.5)
check(not bot.running and "остановлен" in last(ADMIN, OWNER), "manager stopped client bot")
push(ADMIN, OWNER, data=f"tg:{slug}"); time.sleep(0.5)
check(bot.running, "manager restarted client bot")
push(ADMIN, OWNER, "📊 Статистика"); check("Клиентов: <b>1</b>" in last(ADMIN, OWNER), "stats")
# --- перезапуск процесса подхватывает клиентов с диска
stop.set(); bot.stop(); time.sleep(1.5)
mgr2 = M.Manager(); mgr2.load_clients(); time.sleep(0.3)
check(slug in mgr2.bots and mgr2.bots[slug].running and mgr2.bots[slug].services[0]["price"] == "7 500 ₸", "restart loads clients from disk")
print("\nFAILED: " + ", ".join(fail) if fail else "\nALL PASSED")
os._exit(0)
