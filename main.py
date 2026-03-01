import time
import logging
import requests
from requests.exceptions import RequestException
from time import sleep

BOT_TOKEN = "8318064533:AAHlQa7lKoX6uYALYLJ9EbMX8QlNfnHoKgU"
CHAT_ID = "@abdel_tra"

GOLD_PRICE_URL = "https://api.gold-api.com/price/XAU"
CHECK_INTERVAL = 60
REQUEST_TIMEOUT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# إعدادات الأهداف
SL_DISTANCE = 3
TP1_DISTANCE = 19
TP2_DISTANCE = 35
TP3_DISTANCE = 50

LIMIT_DISTANCE = 5

def get_gold_price():
    resp = requests.get(GOLD_PRICE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return float(data["price"])

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return True
    except RequestException as e:
        logging.error("Telegram error: %s", e)
        return False

def create_buy_signal(price):

    sl = price - SL_DISTANCE
    tp1 = price + TP1_DISTANCE
    tp2 = price + TP2_DISTANCE
    tp3 = price + TP3_DISTANCE

    buy_limit = price - LIMIT_DISTANCE
    sell_limit = price + LIMIT_DISTANCE

    message = f"""
🔵 BUY GOLD

Entry: {price:.2f}

Stop Loss: {sl:.2f}

Take Profit 1: {tp1:.2f}
Take Profit 2: {tp2:.2f}
Take Profit 3: {tp3:.2f}

Pending Orders:
Buy Limit: {buy_limit:.2f}
Sell Limit: {sell_limit:.2f}
"""

    return message

def create_sell_signal(price):

    sl = price + SL_DISTANCE
    tp1 = price - TP1_DISTANCE
    tp2 = price - TP2_DISTANCE
    tp3 = price - TP3_DISTANCE

    buy_limit = price - LIMIT_DISTANCE
    sell_limit = price + LIMIT_DISTANCE

    message = f"""
🔴 SELL GOLD

Entry: {price:.2f}

Stop Loss: {sl:.2f}

Take Profit 1: {tp1:.2f}
Take Profit 2: {tp2:.2f}
Take Profit 3: {tp3:.2f}

Pending Orders:
Buy Limit: {buy_limit:.2f}
Sell Limit: {sell_limit:.2f}
"""

    return message


def main():

    last_signal = None

    while True:
        try:

            price = get_gold_price()
            logging.info("Gold price: %s", price)

            if last_signal != "BUY":

                message = create_buy_signal(price)
                send_telegram_message(message)
                last_signal = "BUY"

            sleep(CHECK_INTERVAL)

        except Exception as e:

            logging.error("Error: %s", e)
            sleep(30)


if __name__ == "__main__":
    main()
