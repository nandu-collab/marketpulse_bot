# app.py — MarketPulse final stable version (no binary deps)
import os
import re
import json
import time
import requests
import feedparser
from datetime import datetime, date
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ---------- CONFIG ----------
IST = pytz.timezone("Asia/Kolkata")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@MarketPulse_India").strip()

# change via Render env if you want
NEWS_INTERVAL_MIN   = int(os.getenv("NEWS_INTERVAL_MIN", "60"))   # minutes
NEWS_DAILY_LIMIT    = int(os.getenv("NEWS_DAILY_LIMIT", "36"))
NEWS_SUMMARY_CHARS  = int(os.getenv("NEWS_SUMMARY_CHARS", "600"))
IPO_MORNING_TIME    = os.getenv("IPO_MORNING_TIME", "09:10")     # "HH:MM" IST
IPO_EVENING_TIME    = os.getenv("IPO_EVENING_TIME", "18:00")     # "HH:MM" IST

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# RSS feeds (India + market)
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets"
]

# quick relevance keywords
MUST_INCLUDE = [
    "india","nifty","sensex","nse","bse","sebi","rbi","ipo","gmp","fii","dii",
    "rupee","₹","crore","gst","bank nifty","inflation","budget","tariff","crude","brent","wti"
]
BLOCK_FOREIGN = ["wall street","dow","nasdaq","u.s.","u.k.","uk ","euro","europe","australia","japan","china","hong kong"]

STATE_FILE = "state.json"
DEFAULT_STATE = {"date": None, "posted_ids": [], "posted_fps": [], "news_count_today": 0}

# ---------- STATE helpers ----------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = DEFAULT_STATE.copy()
    today = date.today().isoformat()
    if s.get("date") != today:
        s = DEFAULT_STATE.copy()
        s["date"] = today
        save_state(s)
    return s

def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

STATE = load_state()

# ---------- Telegram helper ----------
def _domain_of(url):
    try:
        h = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        return h
    except Exception:
        return ""

def send_to_telegram(text, url=None):
    if not BOT_TOKEN or "YOUR_BOT_TOKEN" in BOT_TOKEN or not CHANNEL_USERNAME:
        print("Missing BOT_TOKEN or CHANNEL_USERNAME (set env vars)")
        return False
    payload = {
        "chat_id": CHANNEL_USERNAME,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if url:
        source = _domain_of(url).split(".")[0].title() or "Source"
        reply_markup = {"inline_keyboard": [[{"text": f"Read • {source}", "url": url}]]}
        # Telegram expects reply_markup as JSON string
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=25)
        if r.status_code != 200:
            print("TG error:", r.status_code, r.text[:400])
        return r.status_code == 200
    except Exception as e:
        print("TG exception:", e)
        return False

# ---------- small utilities ----------
def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").strip()

def summarize_html(html, max_chars=600):
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ",1)[0] + "…"

def norm_fp(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

def is_india_relevant(title, body):
    tl = (title or "").lower()
    bl = (body or "").lower()
    # block obvious foreign-only items
    if any(b in tl for b in BLOCK_FOREIGN) and not any(k in tl for k in MUST_INCLUDE):
        return False
    # global-impact keywords are allowed if appear in either
    if any(k in (tl+bl) for k in ["tariff","crude","brent","wti","fed","rate hike","rate cut","dollar","inflation","gdp","bond yield"]):
        return True
    # must include keywords in title (strict)
    if any(k in tl for k in MUST_INCLUDE):
        return True
    # fallback: allow if mentions India in body
    if "india" in (tl+bl) or "indian" in (tl+bl):
        return True
    return False

# ---------- NEWS job ----------
def fetch_and_post_news():
    global STATE
    STATE = load_state()
    if STATE["news_count_today"] >= NEWS_DAILY_LIMIT:
        return
    posted = 0
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed)
            for e in parsed.entries[:12]:
                uid = e.get("id") or e.get("link") or e.get("title")
                if not uid:
                    continue
                fp = norm_fp(e.get("title",""))
                if uid in STATE["posted_ids"] or fp in STATE["posted_fps"]:
                    continue
                title = e.get("title","").strip()
                link  = e.get("link","")
                raw_summary = e.get("summary") or e.get("description") or ""
                summary = summarize_html(raw_summary, max_chars=NEWS_SUMMARY_CHARS)
                if not is_india_relevant(title, summary):
                    continue
                tag = "[Market Update]"
                # if global-impact keyword present make tag different
                if any(k in (title+summary).lower() for k in ["tariff","crude","brent","wti","fed","dollar","inflation","gdp"]):
                    tag = "[Global Impact]"
                text = f"{tag} <b>{esc(title)}</b>"
                if summary:
                    text += f"\n\n{esc(summary)}"
                ok = send_to_telegram(text, url=link)
                if ok:
                    STATE["posted_ids"].append(uid)
                    STATE["posted_fps"].append(fp)
                    STATE["posted_ids"] = STATE["posted_ids"][-800:]
                    STATE["posted_fps"] = STATE["posted_fps"][-800:]
                    STATE["news_count_today"] += 1
                    save_state(STATE)
                    posted += 1
                    time.sleep(1.0)
                    if STATE["news_count_today"] >= NEWS_DAILY_LIMIT:
                        return
            # small pause between feeds
            time.sleep(0.6)
        except Exception as ex:
            print("RSS error:", feed, ex)
    if posted == 0:
        print("No eligible news this cycle.")

