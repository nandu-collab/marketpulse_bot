# app.py
# Market Pulse India – Auto-posting Telegram Bot (Render-ready)
# Posts short bullet updates: indices, FII/DII, IPO calendar + GMP, and filtered breaking news.

import os
import re
import json
import time
import pytz
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

IST = pytz.timezone("Asia/Kolkata")

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@MarketPulse_India")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------- Flask (keep-alive) ----------------
app = Flask(__name__)

@app.get("/")
def home():
    return jsonify({"ok": True, "service": "MarketPulse India Bot",
                    "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")})

# ---------------- State (dedupe + daily limit) ----------------
STATE_PATH = "state.json"
DEFAULT_STATE = {"date": None, "posted_news_ids": [], "news_count_today": 0}
NEWS_DAILY_LIMIT = int(os.getenv("NEWS_DAILY_LIMIT", "12"))

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = DEFAULT_STATE.copy()
    today = date.today().isoformat()
    if data.get("date") != today:
        data = DEFAULT_STATE.copy()
        data["date"] = today
        save_state(data)
    return data

def save_state(data):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

STATE = load_state()

# ---------------- Telegram helper ----------------
def tg_send(text, disable_web_page_preview=True):
    if not BOT_TOKEN or "YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        print("[WARN] Set BOT_TOKEN env var before running.")
        return False
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": CHANNEL_USERNAME,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.text[:500])
        return r.status_code == 200
    except Exception as e:
        print("Telegram exception:", e)
        return False

def bullet(s):
    return re.sub(r"\s+", " ", s).strip()

# ---------------- Indices (Yahoo Finance) ----------------
YF_SYMBOLS = {"Sensex": "^BSESN", "Nifty 50": "^NSEI", "Bank Nifty": "^NSEBANK"}

def fetch_yf_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    try:
        data = requests.get(url, timeout=20).json()
        res = data["quoteResponse"]["result"][0]
        price = res.get("regularMarketPrice")
        chp = res.get("regularMarketChangePercent")
        return price, chp
    except Exception as e:
        print("YF error:", symbol, e)
        return None, None

def build_indices_text(title="📊 Market Indices"):
    lines = [f"<b>{title}</b>"]
    for name, sym in YF_SYMBOLS.items():
        p, chp = fetch_yf_quote(sym)
        if p is None or chp is None:
            lines.append(f"{name}: NA")
            continue
        sign = "🔼" if chp >= 0 else "🔻"
        lines.append(f"{name}: {p:.2f} ({sign} {chp:.2f}%)")
    return "\n".join(lines)

# ---------------- FII/DII (best-effort) ----------------
def fetch_fii_dii_from_5paisa():
    try:
        html = requests.get("https://www.5paisa.com/share-market-today/fii-dii", timeout=25).text
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        m_fii = re.search(r"FII\s+CM\*?\s*([+\-]?\d[\d,]*\.?\d*)", text, re.I)
        m_dii = re.search(r"DII\s+CM\*?\s*([+\-]?\d[\d,]*\.?\d*)", text, re.I)
        if m_fii and m_dii:
            fii = float(m_fii.group(1).replace(",", ""))
            dii = float(m_dii.group(1).replace(",", ""))
            return fii, dii
    except Exception as e:
        print("5paisa FII/DII parse error:", e)
    return None, None

def build_fii_dii_text():
    fii, dii = fetch_fii_dii_from_5paisa()
    if fii is None or dii is None:
        return "<b>💰 FII/DII</b>\nData not available today."
    sign_f = "🔼" if fii >= 0 else "🔻"
    sign_d = "🔼" if dii >= 0 else "🔻"
    return f"<b>💰 FII/DII (Cash)</b>\nFII: {sign_f} ₹{abs(fii):,.2f} Cr | DII: {sign_d} ₹{abs(dii):,.2f} Cr"

# ---------------- IPO & GMP ----------------
def fetch_ipo_calendar_top(limit=5):
    """Upcoming/Mainboard IPOs (company, dates, price band, lot) from Chittorgarh."""
    url = "https://www.chittorgarh.com/report/ipo-list-by-time-table-and-lot-size/118/all/?year=2025"
    items = []
    try:
        html = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return items
        rows = table.find_all("tr")[1:]
        for r in rows[:limit]:
            cols = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cols) < 7:
                continue
            company = cols[0]
            open_dt = cols[2]
            close_dt = cols[3]
            price_band = cols[5]
            lot = cols[6]
            items.append({"company": company, "open": open_dt, "close": close_dt,
                          "price_band": price_band, "lot": lot})
    except Exception as e:
        print("IPO calendar parse error:", e)
    return items

