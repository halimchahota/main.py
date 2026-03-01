import requests
import time
import logging

BOT_TOKEN = "8318064533:AAFCDUl4qS3tIKgU1bQeDzoKvpwTfkrC2Ro"
CHAT_ID = "@abdel_tra"

SYMBOL = "XAUUSD"
API_URL = "https://api.gold-api.com/price/XAU"

CHECK_INTERVAL = 60

logging.basicConfig(level=logging.INFO)

# =========================
# ارسال رسالة تليجرام
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": msg
    }
    requests.post(url, data=data)


# =========================
# جلب السعر
# =========================
def get_price():
    r = requests.get(API_URL)
    return float(r.json()["price"])


# =========================
# تحديد الدعم والمقاومة
# =========================
def calculate_support_resistance(price):

    support = price - 5
    resistance = price + 5

    return support, resistance


# =========================
# اكتشاف الفجوة السعرية FVG
# =========================
def detect_fvg(price):

    upper_gap = price + 3
    lower_gap = price - 3

    return lower_gap, upper_gap


# =========================
# اكتشاف Order Block
# =========================
def detect_order_block(price):

    buy_ob = price - 4
    sell_ob = price + 4

    return buy_ob, sell_ob


# =========================
# ارسال اشارة كاملة
# =========================
def send_signal(signal, price):

    sl = price - 3 if signal == "BUY" else price + 3

    tp1 = price + 10 if signal == "BUY" else price - 10
    tp2 = price + 20 if signal == "BUY" else price - 20
    tp3 = price + 30 if signal == "BUY" else price - 30

    buy_limit = price - 2
    sell_limit = price + 2

    message = f"""
📊 GOLD SIGNAL

Signal: {signal}
Entry: {price}

Stop Loss: {sl}

TP1: {tp1}
TP2: {tp2}
TP3: {tp3}

Pending Orders:

BUY LIMIT: {buy_limit}
SELL LIMIT: {sell_limit}
"""

    send_telegram(message)


# =========================
# النظام الرئيسي
# =========================
last_signal = None

while True:

    try:

        price = get_price()

        support, resistance = calculate_support_resistance(price)

        fvg_low, fvg_high = detect_fvg(price)

        buy_ob, sell_ob = detect_order_block(price)

        logging.info(price)

        if price <= support and last_signal != "BUY":

            send_signal("BUY", price)
            last_signal = "BUY"

        elif price >= resistance and last_signal != "SELL":

            send_signal("SELL", price)
            last_signal = "SELL"

        time.sleep(CHECK_INTERVAL)

    except Exception as e:

        logging.error(e)
        time.sleep(10)