# ---------- INDICES snapshot (Yahoo) ----------
YF_SYMBOLS = {"Sensex": "^BSESN", "Nifty 50": "^NSEI", "Bank Nifty": "^NSEBANK"}

def fetch_yf(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
        r = requests.get(url, timeout=12).json()
        res = r.get("quoteResponse", {}).get("result", [])
        if not res:
            return None, None
        res0 = res[0]
        price = res0.get("regularMarketPrice") or res0.get("previousClose")
        ch = res0.get("regularMarketChange")
        chp = res0.get("regularMarketChangePercent")
        return price, chp
    except Exception as e:
        print("YF err:", e)
        return None, None

def build_indices_text(title="Market Snapshot"):
    lines = [f"[{title}]"]
    for name,sym in YF_SYMBOLS.items():
        p,chp = fetch_yf(sym)
        if p is None:
            lines.append(f"{name}: NA")
            continue
        sign = "▲" if (chp or 0) >= 0 else "▼"
        lines.append(f"{name}: {p:,.2f} {sign} {chp:+.2f}%")
    return "\n".join(lines)

# ---------- FII / DII (best-effort) ----------
def fetch_fii_dii():
    try:
        html = requests.get("https://www.5paisa.com/share-market-today/fii-dii", timeout=20, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        m_fii = re.search(r"FII\s*[:\-]?\s*([+\-]?\d[\d,\.]*)", text, re.I)
        m_dii = re.search(r"DII\s*[:\-]?\s*([+\-]?\d[\d,\.]*)", text, re.I)
        if m_fii and m_dii:
            return m_fii.group(1), m_dii.group(1)
    except Exception as e:
        print("FII/DII parse err:", e)
    return None, None

# ---------- IPO (Chittorgarh + Investorgain/IPOWatch) ----------
def fetch_ipo_calendar(limit=6):
    url = "https://www.chittorgarh.com/report/ipo-list-by-time-table-and-lot-size/118/all/?year=2025"
    out = []
    try:
        html = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = table.find_all("tr")[1:] if table else []
        for r in rows[:limit]:
            cols = [c.get_text(" ", strip=True) for c in r.find_all(["td","th"])]
            if len(cols) < 7:
                continue
            company = cols[0]
            open_dt = cols[2]
            close_dt = cols[3]
            price_band = cols[5]
            lot = cols[6]
            link_el = r.find("a")
            detail = None
            if link_el and link_el.get("href"):
                href = link_el.get("href")
                detail = "https://www.chittorgarh.com" + href if href.startswith("/") else href
            out.append({"company":company,"open":open_dt,"close":close_dt,"price_band":price_band,"lot":lot,"detail":detail})
    except Exception as e:
        print("IPO calendar err:", e)
    return out

def fetch_gmp_map():
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    m = {}
    try:
        html = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = table.find_all("tr")[1:] if table else []
        for r in rows:
            cols = [c.get_text(" ", strip=True) for c in r.find_all("td")]
            if len(cols) >= 2:
                m[cols[0].lower()] = cols[1]
    except Exception as e:
        print("GMP err:", e)
    return m

def fetch_subscription(detail_url):
    try:
        if not detail_url:
            return None
        html = requests.get(detail_url, timeout=20, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        cand = None
        for t in tables:
            txt = t.get_text(" ", strip=True)
            if all(k in txt for k in ["QIB","NII","Retail"]):
                cand = t; break
        if not cand:
            return None
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th","td"])] for tr in cand.find_all("tr")]
        # pick last numeric row
        last = None
        for r in reversed(rows):
            combined = " ".join(r)
            if re.search(r"\d", combined) and (any("Total" in x or "Retail" in x for x in r)):
                last = r; break
        if not last:
            return None
        header = [h.lower() for h in rows[0]]
        def find_col(key):
            for i,h in enumerate(header):
                if key in h: return i
            return -1
        qib = last[find_col("qib")] if find_col("qib")!=-1 else "NA"
        nii = last[find_col("nii")] if find_col("nii")!=-1 else (last[find_col("hni")] if find_col("hni")!=-1 else "NA")
        retail = last[find_col("retail")] if find_col("retail")!=-1 else "NA"
        total = last[find_col("total")] if find_col("total")!=-1 else "NA"
        return {"qib":qib,"nii":nii,"retail":retail,"total":total}
    except Exception as e:
        print("Subs err:", e)
        return None

def post_ipo_updates():
    try:
        gmp_map = fetch_gmp_map()
        ipos = fetch_ipo_calendar(limit=6)
        if not ipos:
            send_to_telegram("[IPO] No data available today.")
            return
        today = datetime.now(IST).strftime("%d-%b-%Y")
        send_to_telegram(f"<b>[IPO] Updates for {today}</b>")
        for it in ipos:
            name = it["company"]
            gmp = gmp_map.get(name.lower(), "N/A")
            subs = fetch_subscription(it.get("detail"))
            lines = [
                f"<b>{esc(name)} IPO</b>",
                f"🗓️ {esc(it['open'])} → {esc(it['close'])}",
                f"💰 Price Band: {esc(it['price_band'])} · Lot: {esc(it['lot'])}",
                f"🔥 GMP: {esc(gmp)}"
            ]
            if subs:
                lines.append("📊 Latest Subscription:")
                lines.append(f"• QIB: {esc(subs['qib'])}  • HNI/NII: {esc(subs['nii'])}")
                lines.append(f"• Retail: {esc(subs['retail'])}  • Total: {esc(subs['total'])}")
            text = "\n".join(lines)
            send_to_telegram(text, url=it.get("detail"))
            time.sleep(1.0)
    except Exception as e:
        print("Post IPO err:", e)

# ---------- scheduled jobs ----------
def job_premarket():
    send_to_telegram(build_indices_text := build_indices_text("Pre-Market Snapshot"), url=None)

def job_midday():
    send_to_telegram(build_indices_text("Midday Check"))

def job_close():
    send_to_telegram(build_indices_text("Market Close"), url=None)
    # also post FII/DII after close
    f = fetch_fii_dii()
    if f and f[0] is not None:
        send_to_telegram(f"<b>[FII/DII]</b>\nFII Net: ₹{esc(f[0])} Cr\nDII Net: ₹{esc(f[1])} Cr")

# ---------- scheduler setup ----------
app = Flask(__name__)

@app.get("/")
def home():
    return jsonify({"ok": True, "service": "MarketPulse", "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")})

scheduler = BackgroundScheduler(timezone=IST)
# hourly news
scheduler.add_job(fetch_and_post_news, "interval", minutes=NEWS_INTERVAL_MIN, id="news_interval")
# pre-market snapshot (09:05)
scheduler.add_job(lambda: send_to_telegram(build_indices_text("📊 Pre-Market Snapshot")), CronTrigger(hour=9, minute=5))
# IPO morning
h,m = map(int, IPO_MORNING_TIME.split(":"))
scheduler.add_job(post_ipo_updates, CronTrigger(hour=h, minute=m))
# midday (12:30)
scheduler.add_job(lambda: send_to_telegram(build_indices_text("⏱️ Midday Check")), CronTrigger(hour=12, minute=30))
# market close snapshot (15:40)
scheduler.add_job(lambda: send_to_telegram(build_indices_text("🔔 Market Close Summary")), CronTrigger(hour=15, minute=40))
# IPO evening
he,me = map(int, IPO_EVENING_TIME.split(":"))
scheduler.add_job(post_ipo_updates, CronTrigger(hour=he, minute=me))
# reset daily counters at 00:05 IST
def reset_state():
    global STATE
    STATE = DEFAULT_STATE.copy()
    STATE["date"] = date.today().isoformat()
    save_state(STATE)
scheduler.add_job(reset_state, CronTrigger(hour=0, minute=5))

scheduler.start()

# make scheduler run right away once (optional)
time.sleep(1)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
