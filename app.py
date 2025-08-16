# app.py — Market Pulse India (v2)
# Features: IPO cards (details+GMP+mini-summary), India-focused news in 1–2 paragraphs,
# smart global-impact filter, dedupe, stable links, scheduler, index + FII/DII snapshots.
import os, re, json, time, pytz, feedparser, requests
from datetime import datetime, date
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ---- Config & constants ----
IST = pytz.timezone("Asia/Kolkata")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@MarketPulse_India")

# News knobs
NEWS_DAILY_LIMIT     = int(os.getenv("NEWS_DAILY_LIMIT", "24"))
NEWS_INTERVAL_MIN    = int(os.getenv("NEWS_INTERVAL_MIN", "7"))
NEWS_INCLUDE_LINKS   = os.getenv("NEWS_INCLUDE_LINKS", "1") == "1"
NEWS_SUMMARY_CHARS   = int(os.getenv("NEWS_SUMMARY_CHARS", "600"))  # 1–2 short paragraphs
NEWS_STRICT_INDIA    = os.getenv("NEWS_STRICT_INDIA", "1") == "1"

# Telegram API
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Feeds
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.business-standard.com/rss/markets-106.rss"
]

# Keywords for filtering
MUST_INCLUDE = [
    "india","nifty","sensex","nse","bse","sebi","rbi","ipo","gmp","fii","dii",
    "rupee","₹","crore","gst","psu","bank nifty","nbfc","psb",
    "reliance","tcs","infosys","hdfc","icici","sbi","itc","ongc"
]
BLOCK_FOREIGN = [
    "wall street","dow","nasdaq","s&p","euro zone","europe","uk ",
    "u.k.","us ","u.s.","australia","japan","china","hong kong",
    "korea","australian","european","german","france","italy","spain"
]
GLOBAL_WITH_IMPACT = [
    # If these appear, we may keep the story and add “India impact”
    "tariff","import duty","export duty","sanction","opec","crude oil",
    "brent","wti","fed","federal reserve","rate hike","rate cut",
    "inflation","cpi","ppi","gdp","bond yields","dollar index","usd","yuan","renminbi"
]

# Index tickers (Yahoo Finance)
YF_SYMBOLS = {"Sensex": "^BSESN", "Nifty 50": "^NSEI", "Bank Nifty": "^NSEBANK"}

# ---- Flask keep-alive endpoint ----
app = Flask(__name__)
@app.get("/")
def home():
    return jsonify({"ok": True, "service": "MarketPulse India v2",
                    "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")})

# ---- State (dedupe + daily limit) ----
STATE_FILE = "state.json"
DEFAULT_STATE = {"date": None, "posted_ids": [], "posted_fps": [], "news_count_today": 0}
def load_state():
    try:
        data = json.load(open(STATE_FILE, "r", encoding="utf-8"))
    except Exception:
        data = DEFAULT_STATE.copy()
    today = date.today().isoformat()
    if data.get("date") != today:
        data = DEFAULT_STATE.copy(); data["date"] = today
        save_state(data)
    return data
