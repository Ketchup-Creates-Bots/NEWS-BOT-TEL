#!/usr/bin/env python3
import os
import time
import sqlite3
import logging
import threading
from datetime import datetime, date
import requests
from bs4 import BeautifulSoup
from telegram import Bot, ParseMode, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, Dispatcher
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import utc
from flask import Flask, request

# Optional OpenAI import - used only if OPENAI_API_KEY is set
try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# === KONFIGURACJA ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
X_USERNAME = os.getenv("X_USERNAME", "")
FOREX_FACTORY_URL = os.getenv("FOREX_FACTORY_URL", "https://www.forexfactory.com/calendar.php")
FOREX_DAILY_HOUR = int(os.getenv("FOREX_DAILY_HOUR", "8"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Configure OpenAI if available
if OPENAI_API_KEY and OPENAI_AVAILABLE:
    openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not TELEGRAM_TOKEN or TARGET_CHAT_ID == 0:
    logger.error("Brakuje TELEGRAM_TOKEN lub TARGET_CHAT_ID - ustaw zmienne środowiskowe.")
    raise SystemExit("Brakuje TELEGRAM_TOKEN lub TARGET_CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# === BAZA DANYCH (deduplikacja) ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS sent_items (
        id TEXT PRIMARY KEY,
        source TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    """)
    conn.commit()
    conn.close()

def mark_sent(item_id: str, source: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO sent_items(id, source) VALUES (?, ?)", (item_id, source))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def was_sent(item_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_items WHERE id=?", (item_id,))
    r = cur.fetchone()
    conn.close()
    return r is not None

# === TWITTER / X ===
last_seen_x_id = None

def fetch_latest_from_x(username, bearer_token, since_id=None):
    if not bearer_token or not username:
        return []
    headers = {"Authorization": f"Bearer {bearer_token}"}
    user = requests.get(f"https://api.twitter.com/2/users/by/username/{username}", headers=headers, timeout=15)
    if user.status_code != 200:
        logger.warning("Błąd pobierania usera z X: %s", user.text[:200])
        return []
    user = user.json()
    user_id = user.get("data", {}).get("id")
    if not user_id:
        return []
    params = {"max_results": 5, "tweet.fields": "created_at,text"}
    if since_id:
        params["since_id"] = since_id
    res = requests.get(f"https://api.twitter.com/2/users/{user_id}/tweets", headers=headers, params=params, timeout=15)
    if res.status_code != 200:
        logger.warning("Błąd pobierania tweetów: %s", res.text[:200])
        return []
    tweets = res.json().get("data", [])
    out = []
    for t in tweets:
        out.append({
            "id": t.get("id"),
            "text": t.get("text"),
            "created_at": t.get("created_at"),
            "url": f"https://x.com/{username}/status/{t.get('id')}"
        })
    return out

def x_poll_job():
    global last_seen_x_id
    try:
        tweets = fetch_latest_from_x(X_USERNAME, X_BEARER_TOKEN, since_id=last_seen_x_id)
        tweets = sorted(tweets, key=lambda t: t.get("created_at", ""))
        for t in tweets:
            tid = t["id"]
            uid = f"x:{tid}"
            if was_sent(uid):
                continue
            text = t["text"]
            url = t["url"]
            message = f"📰 Nowy wpis z X ({X_USERNAME}):\n\n{text}\n\n{url}"
            try:
                bot.send_message(TARGET_CHAT_ID, message)
                mark_sent(uid, "x")
                last_seen_x_id = tid
                logger.info("Wysłano wpis X %s", tid)
            except TelegramError as e:
                logger.exception("Błąd wysyłki do Telegram: %s", e)
    except Exception as e:
        logger.exception("Błąd w x_poll_job: %s", e)

# === FOREX FACTORY SCRAPER ===
def fetch_forex_today():
    try:
        res = requests.get(FOREX_FACTORY_URL, params={"day": "today"}, timeout=15)
        if res.status_code != 200:
            logger.warning("ForexFactory HTTP %s", res.status_code)
            return []
        soup = BeautifulSoup(res.text, "html.parser")
        rows = soup.select("table#calendar tbody tr")
        events = []
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            time_txt = tds[0].get_text(strip=True)
            currency = tds[1].get_text(strip=True)
            impact = tds[2].get_text(strip=True)
            event = tds[3].get_text(strip=True)
            actual = tds[4].get_text(strip=True)
            forecast = tds[5].get_text(strip=True)
            impact_l = impact.lower()
            if any(k in impact_l for k in ("med", "m", "yellow", "high", "h", "important", "red")) or impact_l:
                if ("low" in impact_l) or ("low-impact" in impact_l):
                    continue
                eid = f"ff:{date.today().isoformat()}:{currency}:{event}:{time_txt}"
                events.append({
                    "id": eid,
                    "time": time_txt,
                    "currency": currency,
                    "impact": impact,
                    "event": event,
                    "actual": actual,
                    "forecast": forecast
                })
        return events
    except Exception as e:
        logger.exception("Błąd przy pobieraniu FF: %s", e)
        return []

# === AI ANALYSIS ===
def analyze_event_with_ai(event):
    prompt = f"""
Jesteś asystentem rynkowym. W kilku (2-4) krótkich zdaniach po polsku:
- opisz znaczenie wydarzenia ekonomicznego dla rynków walutowych,
- wskaż możliwy kierunek wpływu na odpowiednią walutę (np. umocnienie/ osłabienie),
- oceń krótkoterminowy poziom zmienności (niski/średni/wysoki).

Dane wydarzenia:
Nazwa: {event.get('event')}
Waluta: {event.get('currency')}
Impact: {event.get('impact')}
Czas: {event.get('time')}
Forecast: {event.get('forecast')}
Actual: {event.get('actual')}
"""
    try:
        if OPENAI_API_KEY and OPENAI_AVAILABLE:
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "Jesteś ekspertem finansowym, mówisz po polsku."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=160,
                temperature=0.2,
            )
            text = resp["choices"][0]["message"]["content"].strip()
            return text
    except Exception as e:
        logger.exception("OpenAI error: %s", e)

    cur = event.get("currency", "")
    ev = event.get("event", "")
    direction = "możliwe większe wahania"
    if "cpi" in ev.lower() or "inflation" in ev.lower():
        direction = f"możliwe umocnienie {cur} jeśli dane będą powyżej oczekiwań, osłabienie jeśli poniżej."
    elif "unemployment" in ev.lower() or "job" in ev.lower() or "nfp" in ev.lower():
        direction = f"duży wpływ na rynek pracy i {cur}, zwiększona zmienność."
    elif "gdp" in ev.lower():
        direction = f"długoterminowy wpływ na kondycję gospodarki i {cur}."
    return f"{event.get('event')} ({cur}) — {direction} Krótkoterminowo spodziewana zmienność: wysoka."

# === FOREX DAILY JOB ===
def forex_daily_job():
    try:
        events = fetch_forex_today()
        if not events:
            bot.send_message(TARGET_CHAT_ID, "📅 ForexFactory: brak wydarzeń medium/high lub błąd pobierania.")
            return
        lines = ["📊 <b>ForexFactory — dzisiejsze wydarzenia (żółte/czerwone):</b>\n"]
        for e in events:
            if was_sent(e["id"]):
                continue
            analysis = analyze_event_with_ai(e)
            lines.append(f"<b>{e['time']} | {e['currency']} | {e['impact']}</b>\n{e['event']}\nPrognoza: {e.get('forecast','-')} | Wynik: {e.get('actual','-')}\n\n{analysis}\n---\n")
            mark_sent(e["id"], "forex")
        message = "\n".join(lines)
        bot.send_message(TARGET_CHAT_ID, message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Błąd w forex_daily_job: %s", e)
        try:
            bot.send_message(TARGET_CHAT_ID, "Błąd podczas pobierania/analizy ForexFactory.")
        except Exception:
            pass

# === /STATUS KOMENDA (TELEGRAM + HTTP) ===
def status_command(update: Update, context):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (
        f"✅ Bot działa!\n"
        f"Czas serwera: {now}\n"
        f"Scheduler: aktywny ✅\n"
        f"X użytkownik: {X_USERNAME or 'brak'}\n"
        f"ForexFactory: {FOREX_FACTORY_URL}"
    )
    update.message.reply_text(msg)

# === MAIN ===
def main():
    init_db()
    scheduler = BackgroundScheduler(timezone=utc)
    scheduler.add_job(x_poll_job, "interval", seconds=POLL_INTERVAL, next_run_time=datetime.utcnow())
    scheduler.add_job(forex_daily_job, "cron", hour=FOREX_DAILY_HOUR, minute=0)
    scheduler.start()
    logger.info("Bot wystartował. Harmonogram uruchomiony.")

    # Telegram webhook dispatcher (simple inline, no polling)
    from telegram.ext import Dispatcher
    dispatcher = Dispatcher(bot, None, workers=0)
    dispatcher.add_handler(CommandHandler("status", status_command))

    # Keep process alive
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping...")

# --- KEEP ALIVE FLASK SERVER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot is running and responding!", 200

@app.route('/status', methods=['GET'])
def http_status():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"✅ Bot działa!\n"
        f"Czas serwera: {now}\n"
        f"Scheduler: aktywny ✅\n"
        f"X użytkownik: {X_USERNAME or 'brak'}\n"
        f"ForexFactory: {FOREX_FACTORY_URL}",
        200
    )

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    main()
