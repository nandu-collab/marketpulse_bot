import os
import requests
import time
import feedparser
from datetime import datetime, date
from bs4 import BeautifulSoup
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# === Config ===
IST = "Asia/Kolkata"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

NEWS_FEEDS = {
    "[Market Update]": "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "[Business News]": "https://www.livemint.com/rss/markets",
    "[Global Impact]": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
}
posted_links = set()

# Flask keep-alive
app = Flask(__name__)
@app.get("/")
def ping():
    return {"status": "OK", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

# === News Function ===
def fetch_and_post_news():
    for tag, url in NEWS_FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            link = entry.get("link", "")
            if link in posted_links:
                continue
            title = entry.get("title", "...").strip()
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text()[:180] + "..."

            text = f"{tag} {title}\n\n{summary}"
            reply = {"inline_keyboard": [[{"text": "Read", "url": link}]]}

            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML", "reply_markup": reply},
                timeout=20
            )
            posted_links.add(link)
            time.sleep(1)

# === IPO Functions ===
def fetch_ipo_calendar(limit=5):
    url = "https://www.chittorgarh.com/report/ipo-list-by-time-table-and-lot-size/118/all/?year=2025"
    res = requests.get(url, headers={"User-Agent":"Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table")
    out = []
    if table:
        for row in table.find_all("tr")[1:limit+1]:
            cols = row.find_all("td")
            if len(cols)<7: continue
            link = cols[0].find("a")
            detail_url = ("https://www.chittorgarh.com" + link["href"]) if link and link.get("href","").startswith("/") else ""
            out.append({
                "company": cols[0].get_text(strip=True),
                "open": cols[2].get_text(strip=True),
                "close": cols[3].get_text(strip=True),
                "price_band": cols[5].get_text(strip=True),
                "lot": cols[6].get_text(strip=True),
                "detail_url": detail_url
            })
    return out

def fetch_gmp_map():
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    res = requests.get(url, headers={"User-Agent":"Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table")
    m = {}
    if table:
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols)>=4:
                name = cols[0].get_text(strip=True).lower()
                m[name] = (cols[1].get_text(strip=True), cols[3].get_text(strip=True))
    return m

def fetch_ipo_summary(detail_url):
    if not detail_url:
        return ""
    res = requests.get(detail_url, headers={"User-Agent":"Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")
    p = soup.find("p")
    if p:
        return p.get_text(strip=True)[:250] + "..."
    return ""

def post_ipo_updates():
    ipos = fetch_ipo_calendar(limit=5)
    gmpmap = fetch_gmp_map()
    for ipo in ipos:
        name = ipo["company"]
        key = name.lower()
        gmp, est = gmpmap.get(key, ("NA","NA"))
        summary = fetch_ipo_summary(ipo["detail_url"])
        text = ("[IPO] {comp}\nOpen: {o}  Close: {c}\nPrice Band: {pb}  Lot: {lot}\n"
                "GMP: {gmp}  Est. List: {est}\n").format(
            comp=name, o=ipo["open"], c=ipo["close"],
            pb=ipo["price_band"], lot=ipo["lot"],
            gmp=gmp, est=est
        )
        if summary:
            text += "\n" + summary
        reply = {"inline_keyboard": [[{"text": "Read", "url": ipo["detail_url"] or "https://www.chittorgarh.com"}]]}
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML", "reply_markup": reply},
            timeout=20
        )
        time.sleep(1)

# === Scheduler ===
sched = BackgroundScheduler(timezone=IST)
sched.add_job(fetch_and_post_news, "interval", minutes=60, id="hourly_news")
sched.add_job(post_ipo_updates, CronTrigger(hour=9, minute=10), id="ipo_morning")
sched.add_job(post_ipo_updates, CronTrigger(hour=18, minute=0), id="ipo_evening")
sched.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
  
