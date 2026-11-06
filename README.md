# BotKerek — Telegram booking bots for small businesses

**BotKerek** (from Kazakh *керек* — "needed") is a multi-tenant Telegram bot platform for salons, barbershops and clinics in Kazakhstan.
One Python process runs the booking bots of **all clients** plus a private **manager bot** that lets me onboard and manage clients from my phone.

> 🇷🇺 Платформа Telegram-ботов онлайн-записи для малого бизнеса. Один процесс держит ботов всех клиентов и личного бота-менеджера, через которого клиенты подключаются за 2 минуты прямо с телефона. Интерфейс ботов — на русском и казахском.

## Features

**Client bot (for the business's customers)**
- Online booking: service → date → time slot → name → phone → confirmation
- Busy slots are hidden, past slots on the current day are not offered
- Services & prices, address, working hours, FAQ
- Two languages: Russian / Kazakh

**Owner panel (inside the business's own bot)**
- Every new booking is sent to the owner instantly
- `/admin`: change prices, see latest bookings, pause online booking

**Manager bot (only for me)**
- 6-step wizard to add a new client: token → name → phone → address → hours → services
- Token is validated via `getMe`; the client bot starts immediately, without restart
- Client cards: bookings, edit services/address, add owner, stop/start the bot
- Statistics across all clients
- Access restricted to whitelisted chat IDs

## Tech

- **Python 3.9+, standard library only** — `urllib` for the Telegram Bot API (long polling), `sqlite3` for storage, `threading` (one thread per client bot)
- Per-client config (`clients/<slug>/config.json`) and database (`clients/<slug>/data.db`)
- Runs 24/7 on a cheap Linux VPS as a `systemd` service
- End-to-end test with a **mock Telegram API server** (`http.server`) — 31 checks covering the manager wizard, booking flow, owner panel, permissions and restart recovery

```
manager.py          # manager bot + loads/starts all client bots
client_bot.py       # booking bot logic for one business
tg.py               # minimal Telegram Bot API client
template_config.json# default texts (RU/KZ), FAQ, slots
tests/              # e2e test with a fake Telegram server
```

## Run

```bash
cp .env.example .env        # put ADMIN_BOT_TOKEN from @BotFather
python3 manager.py
# send /id to your manager bot, put the number into OWNER_CHAT_IDS, restart
```

Tests (no network, no tokens needed):

```bash
python3 tests/test_manager.py   # → ALL PASSED
```

### 24/7 on a server (systemd)

```ini
[Unit]
Description=BotKerek Manager
After=network-online.target

[Service]
WorkingDirectory=/opt/botkerek
ExecStart=/usr/bin/python3 /opt/botkerek/manager.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Security notes

- Tokens live only in `.env` and `clients/*/config.json` — both are in `.gitignore`
- Manager commands are allowed only for chat IDs from `OWNER_CHAT_IDS`; owner panel only for the business owner's chat ID
- If a token leaks: `/revoke` in @BotFather and update the client's config

## Roadmap

- [ ] Reminders to customers 24h / 2h before the visit
- [ ] Cancel / reschedule by the customer
- [ ] Several masters per business
- [ ] Weekly report for the owner

---

Built by **Aryn Ospan** with AI pair-programming. · [LinkedIn](https://www.linkedin.com/in/aryn-ospan-207b30442)
