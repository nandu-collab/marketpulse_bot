import os, json, time, threading, re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ── CONFIG (Render → Environment) ──────────────────────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME  = os.getenv("CHANNEL_USERNAME", "").strip()   # e.g. @MarketPulse_India
NEWS_API_KEY      = os.getenv("NEWS_API_KEY", "").strip()       # optional (NewsAPI)
NEWS_INTERVAL_MIN = int(os.getenv("NEWS_INTERVAL_MIN", "60"))   # hourly

IST = ZoneInfo("Asia/Kolkata")
bot = Bot(BOT_TOKEN)

POSTED_NEWS_PATH = "/tmp/posted_news.json"
try:
    with open(POSTED_NEWS_PATH, "r") as f:
        POSTED_NEWS = set(json.load(f))
except Exception:
    POSTED_NEWS = set()

def save_posted_news():
    try:
        with open(POSTED_NEWS_PATH, "w") as f:
            json.dump(list(POSTED_NEWS), f)
    except Exception:
        pass

def now_ist():
    return datetime.now(IST)

def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def send_message(text, url_button=None):
    try:
        markup = None
        if url_button:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Read", url=url_button)]]
            )
        bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup
        )
    except Exception as e:
        print("Send error:", e)

# ── NEWS ──────────────────────────────────────────────────────────────────────
def fetch_news_newsapi():
    if not NEWS_API_KEY:
        return []
    params = {
        "apiKey": NEWS_API_KEY,
        "language": "en",
        "pageSize": 8,
        "q": "India stock OR Sensex OR Nifty OR RBI OR tariffs OR crude oil OR rupee OR global markets",
        "domains": ",".join([
            "economictimes.indiatimes.com",
            "moneycontrol.com",
            "business-standard.com",
            "livemint.com",
            "financialexpress.com",
            "reuters.com",
            "bloomberg.com",
        ])
    }
    try:
        r = requests.get("https://newsapi.org/v2/top-headlines", params=params, timeout=20)
        data = r.json()
        arts = data.get("articles", []) or []
        out = []
        for a in arts:
            out.append({
                "title": a.get("title") or "",
                "desc": a.get("description") or "",
                "url": a.get("url") or "",
                "source": (a.get("source") or {}).get("name","")
            })
        return out
    except Exception as e:
        print("NewsAPI error:", e)
        return []

def fetch_news_gnews_rss():
    url = ("https://news.google.com/rss/search"
           "?q=India%20stock%20market%20OR%20Sensex%20OR%20Nifty%20OR%20RBI%20OR%20crude%20oil%20OR%20rupee"
           "&hl=en-IN&gl=IN&ceid=IN:en")
    try:
        r = requests.get(url, timeout=20)
        soup = BeautifulSoup(r.text, "xml")
        items = soup.find_all("item")[:8]
        out = []
        for it in items:
            title = it.title.text if it.title else ""
            link  = it.link.text if it.link else ""
            m = re.search(r"url=(https?[^&]+)", link)
            if m:
                link = requests.utils.unquote(m.group(1))
            out.append({"title": title, "desc": "", "url": link, "source": "Google News"})
        return out
    except Exception as e:
        print("GNews RSS error:", e)
        return []

def post_hourly_news():
    print("Running hourly news...")
    arts = fetch_news_newsapi() or fetch_news_gnews_rss()
    sent = 0
    for a in arts:
        url = a.get("url")
        if not url or url in POSTED_NEWS:
            continue
        title = html_escape(a.get("title"))
        desc  = html_escape(a.get("desc") or "")
        if len(desc) > 550:
            desc = desc[:550] + "…"
        msg = f"[Market Update]\n<b>{title}</b>"
        if desc:
            msg += f"\n{desc}"
        send_message(msg, url_button=url)
        POSTED_NEWS.add(url)
        sent += 1
        if sent >= 5:
            break
    save_posted_news()
    print(f"Posted {sent} news items.")