def fetch_gmp_list(limit=5):
    """Live IPO GMP (top rows) from Investorgain."""
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    out = []
    try:
        html = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return out
        rows = table.find_all("tr")[1:]
        for r in rows[:limit]:
            cols = [c.get_text(" ", strip=True) for c in r.find_all("td")]
            if len(cols) < 4:
                continue
            name = cols[0]
            gmp = cols[1]
            est_list = cols[3]  # often contains estimated listing or % gain
            out.append({"name": name, "gmp": gmp, "est": est_list})
    except Exception as e:
        print("GMP parse error:", e)
    return out

def build_ipo_text():
    cal = fetch_ipo_calendar_top(limit=5)
    gmp = fetch_gmp_list(limit=5)
    lines = ["<b>🧾 IPO Update</b>"]
    if cal:
        lines.append("<u>Upcoming/Mainboard</u>")
        for it in cal:
            lines.append(f"• {it['company']}: {it['open']}–{it['close']} | {it['price_band']} | Lot: {it['lot']}")
    else:
        lines.append("• IPO calendar data not available.")
    if gmp:
        lines.append("<u>Live GMP</u>")
        for it in gmp:
            lines.append(f"• {it['name']}: GMP {it['gmp']} | Est. {it['est']}")
    else:
        lines.append("• GMP data not available.")
    return "\n".join(lines)

# ---------------- News (RSS + keyword filter) ----------------
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
]
KEYWORDS = [
    "rbi","repo rate","rate hike","rate cut","inflation","gdp","budget","fiscal","monetary",
    "merger","acquisition","buyback","dividend","bonus","split","delisting",
    "all-time high","record high","record low","upper circuit","lower circuit","surge","plunge","soar","crash",
    "sebi","nclt","supreme court","bankruptcy","default",
    "profit jumps","profit surges","loss widens","guidance",
    "ipo","gmp","subscription","oversubscribed","listing gains"
]
def is_major_headline(title: str) -> bool:
    t = title.lower()
    for kw in KEYWORDS:
        if kw in t:
            return True
    if re.search(r"\b\d{1,2}\s?%\b", t):  # e.g., jumps 10%
        return True
    return False

def fetch_and_post_major_news():
    global STATE
    STATE = load_state()
    if STATE["news_count_today"] >= NEWS_DAILY_LIMIT:
        return
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed)
            for entry in parsed.entries[:10]:
                uid = entry.get("id") or entry.get("link") or entry.get("title")
                if not uid or uid in STATE["posted_news_ids"]:
                    continue
                title = entry.get("title", "")
                if not is_major_headline(title):
                    continue
                source = re.sub(r"^https?://(www\.)?", "", entry.get("link", "")).split("/")[0]
                text = f"🚨 <b>{bullet(title)}</b>\n<i>Source:</i> {source}"
                ok = tg_send(text)
                if ok:
                    STATE["posted_news_ids"].append(uid)
                    STATE["news_count_today"] += 1
                    save_state(STATE)
                time.sleep(1.0)
                if STATE["news_count_today"] >= NEWS_DAILY_LIMIT:
                    return
        except Exception as e:
            print("RSS error:", feed, e)

# ---------------- Jobs ----------------
def job_premarket():
    tg_send(build_indices_text("📊 Pre-Market Snapshot"))

def job_midday():
    tg_send(build_indices_text("⏱️ Midday Check"))

def job_close():
    msg = f"🔔 <b>Closing Summary</b>\n{build_indices_text()}\n\n{build_fii_dii_text()}"
    tg_send(msg)

def job_ipo():
    tg_send(build_ipo_text())

def job_reset():
    global STATE
    STATE = DEFAULT_STATE.copy()
    STATE["date"] = date.today().isoformat()
    save_state(STATE)

scheduler = BackgroundScheduler(timezone=IST)
# every 5 min: major headlines only
scheduler.add_job(fetch_and_post_major_news, "interval", minutes=5, id="news_interval")
# fixed-time posts (IST)
scheduler.add_job(job_premarket, CronTrigger(hour=9, minute=5))
scheduler.add_job(job_ipo, CronTrigger(hour=9, minute=10))
scheduler.add_job(job_midday, CronTrigger(hour=12, minute=30))
scheduler.add_job(job_close, CronTrigger(hour=15, minute=40))
scheduler.add_job(job_ipo, CronTrigger(hour=18, minute=0))
scheduler.add_job(job_reset, CronTrigger(hour=0, minute=5))
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
      
