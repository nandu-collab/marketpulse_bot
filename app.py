import os, json, time, threading, re
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ─────────────────────────────
# CONFIG (from Render ENV)
# ─────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME  = os.getenv("CHANNEL_USERNAME", "").strip()  # e.g. @MarketPulse_India
NEWS_API_KEY      = os.getenv("NEWS_API_KEY", "").strip()      # optional (NewsAPI); bot will fall back to Google News RSS
NEWS_INTERVAL_MIN = int(os.getenv("NEWS_INTERVAL_MIN", "60"))  # hourly news by default

IST = ZoneInfo("Asia/Kolkata")
bot = Bot(BOT_TOKEN)

# Keep a tiny file of posted news links to avoid duplicates during this run
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

# ─────────────────────────────
# HELPERS
# ─────────────────────────────
def now_ist():
    return datetime.now(IST)

def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def send_message(text, url_button=None):
    """Send to channel with optional single URL button."""
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

# ─────────────────────────────
# NEWS (hourly)
# ─────────────────────────────
def fetch_news_newsapi():
    """Prefer NewsAPI if key is present; return list of dicts {title, desc, url, source}."""
    if not NEWS_API_KEY:
        return []

    params = {
        "apiKey": NEWS_API_KEY,
        "language": "en",
        "pageSize": 8,
        # Focus on Indian markets or global stories impacting India
        "q": "India stock OR Sensex OR Nifty OR RBI OR tariffs OR crude oil OR rupee OR 'global markets'",
        # Restrict to reliable business publishers
        "domains": ",".join([
            "economictimes.indiatimes.com",
            "moneycontrol.com",
            "business-standard.com",
            "livemint.com",
            "financialexpress.com",
            "reuters.com",
            "bloomberg.com"
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
    """Fallback: Google News RSS."""
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
            # strip Google redirect if present
            m = re.search(r"url=(https?[^&]+)", link)
            if m: link = requests.utils.unquote(m.group(1))
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

# ─────────────────────────────
# INDICES SNAPSHOT (Nifty, Sensex, BankNifty)
# via Yahoo Finance (stable & simple)
# ─────────────────────────────
def fetch_indices_snapshot():
    try:
        q = "%5EBSESN,%5ENSEI,%5ENSEBANK"  # Sensex, Nifty50, BankNifty
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={q}"
        data = requests.get(url, timeout=20).json()
        res = { }
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

# ─────────────────────────────
# FII / DII (Moneycontrol table scrape)
# ─────────────────────────────
def fetch_fii_dii():
    url = "https://www.moneycontrol.com/stocks/marketstats/fii-dii-activity/"
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        # Look for the first table that has 'FII' and 'DII' in it
        tbl = None
        for t in soup.find_all("table"):
            txt = t.get_text(" ", strip=True)
            if "FII" in txt and "DII" in txt and "Net" in txt:
                tbl = t; break
        if not tbl: return None
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th","td"])] for tr in tbl.find_all("tr")]
        # Try to detect latest row (usually row 1 after headers)
        latest = None
        for r in rows[1:4]:
            if any(x for x in r if re.search(r"\d{1,2}-[A-Za-z]{3}-\d{4}", x)):  # date like 17-Aug-2025
                latest = r; break
        if not latest:
            latest = rows[1] if len(rows) > 1 else None
        if not latest: return None

        text = " ".join(latest)
        # Extract date and net values (heuristic)
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

# ─────────────────────────────
# IPO (Chittorgarh + IPOWatch GMP)
# ─────────────────────────────
def fetch_gmp_map():
    """Return dict name->gmp string using IPOWatch consolidated page."""
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
    # loose match
    for k,v in gmp_map.items():
        if key.startswith(k) or k in key:
            return v
    return "N/A"

def fetch_ipo_list():
    """Return list of current & upcoming IPOs with basics & detail link (Chittorgarh)."""
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
    """Parse latest subscription from IPO detail page (QIB/NII/Retail/Total)."""
    try:
        r = requests.get(detail_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        # find a table that has these headers
        tables = soup.find_all("table")
        cand = None
        for t in tables:
            txt = t.get_text(" ", strip=True)
            if all(w in txt for w in ["
  
