import os
import time
import logging
import requests
from requests.exceptions import RequestException
from time import sleep

# Configuration from environment
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOLD_PRICE_URL = os.getenv("GOLD_PRICE_URL", "https://api.gold-api.com/price/XAU")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "3326"))
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "3313"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

def get_gold_price():
    """Fetch the gold price JSON and return numeric price. Raises on failure."""
    try:
        resp = requests.get(GOLD_PRICE_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # Adjust this key if the API returns a different structure
        if "price" in data:
            return float(data["price")
        # fallback checks for other common fields
        for key in ("price_usd", "priceUSD", "value", "ask"):
            if key in data:
                return float(data[key])
        raise ValueError("Price field not found in API response")
    except (RequestException, ValueError):
        raise

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return True
    except RequestException as e:
        logging.error("Failed to send Telegram message: %s", e)
        return False

def main():
    last_signal = None
    backoff = 1
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

            backoff = 1  # reset backoff on success
            sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.error("Error fetching price or sending message: %s", e)
            sleep(min(60, backoff))
            backoff = min(300, backoff * 2)  # exponential backoff up to 5 minutes

if __name__ == "__main__":
    main()