def save_state(data):
    try:
        json.dump(data, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
STATE = load_state()

# ---- Helpers ----
def esc(s:str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").strip()
def norm_fp(text:str) -> str:
    return re.sub(r"[^a-z0-9]+","",(text or "").lower())

def tg_send(text, disable_preview=True):
    if not BOT_TOKEN: 
        print("BOT_TOKEN missing"); return False
    try:
        r = requests.post(f"{TG}/sendMessage", data={
            "chat_id": CHANNEL_USERNAME, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": disable_preview
        }, timeout=25)
        if r.status_code != 200: print("TG err:", r.text[:400])
        return r.status_code == 200
    except Exception as e:
        print("TG ex:", e); return False

# ---- Summarizer (lightweight rule-based) ----
def summarize_text(text: str, max_chars=600):
    """
    Quick summarizer: 
    - take first 3–5 meaningful sentences,
    - prefer sentences that mention India/markets/impact words,
    - cap by max_chars.
    """
    text = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+"," ", text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    if not sents: return ""
    # score sentences
    priority = ["india","nifty","sensex","nse","bse","rbi","sebi","rupee","tariff","fed","crude","oil","usd"]
    def score(s): 
        sl = s.lower()
        return sum(1 for w in priority if w in sl) + (1 if len(s) > 40 else 0)
    ranked = sorted(sents, key=score, reverse=True)
    # take top and sort back by original order
    chosen = sorted(ranked[:5], key=lambda x: sents.index(x))
    out = " ".join(chosen)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ",1)[0] + "…"
    return out

# ---- Global -> India impact hint ----
def india_impact_hint(title:str, body:str):
    t = (title or "").lower() + " " + (body or "").lower()
    hints = []
    if any(k in t for k in ["crude","brent","wti","opec","oil"]):
        hints.append("May sway India’s inflation and OMC margins; watch paint, aviation & chemicals.")
    if "fed" in t or "rate" in t or "yields" in t:
        hints.append("Rates/dollar moves can hit FPIs & IT/Financials; rupee sensitivity high.")
    if "tariff" in t or "duty" in t or "sanction" in t:
        hints.append("Could shift global trade flows; look at domestic import/export exposed sectors.")
    if "dollar index" in t or "usd" in t:
        hints.append("USD strength/weakness can impact rupee, IT revenues and FPI flows.")
    return " ".join(hints)

# ---- Yahoo Finance quotes with fallbacks ----
def fetch_yf_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    try:
        data = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"}).json()
        r = data["quoteResponse"]["result"][0]
        price = r.get("regularMarketPrice") or r.get("postMarketPrice") or r.get("preMarketPrice") or r.get("previousClose")
        prev  = r.get("regularMarketPreviousClose") or r.get("previousClose")
        if price is None: return None, None
        chp = ((price - prev) / prev * 100.0) if prev else r.get("regularMarketChangePercent")
        return float(price), (float(chp) if chp is not None else None)
    except Exception as e:
        print("YF error:", symbol, e); return None, None

def build_indices_text(title="📊 Market Indices"):
    lines = [f"<b>{title}</b>"]
    for name, sym in YF_SYMBOLS.items():
        p, chp = fetch_yf_quote(sym)
        if p is None: lines.append(f"{name}: NA"); continue
        sign = "🔼" if (chp or 0) >= 0 else "🔻"
        lines.append(f"{name}: {p:,.2f} ({sign} {abs(chp):.2f}%)" if chp is not None else f"{name}: {p:,.2f}")
    return "\n".join(lines)

# ---- FII/DII (best-effort scrape) ----
def fetch_fii_dii():
    try:
        html = requests.get("https://www.5paisa.com/share-market-today/fii-dii",
                            timeout=25, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        txt = soup.get_text(" ", strip=True)
        m1 = re.search(r"FII\s+CM\*?\s*([+\-]?\d[\d,]*\.?\d*)", txt, re.I)
        m2 = re.search(r"DII\s+CM\*?\s*([+\-]?\d[\d,]*\.?\d*)", txt, re.I)
        if m1 and m2:
            fii = float(m1.group(1).replace(",", "")); dii = float(m2.group(1).replace(",", ""))
            return fii, dii
    except Exception as e:
        print("FII/DII error:", e)
    return None, None

def build_fii_dii_text():
    fii, dii = fetch_fii_dii()
    if fii is None or dii is None:
        return "<b>💰 FII/DII</b>\nData not available."
    sf = "🔼" if fii >= 0 else "🔻"; sd = "🔼" if dii >= 0 else "🔻"
    return f"<b>💰 FII/DII (Cash)</b>\nFII: {sf} ₹{abs(fii):,.2f} Cr | DII: {sd} ₹{abs(dii):,.2f} Cr"

# ---- IPO scraping (calendar + details + GMP) ----
def get_text(el): return el.get_text(" ", strip=True) if el else ""

def fetch_ipo_calendar(limit=6):
    """Chittorgarh list page: company, dates, price band, lot + details link"""
    url = "https://www.chittorgarh.com/report/ipo-list-by-time-table-and-lot-size/118/all/?year=2025"
    out = []
    try:
        html = requests.get(url, timeout=30, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = table.find_all("tr")[1:] if table else []
        for r in rows[:limit]:
            tds = r.find_all("td")
            if len(tds) < 7: continue
            link = tds[0].find("a")
            out.append({
                "company": get_text(link) or get_text(tds[0]),
                "detail_url": ("https://www.chittorgarh.com" + link.get("href")) if link and link.get("href","/").startswith("/") else (link.get("href") if link else ""),
                "open": get_text(tds[2]), "close": get_text(tds[3]),
                "price_band": get_text(tds[5]), "lot": get_text(tds[6])
            })
    except Exception as e:
        print("IPO calendar error:", e)
    return out

def fetch_ipo_detail_summary(detail_url):
    """First paragraph / company overview from detail page"""
    try:
        if not detail_url: return ""
        html = requests.get(detail_url, timeout=30, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        p = soup.find("p")
        text = get_text(p)
        return summarize_text(text, max_chars=380) if text else ""
    except Exception as e:
        print("IPO detail error:", e); return ""

def fetch_gmp_map():
    """Map {lower(company): (gmp, est)} from Investorgain live gmp table"""
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    m = {}
    try:
        html = requests.get(url, timeout=30, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = table.find_all("tr")[1:] if table else []
        for r in rows:
            cols = [get_text(td) for td in r.find_all("td")]
            if len(cols) < 4: continue
            m[cols[0].lower()] = (cols[1], cols[3])
    except Exception as e:
        print("GMP map error:", e)
    return m

def build_ipo_posts():
    items = fetch_ipo_calendar(limit=6)
    gmp = fetch_gmp_map()
    posts = []
    for it in items:
        key = it["company"].lower()
        gmp_txt, est = gmp.get(key, ("NA", "NA"))
        summary = fetch_ipo_detail_summary(it["detail_url"])
        lines = [
            f"🧾 <b>{esc(it['company'])} IPO</b>",
            f"🗓️ {esc(it['open'])} → {esc(it['close'])}",
            f"💰 Price Band: {esc(it['price_band'])} · Lot: {esc(it['lot'])}",
            f"🔥 GMP: {esc(gmp_txt)} · Est. Listing: {esc(est)}",
        ]
        if summary: lines.append(esc(summary))
        if it["detail_url"]:
            lines.append(f'<a href="{esc(it["detail_url"])}">Details</a> · <i>Chittorgarh</i>')
        posts.append("\n".join(lines))
        time.sleep(0.5)
    return posts

# ---- News logic ----
def is_india_headline(title:str, summary:str) -> bool:
    t = (title or "").lower()
    s = (summary or "").lower()
    # Allow global with India impact words
    if any(k in t+s for k in GLOBAL_WITH_IMPACT):
        return True
    if NEWS_STRICT_INDIA and not any(k in t for k in MUST_INCLUDE):
        return False
    if any(b in t for b in BLOCK_FOREIGN):
        return False
    return True

def short_from_entry(e):
    raw = e.get("summary") or e.get("description") or ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return summarize_text(text, max_chars=NEWS_SUMMARY_CHARS)

def fetch_and_post_news():
    global STATE
    STATE = load_state()
    if STATE["news_count_today"] >= NEWS_DAILY_LIMIT: return

    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed)
            for entry in parsed.entries[:12]:
                uid = entry.get("id") or entry.get("link") or entry.get("title")
                title = entry.get("title", "")
                link  = entry.get("link", "")
                if not uid or not title: continue

                fp = norm_fp(title)
                if uid in STATE["posted_ids"] or fp in STATE["posted_fps"]: 
                    continue

                summary = short_from_entry(entry)
                if not is_india_headline(title, summary):
                    continue

                impact = india_impact_hint(title, summary)
                src = re.sub(r"^https?://(www\.)?", "", link).split("/")[0]
                parts = [f"🚨 <b>{esc(title)}</b>"]
                if summary: parts.append(esc(summary))
                if impact: parts.append(f"➡️ {esc(impact)}")
                if NEWS_INCLUDE_LINKS and link:
                    parts.append(f'<a href="{esc(link)}">Read</a> · <i>{esc(src)}</i>')
                else:
                    parts.append(f"<i>Source:</i> {esc(src)}")

                msg = "\n".join(parts)
                if tg_send(msg, disable_preview=True):
                    STATE["posted_ids"].append(uid)
                    STATE["posted_fps"].append(fp)
                    STATE["posted_ids"] = STATE["posted_ids"][-600:]
                    STATE["posted_fps"] = STATE["posted_fps"][-600:]
                    STATE["news_count_today"] += 1
                    save_state(STATE)
                    time.sleep(1.0)
                    if STATE["news_count_today"] >= NEWS_DAILY_LIMIT: 
                        return
        except Exception as e:
            print("RSS error:", feed, e)

# ---- Schedules (skip weekends for market snapshots) ----
def is_weekday(): return datetime.now(IST).weekday() < 5

def job_premarket():
    if is_weekday(): tg_send(build_indices_text("📊 Pre-Market Snapshot"))
def job_midday():
    if is_weekday(): tg_send(build_indices_text("⏱️ Midday Check"))
def job_close():
    if is_weekday():
        msg = f"🔔 <b>Closing Summary</b>\n{build_indices_text()}\n\n{build_fii_dii_text()}"
        tg_send(msg)
def job_ipo():
    posts = build_ipo_posts()
    if not posts:
        tg_send("🧾 <b>IPO Update</b>\n• Data not available.")
    else:
        for p in posts: tg_send(p)

def job_reset():
    global STATE
    STATE = DEFAULT_STATE.copy(); STATE["date"] = date.today().isoformat(); save_state(STATE)

# ---- Start scheduler ----
scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(fetch_and_post_news, "interval", minutes=NEWS_INTERVAL_MIN, id="news_interval")
scheduler.add_job(job_premarket, CronTrigger(hour=9, minute=5))
scheduler.add_job(job_ipo,       CronTrigger(hour=9, minute=10))
scheduler.add_job(job_midday,    CronTrigger(hour=12, minute=30))
scheduler.add_job(job_close,     CronTrigger(hour=15, minute=40))
scheduler.add_job(job_ipo,       CronTrigger(hour=18, minute=0))
scheduler.add_job(job_reset,     CronTrigger(hour=0,  minute=5))
scheduler.start()

if __name__ == "__main__":
    # Local dev run; on Render we will use gunicorn
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
