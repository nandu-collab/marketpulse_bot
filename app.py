# app.py — Market Pulse India (news + IPO, hourly auto-posts, tagged, single link button)
import os, re, json, time, pytz, feedparser, requests
from datetime import datetime, date
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ===== Config =====
IST = pytz.timezone("Asia/Kolkata")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@MarketPulse_India")

# News controls
NEWS_INTERVAL_MIN = int(os.getenv("NEWS_INTERVAL_MIN", "60"))  # every hour
NEWS_DAILY_LIMIT   = int(os.getenv("NEWS_DAILY_LIMIT", "36"))  # cap per day
NEWS_SUMMARY_CHARS = int(os.getenv("NEWS_SUMMARY_CHARS", "550"))
INCLUDE_LINKS      = os.getenv("INCLUDE_LINKS", "1") == "1"

# Feeds (add/remove if you like)
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets"
]

# India filters
MUST_INCLUDE = [
    "india","nifty","sensex","nse","bse","sebi","rbi","ipo","gmp","fii","dii",
    "rupee","₹","crore","gst","psu","bank nifty","nbfc","psb",
    "reliance","tcs","infosys","hdfc","icici","sbi","itc","ongc"
]
BLOCK_FOREIGN = [
    "wall street","dow","nasdaq","s&p","ftse","cac","dax","euro zone","europe",
    "u.k.","uk ","us ","u.s.","australia","japan","china","hong kong","korea"
]
GLOBAL_WITH_IMPACT = [
    "tariff","duty","sanction","opec","crude","brent","wti","fed","federal reserve",
    "rate hike","rate cut","inflation","cpi","ppi","gdp","bond yield","dollar index",
    "usd","yuan","renminbi"
]

# ===== Telegram =====
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
def tg_send_text_with_button(text, url, source):
    if not BOT_TOKEN: 
        print("[WARN] BOT_TOKEN missing"); 
        return False
    # One button only: "Read • Source" -> article URL
    reply_markup = {"inline_keyboard": [[{"text": f"Read • {source}", "url": url}]]]}
    try:
        r = requests.post(
            f"{TG}/sendMessage",
            json={
                "chat_id": CHANNEL_USERNAME,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup
            },
            timeout=25
        )
        if r.status_code != 200:
            print("TG error:", r.text[:400])
        return r.status_code == 200
    except Exception as e:
        print("TG ex:", e)
        return False

# ===== Flask keep-alive =====
app = Flask(__name__)
@app.get("/")
def home():
    return jsonify({"ok": True, "service": "MarketPulse India", "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")})

# ===== State (dedupe + daily limit) =====
STATE_FILE = "state.json"
DEFAULT_STATE = {"date": None, "posted_ids": [], "posted_fps": [], "count": 0}
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

