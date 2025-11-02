#!/usr/bin/env python3
import os
import time
import sqlite3
import logging
import threading
from datetime import datetime, date
import requests
from bs4 import BeautifulSoup
from telegram import Bot, ParseMode
from telegram.error import TelegramError
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

# --- Set webhook for Telegram ---
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
if RENDER_URL:
    webhook_url = f"{RENDER_URL}/{TELEGRAM_TOKEN}"
    try:
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook ustawiony na: {webhook_url}")
    except Exception as e:
        logger.exception("Nie udało się ustawić webhooka: %s", e)
else:
    logger.warning("⚠️ Brak RENDER_EXTERNAL_URL — webhook nie został ustawiony")

# === BAZA DANYCH ===
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
    user_id = user.json().get("data", {}).get("id")
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
            message = f"📰 Nowy wpis z X ({X_USERNAME}):\n\n{t['text']}\n\n{t['url']}"
            try:
                bot.send_message(TARGET_CHAT_ID, message)
                mark_sent(uid, "x")
                last_seen_x_id = tid
                logger.info("Wysłano wpis X %s", tid)
            except TelegramError as e:
                logger.exception("Błąd wysyłki do Telegrama: %s", e)
    except Exception as e:
        logger.exception("Błąd w x_poll_job: %s", e)

# === FOREX FACTORY SCRAPER ===
def fetch_forex_today():
    try:
        res = requests.get(FOREX_FACTORY_URL, params={"day": "today"}, timeout=15)
        if res.status_code != 200:
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
            if any(k in impact_l for k in ("med", "high", "important", "red")):
                if "low" in impact_l:
                    continue
                eid = f"ff:{date.today().isoformat()}:{currency}:{event}:{time_txt}"
                events.append({
                    "id": eid, "time": time_txt, "currency": currency, "impact": impact,
                    "event": event, "actual": actual, "forecast": forecast
                })
        return events
    except Exception as e:
        logger.exception("Błąd przy pobieraniu FF: %s", e)
        return []

# === AI ANALYSIS ===
def analyze_event_with_ai(event):
    prompt = f"""
Jesteś asystentem rynkowym. W kilku zdaniach po polsku opisz znaczenie wydarzenia:
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
            return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
    return f"{event.get('event')} ({event.get('currency')}) — możliwe wahania, szczególnie przy odchyleniach od prognoz."

# === FOREX DAILY JOB ===
def forex_daily_job():
    try:
        events = fetch_forex_today()
        if not events:
            bot.send_message(TARGET_CHAT_ID, "📅 ForexFactory: brak wydarzeń medium/high.")
            return
        lines = ["📊 <b>ForexFactory — dzisiejsze wydarzenia:</b>\n"]
        for e in events:
            if was_sent(e["id"]):
                continue
            analysis = analyze_event_with_ai(e)
            lines.append(f"<b>{e['time']} | {e['currency']} | {e['impact']}</b>\n{e['event']}\n{analysis}\n---\n")
            mark_sent(e["id"], "forex")
        bot.send_message(TARGET_CHAT_ID, "\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Błąd w forex_daily_job: %s", e)

# === FLASK (keep alive + webhook) ===
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot is running and responding!", 200

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json(force=True)
    message = update.get("message", {})
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")

    if not text or not chat_id:
        return "No data", 200

    text_lower = text.strip().lower()

    if text_lower == "/status":
        bot.send_message(chat_id, "✅ Bot działa poprawnie. Harmonogram aktywny.")
    elif text_lower == "/help":
        bot.send_message(chat_id, "📋 Dostępne komendy:\n/status — sprawdź, czy bot działa\n/help — lista komend")
    else:
        bot.send_message(chat_id, "Nieznana komenda. Użyj /help.")
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# === MAIN ===
def main():
    init_db()
    scheduler = BackgroundScheduler(timezone=utc)
    scheduler.add_job(x_poll_job, "interval", seconds=POLL_INTERVAL, next_run_time=datetime.utcnow())
    scheduler.add_job(forex_daily_job, "cron", hour=FOREX_DAILY_HOUR, minute=0)
    scheduler.start()
    logger.info("Bot wystartował. Harmonogram uruchomiony.")
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping...")

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    main()
