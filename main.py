import os
import logging
import requests
from requests.exceptions import RequestException
from time import sleep

# الأفضل وضعها في Railway Variables
BOT_TOKEN = 8318064533:AAHlQa7lKoX6uYALYLJ9EbMX8QlNfnHoKgU
CHAT_ID = os.getenv("CHAT_ID", "@abdel_tra")

GOLD_PRICE_URL = "https://api.gold-api.com/price/XAU"
CHECK_INTERVAL = 60

BUY_THRESHOLD = 3326.0
SELL_THRESHOLD = 3313.0
REQUEST_TIMEOUT = 10

# ===== إعدادات BUY (مثل مثالك) =====
BUY_SL_OFFSET  = 3
BUY_TP1_OFFSET = 19
BUY_TP2_OFFSET = 35
BUY_TP3_OFFSET = 50

# ===== إعدادات SELL (عكس BUY) =====
SELL_SL_OFFSET  = 3
SELL_TP1_OFFSET = 19
SELL_TP2_OFFSET = 35
SELL_TP3_OFFSET = 50

# ===== الأوامر المعلقة =====
ENABLE_PENDING = True
PENDING_OFFSET = 5   # +/- 5 حول الدخول

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def get_gold_price():
    resp = requests.get(GOLD_PRICE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if "price" in data:
        return float(data["price"])

    for key in ("price_usd", "priceUSD", "value", "ask"):
        if key in data:
            return float(data[key])

    raise ValueError("Price field not found in API response")


def send_telegram_message(text: str) -> bool:
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN is empty. Set it in Railway Variables.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return True
    except RequestException as e:
        logging.error("Failed to send Telegram message: %s", e)
        return False


def build_buy_message(entry: float) -> str:
    entry = round(entry, 2)
    sl  = round(entry - BUY_SL_OFFSET, 2)
    tp1 = round(entry + BUY_TP1_OFFSET, 2)
    tp2 = round(entry + BUY_TP2_OFFSET, 2)
    tp3 = round(entry + BUY_TP3_OFFSET, 2)

    msg = (
        f"🔵 BUY GOLD\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"TP3: {tp3}\n"
    )

    if ENABLE_PENDING:
        buy_limit  = round(entry - PENDING_OFFSET, 2)  # شراء من أسفل
        sell_limit = round(entry + PENDING_OFFSET, 2)  # بيع من أعلى
        msg += (
            f"\n⏳ Pending Orders:\n"
            f"Buy Limit: {buy_limit}\n"
            f"Sell Limit: {sell_limit}\n"
        )

    return msg


def build_sell_message(entry: float) -> str:
    entry = round(entry, 2)
    sl  = round(entry + SELL_SL_OFFSET, 2)
    tp1 = round(entry - SELL_TP1_OFFSET, 2)
    tp2 = round(entry - SELL_TP2_OFFSET, 2)
    tp3 = round(entry - SELL_TP3_OFFSET, 2)

    msg = (
        f"🔴 SELL GOLD\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"TP3: {tp3}\n"
    )

    if ENABLE_PENDING:
        sell_limit = round(entry + PENDING_OFFSET, 2)  # بيع من أعلى
        buy_limit  = round(entry - PENDING_OFFSET, 2)  # شراء من أسفل
        msg += (
            f"\n⏳ Pending Orders:\n"
            f"Sell Limit: {sell_limit}\n"
            f"Buy Limit: {buy_limit}\n"
        )

    return msg


def main():
    last_signal = None
    backoff = 1

    while True:
        try:
            price = get_gold_price()
            logging.info("Gold price: %s", price)

            if price > BUY_THRESHOLD and last_signal != "BUY":
                message = build_buy_message(price)
                if send_telegram_message(message):
                    last_signal = "BUY"

            elif price < SELL_THRESHOLD and last_signal != "SELL":
                message = build_sell_message(price)
                if send_telegram_message(message):
                    last_signal = "SELL"

            backoff = 1
            sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.error("Error: %s", e)
            sleep(min(60, backoff))
            backoff = min(300, backoff * 2)


if __name__ == "__main__":
    main()
