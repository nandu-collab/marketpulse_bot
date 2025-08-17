import os
import time
import requests
import logging
import schedule
from telegram import Bot
from telegram.constants import ParseMode

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@MarketPulse_India")

bot = Bot(token=BOT_TOKEN)

NEWS_INTERVAL_MIN = 60
IPO_UPDATE_TIME = "09:30"
MARKET_CLOSE_TIME = "16:15"
FII_DII_TIME = "20:00"

# ========== HELPERS ==========

def send_message(text):
    try:
        bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Send error: {e}")

# ====== MARKET DATA ======

def fetch_indices():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/^NSEI"
        nifty = requests.get(url).json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

        url2 = "https://query1.finance.yahoo.com/v8/finance/chart/^BSESN"
        sensex = requests.get(url2).json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

        url3 = "https://query1.finance.yahoo.com/v8/finance/chart/^NSEBANK"
        banknifty = requests.get(url3).json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

        msg = f"📊 <b>Market Close Update</b>\n\n" \
              f"• Nifty 50: {nifty}\n" \
              f"• Sensex: {sensex}\n" \
              f"• Bank Nifty: {banknifty}"
        send_message(msg)

    except Exception as e:
        logging.error(f"Indices error: {e}")

def fetch_fii_dii():
    try:
        # Example free API (replace if you have better source)
        url = "https://www.moneycontrol.com/mc/widget/mc_fiidii/fii_dii_json.php"
        data = requests.get(url).json()

        fii = data["fii"]["net"] if "fii" in data else "N/A"
        dii = data["dii"]["net"] if "dii" in data else "N/A"

        msg = f"💰 <b>FII / DII Activity</b>\n\n" \
              f"• FII Net: {fii}\n" \
              f"• DII Net: {dii}"
        send_message(msg)

    except Exception as e:
        logging.error(f"FII/DII error: {e}")

# ====== IPO DATA ======

def fetch_ipo_updates():
    try:
        url = "https://api.ipoapi.in/ipo"  # demo source, replace with real
        data = requests.get(url).json()

        if not data:
            send_message("No IPO updates available today.")
            return

        msg = "🆕 <b>IPO Updates</b>\n\n"
        for ipo in data[:3]:  # first 3 IPOs
            msg += f"📌 <b>{ipo.get('name')}</b>\n" \
                   f"Start: {ipo.get('openDate')} | End: {ipo.get('closeDate')}\n" \
                   f"GMP: {ipo.get('gmp')} | Subscribed: {ipo.get('subscription')}\n" \
                   f"Expected Listing Gain: {ipo.get('expectedGain')}\n\n"

        send_message(msg)

    except Exception as e:
        logging.error(f"IPO error: {e}")

# ====== NEWS ======

def fetch_news():
    try:
        url = "https://newsapi.org/v2/top-headlines?country=in&category=business&apiKey=demo"
        data = requests.get(url).json()

        if not data.get("articles"):
            return

        article = data["articles"][0]
        msg = f"📰 <b>{article['title']}</b>\n{article['description']}\n\n🔗 {article['url']}"
        send_message(msg)

    except Exception as e:
        logging.error(f"News error: {e}")

# ========== SCHEDULE ==========
schedule.every(NEWS_INTERVAL_MIN).minutes.do(fetch_news)
schedule.every().day.at(IPO_UPDATE_TIME).do(fetch_ipo_updates)
schedule.every().day.at(MARKET_CLOSE_TIME).do(fetch_indices)
schedule.every().day.at(FII_DII_TIME).do(fetch_fii_dii)

# ========== MAIN LOOP ==========
if __name__ == "__main__":
    send_message("✅ MarketPulse Bot Started")
    while True:
        schedule.run_pending()
        time.sleep(30)
      
