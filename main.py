import os
import logging
import requests
from requests.exceptions import RequestException
from time import sleep
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

# Read sensitive values from environment variables
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GOLD_PRICE_URL = os.getenv("GOLD_PRICE_URL", "https://api.gold-api.com/price/XAU")
CHECK_INTERVAL = 60
BUY_THRESHOLD = 3326.0
SELL_THRESHOLD = 3313.0
REQUEST_TIMEOUT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Configure a session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))

def get_gold_price():
    """Fetch the gold price JSON and return numeric price. Raises on failure."""
    try:
        resp = session.get(GOLD_PRICE_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for key in ("price", "price_usd", "priceUSD", "value", "ask"):
            if key in data:
                return float(data[key])
        raise ValueError("Price field not found in API response")
    except RequestException:
        logging.exception("Network error while fetching gold price")
        raise
    except ValueError:
        logging.exception("Unexpected API response format")
        raise

def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing BOT_TOKEN or CHAT_ID environment variables; message not sent")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return True
    except RequestException:
        logging.exception("Failed to send Telegram message")
        return False

def main():
    last_signal = None
    backoff = 1
    try:
        while True:
            try:
                price = get_gold_price()
                logging.info("Gold price: %s", price)

                if price > BUY_THRESHOLD and last_signal != "BUY":
                    message = f"🔵 BUY GOLD\nPrice: {price}"
                    if send_telegram_message(message):
                        last_signal = "BUY"

                elif price < SELL_THRESHOLD and last_signal != "SELL":
                    message = f"🔴 SELL GOLD\nPrice: {price}"
                    if send_telegram_message(message):
                        last_signal = "SELL"

                backoff = 1
                sleep(CHECK_INTERVAL)

            except Exception:
                logging.exception("Error fetching price or sending message; backing off")
                sleep(min(60, backoff))
                backoff = min(300, backoff * 2)
    except KeyboardInterrupt:
        logging.info("Interrupted by user; shutting down")

if __name__ == "__main__":
    main()