# ── INDICES (Yahoo Finance) ───────────────────────────────────────────────────
def fetch_indices_snapshot():
    try:
        q = "%5EBSESN,%5ENSEI,%5ENSEBANK"  # Sensex, Nifty50, BankNifty
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={q}"
        data = requests.get(url, timeout=20).json()
        res = {}
        for r in data.get("quoteResponse", {}).get("result", []):
            symbol = r.get("symbol","")
            obj = {
                "price": r.get("regularMarketPrice"),
                "chg": r.get("regularMarketChange"),
                "chgPct": r.get("regularMarketChangePercent")
            }
            if symbol == "^NSEI":
                res["Nifty 50"] = obj
            elif symbol == "^NSEBANK":
                res["Bank Nifty"] = obj
            elif symbol == "^BSESN":
                res["Sensex"] = obj
        return res
    except Exception as e:
        print("Indices error:", e)
        return {}

def fmt_idx(name, d):
    if not d: return f"{name}: —"
    p = d["price"]; ch = d["chg"]; pc = d["chgPct"]
    if p is None: return f"{name}: —"
    sgn = "▲" if (ch or 0) > 0 else ("▼" if (ch or 0) < 0 else "•")
    return f"{name}: {p:.2f}  {sgn} {ch:+.2f} ({pc:+.2f}%)"

def post_market_snapshot(tag="Market Snapshot"):
    data = fetch_indices_snapshot()
    lines = [
        f"[{tag}]",
        fmt_idx("Sensex",     data.get("Sensex")),
        fmt_idx("Nifty 50",   data.get("Nifty 50")),
        fmt_idx("Bank Nifty", data.get("Bank Nifty")),
    ]
    send_message("\n".join(lines))

# ── FII/DII (Moneycontrol) ────────────────────────────────────────────────────
def fetch_fii_dii():
    url = "https://www.moneycontrol.com/stocks/marketstats/fii-dii-activity/"
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        tbl = None
        for t in soup.find_all("table"):
            txt = t.get_text(" ", strip=True)
            if "FII" in txt and "DII" in txt and "Net" in txt:
                tbl = t; break
        if not tbl: return None
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th","td"])] for tr in tbl.find_all("tr")]
        latest = rows[1] if len(rows) > 1 else None
        if not latest: return None
        text = " ".join(latest)
        mdate = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", text)
        date_str = mdate.group(1) if mdate else "Latest"
        m_fii = re.search(r"FII.*?([-+]?\d[\d,\.]*)", text)
        m_dii = re.search(r"DII.*?([-+]?\d[\d,\.]*)", text)
        fii = m_fii.group(1) if m_fii else "NA"
        dii = m_dii.group(1) if m_dii else "NA"
        return {"date": date_str, "fii": fii, "dii": dii}
    except Exception as e:
        print("FII/DII error:", e)
        return None

def post_fii_dii():
    d = fetch_fii_dii()
    if not d:
        send_message("[FII/DII] Data not available.")
        return
    msg = (f"[FII/DII] {html_escape(d['date'])}\n"
           f"FII Net: ₹{html_escape(d['fii'])} Cr\n"
           f"DII Net: ₹{html_escape(d['dii'])} Cr")
    send_message(msg)

# ── IPO (Chittorgarh + IPOWatch GMP) ──────────────────────────────────────────
def fetch_gmp_map():
    url = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
    out = {}
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cols = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
                if len(cols) >= 2:
                    name = cols[0]
                    gmp  = cols[1]
                    if "IPO" in name:
                        name = name.replace(" IPO", "").strip()
                    out[name.lower()] = gmp
    except Exception as e:
        print("GMP map error:", e)
    return out

def best_gmp(name, gmp_map):
    key = name.lower()
    if key in gmp_map: return gmp_map[key]
    for k,v in gmp_map.items():
        if key.startswith(k) or k in key:
            return v
    return "N/A"

def fetch_ipo_list():
    url = "https://www.chittorgarh.com/report/latest-ipos-in-india-bse-nse/ipo/85/"
    items = []
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table: return items
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            name_link = tds[0].find("a")
            name = name_link.get_text(strip=True) if name_link else tds[0].get_text(strip=True)
            details_url = ("https://www.chittorgarh.com" + name_link["href"]) if name_link and name_link.get("href") else None
            open_date   = tds[1].get_text(strip=True)
            close_date  = tds[2].get_text(strip=True)
            lot_size    = tds[3].get_text(strip=True)
            price_band  = tds[4].get_text(strip=True)
            items.append({
                "name": name,
                "details": details_url,
                "open": open_date,
                "close": close_date,
                "lot": lot_size,
                "price": price_band
            })
    except Exception as e:
        print("IPO list error:", e)
    return items