# ===== Helpers =====
def esc(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").strip()
def norm_fp(text: str) -> str:
    return re.sub(r"[^a-z0-9]+","",(text or "").lower())

def summarize(text: str, max_chars=550):
    txt = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    txt = re.sub(r"\s+"," ", txt)
    if len(txt) <= max_chars: return txt
    return txt[:max_chars].rsplit(" ",1)[0] + "…"

def india_relevance(title: str, body: str):
    t = (title or "").lower(); b = (body or "").lower()
    if any(k in t+b for k in GLOBAL_WITH_IMPACT):
        return "Global Impact"
    if any(bw in t for bw in BLOCK_FOREIGN) and not any(k in t for k in MUST_INCLUDE):
        return None
    if any(k in t for k in MUST_INCLUDE):
        return "Market Update"
    # allow some that mention India in body
    if "india" in t+b or "indian" in t+b:
        return "Market Update"
    return None

# ===== IPO scraping (calendar + details + GMP) =====
def get_text(el): return el.get_text(" ", strip=True) if el else ""
def fetch_ipo_calendar(limit=6):
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
    try:
        if not detail_url: return ""
        html = requests.get(detail_url, timeout=30, headers={"User-Agent":"Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        p = soup.find("p")
        text = get_text(p)
        return summarize(text, max_chars=380) if text else ""
    except Exception as e:
        print("IPO detail error:", e); return ""

def fetch_gmp_map():
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
            m[cols[0].lower()] = (cols[1], cols[3])  # name -> (gmp, est)
    except Exception as e:
        print("GMP error:", e)
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
            f"[IPO] <b>{esc(it['company'])}</b>",
            f"🗓️ {esc(it['open'])} → {esc(it['close'])}",
            f"💰 Price Band: {esc(it['price_band'])} · Lot: {esc(it['lot'])}",
            f"🔥 GMP: {esc(gmp_txt)} · Est. Listing: {esc(est)}",
        ]
        if summary: lines.append(esc(summary))
        text = "\n".join(lines)
        posts.append({"text": text, "url": it["detail_url"], "source": "Chittorgarh"})
        time.sleep(0.5)
    return posts

# ===== News fetch =====
def fetch_and_post_news():
    global STATE
    STATE = load_state()
    if STATE["count"] >= NEWS_DAILY_LIMIT: 
        return

    any_posted = False
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed)
            for e in parsed.entries[:10]:
                uid = e.get("id") or e.get("link") or e.get("title")
                title = e.get("title", "")
                link  = e.get("link", "")
                if not uid or not title or not link: 
                    continue

                fp = norm_fp(title)
                if uid in STATE["posted_ids"] or fp in STATE["posted_fps"]:
                    continue

                raw_summary = e.get("summary") or e.get("description") or ""
                short = summarize(raw_summary, max_chars=NEWS_SUMMARY_CHARS)

                tag = india_relevance(title, short)
                if not tag:
                    continue

                # Build message (no 🚨, no extra site link)
                source = re.sub(r"^https?://(www\.)?", "", link).split("/")[0].split(".")[0].title()
                text = f"[{tag}] <b>{esc(title)}</b>"
                if short:
                    text += f"\n\n{esc(short)}"

                if INCLUDE_LINKS:
                    tg_send_text_with_button(text, link, source)
                else:
                    tg_send_text_with_button(text, link, "Read")

                # update state
                STATE["posted_ids"].append(uid)
                STATE["posted_fps"].append(fp)
                STATE["posted_ids"] = STATE["posted_ids"][-600:]
                STATE["posted_fps"] = STATE["posted_fps"][-600:]
                STATE["count"] += 1
                save_state(STATE)

                any_posted = True
                time.sleep(1.2)

                if STATE["count"] >= NEWS_DAILY_LIMIT:
                    return
        except Exception as ex:
            print("RSS error:", feed, ex)

    if not any_posted:
        print("No eligible news this cycle.")

def post_ipo_updates():
    posts = build_ipo_posts()
    if not posts:
        tg_send_text_with_button("[IPO] No data available.", "https://www.chittorgarh.com/ipo/", "Chittorgarh")
        return
    for p in posts:
        tg_send_text_with_button(p["text"], p["url"], p["source"])
        time.sleep(1.0)

# ===== Schedules =====
def is_weekday(): return datetime.now(IST).weekday() < 5

scheduler = BackgroundScheduler(timezone=IST)
# hourly news
scheduler.add_job(fetch_and_post_news, "interval", minutes=NEWS_INTERVAL_MIN, id="news_hourly")
# IPO at 9:10 AM & 6:00 PM daily
scheduler.add_job(post_ipo_updates, CronTrigger(hour=9,  minute=10))
scheduler.add_job(post_ipo_updates, CronTrigger(hour=18, minute=0))
# reset daily counters
scheduler.add_job(lambda: save_state({"date": date.today().isoformat(), "posted_ids": [], "posted_fps": [], "count": 0}),
                  CronTrigger(hour=0, minute=5))
scheduler.start()

if __name__ == "__main__":
    # On Render we use gunicorn; this is for local run.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
  