def fetch_subscription_from_detail(detail_url):
    try:
        r = requests.get(detail_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")
        cand = None
        for t in tables:
            txt = t.get_text(" ", strip=True)
            if all(w in txt for w in ["QIB","NII","Retail"]):
                cand = t
        if not cand:
            return None
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th","td"])] for tr in cand.find_all("tr")]
        last = None
        for r2 in reversed(rows):
            joined = " ".join(r2)
            if re.search(r"\d", joined) and any("Total" in x or "Retail" in x for x in r2):
                last = r2; break
        if not last:
            return None
        hdr = [c.lower() for c in rows[0]]
        def find_col(key):
            for i,h in enumerate(hdr):
                if key in h: return i
            return -1
        i_qib    = find_col("qib")
        i_hni    = find_col("nii") if find_col("nii") != -1 else find_col("hni")
        i_retail = find_col("retail")
        i_total  = find_col("total")
        subs = {
            "qib":    last[i_qib] if i_qib!=-1 and i_qib < len(last) else "NA",
            "hni":    last[i_hni] if i_hni!=-1 and i_hni < len(last) else "NA",
            "retail": last[i_retail] if i_retail!=-1 and i_retail < len(last) else "NA",
            "total":  last[i_total] if i_total!=-1 and i_total < len(last) else "NA",
        }
        return subs
    except Exception as e:
        print("Subs parse error:", e)
        return None

def post_daily_ipo_update():
    print("Posting IPO update…")
    gmp_map = fetch_gmp_map()
    ipos = fetch_ipo_list()
    if not ipos:
        send_message("[IPO] No current IPO data available.")
        return

    today = now_ist().strftime("%d-%b-%Y")
    send_message(f"<b>[IPO] Updates for {today}</b>")

    for it in ipos[:6]:
        subs = fetch_subscription_from_detail(it["details"]) if it.get("details") else None
        gmp  = best_gmp(it["name"], gmp_map)
        lines = [
            f"<b>{html_escape(it['name'])} IPO</b>",
            f"🗓️ Open: {html_escape(it['open'])} | Close: {html_escape(it['close'])}",
            f"🎯 Price Band: {html_escape(it['price'])} | Lot: {html_escape(it['lot'])}",
            f"🔥 GMP: {html_escape(gmp)}",
        ]
        if subs:
            lines += [
                "📊 Subscription (latest):",
                f"• QIB: {html_escape(subs['qib'])}x  • HNI: {html_escape(subs['hni'])}x",
                f"• Retail: {html_escape(subs['retail'])}x  • Total: {html_escape(subs['total'])}x",
            ]
        text = "\n".join(lines)
        send_message(text, url_button=it.get("details"))

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
class DailyGate:
    def __init__(self):
        self.last_run = {}
    def should_run(self, name, hhmm):
        now = now_ist()
        if self.last_run.get(name) == now.date():
            return False
        if now.strftime("%H:%M") == hhmm:
            self.last_run[name] = now.date()
            return True
        return False

GATE = DailyGate()
_last_news = 0

def scheduler_loop():
    print("Scheduler started (IST).")
    try:
        post_market_snapshot("Market Snapshot")
        post_hourly_news()
    except Exception as e:
        print("Initial post error:", e)
    global _last_news
    while True:
        now = now_ist()

        if time.time() - _last_news > NEWS_INTERVAL_MIN * 60:
            post_hourly_news()
            _last_news = time.time()

        if GATE.should_run("IPO", "09:05"):
            post_daily_ipo_update()

        if GATE.should_run("PRE_SNAPSHOT", "09:20"):
            post_market_snapshot("Pre-Market Snapshot")

        if GATE.should_run("CLOSE_SNAPSHOT", "15:45"):
            post_market_snapshot("Market Close Snapshot")

        if GATE.should_run("FII_DII", "20:00"):
            post_fii_dii()

        time.sleep(30)

# ── FLASK (keep Render alive) ─────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return Response("OK", status=200)

_thread_started = False
def start_bg():
    global _thread_started
    if not _thread_started and BOT_TOKEN and CHANNEL_USERNAME:
        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()
        _thread_started = True

start_bg()

if __name__ == "__main__":
    start_bg()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
      
