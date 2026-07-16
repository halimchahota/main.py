# -*- coding: utf-8 -*-

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
import yfinance as yf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger("ProMax_VIP")


# =========================
# RAILWAY CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()


CHECK_INTERVAL_SEC = int(
    os.getenv("CHECK_INTERVAL_SEC", "300")
)

MIN_SIGNAL_SCORE = float(
    os.getenv("MIN_SIGNAL_SCORE", "70")
)


LOT = 0.01


# =========================
# SYMBOLS
# =========================

SYMBOLS = {

    "XAUUSD": "GC=F",

    "EURUSD": "EURUSD=X",

    "GBPUSD": "GBPUSD=X",

    "USDJPY": "USDJPY=X",

    "BTCUSD": "BTC-USD",

    "US100": "NQ=F",

    "US30": "^DJI",

    "GER40": "^GDAXI",

    "WTI": "CL=F"

}


# =========================
# TELEGRAM
# =========================

def send_message(text):

    if not BOT_TOKEN or not CHAT_ID:
        logger.error(
            "BOT_TOKEN or CHAT_ID missing"
        )
        return


    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )


    try:

        requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=15
        )

    except Exception as e:

        logger.error(e)



def send_photo(path, caption):

    if not BOT_TOKEN or not CHAT_ID:
        return


    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendPhoto"
    )


    try:

        with open(path, "rb") as photo:

            requests.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "caption": caption
                },
                files={
                    "photo": photo
                },
                timeout=30
            )

    except Exception as e:

        logger.error(e)



# =========================
# PRICE DATA
# =========================

def get_data(symbol):

    try:

        df = yf.download(
            symbol,
            period="3mo",
            interval="1h",
            progress=False
        )


        if df.empty:
            return None


        return df


    except Exception as e:

        logger.error(
            f"DATA ERROR {symbol}: {e}"
        )

        return None



# =========================
# INDICATORS
# =========================

def indicators(df):

    close = df["Close"]


    df["EMA20"] = (
        close.ewm(span=20)
        .mean()
    )


    df["EMA50"] = (
        close.ewm(span=50)
        .mean()
    )


    delta = close.diff()


    gain = (
        delta.where(delta > 0, 0)
        .rolling(14)
        .mean()
    )


    loss = (
        -delta.where(delta < 0, 0)
        .rolling(14)
        .mean()
    )


    rs = gain / loss


    df["RSI"] = (
        100 - (100 / (1 + rs))
    )


    return df
    # =========================
# SIGNAL ENGINE
# =========================

def analyze(df):

    df = indicators(df)


    last = df.iloc[-1]


    score = 0

    reasons = []


    price = float(
        last["Close"]
    )


    # TREND

    if last["EMA20"] > last["EMA50"]:

        score += 30

        reasons.append(
            "الاتجاه صاعد EMA20>EMA50"
        )

        direction = "BUY"


    else:

        score += 30

        reasons.append(
            "الاتجاه هابط EMA20<EMA50"
        )

        direction = "SELL"



    # RSI

    rsi = float(
        last["RSI"]
    )


    if direction == "BUY" and rsi > 50:

        score += 20

        reasons.append(
            "RSI داعم للشراء"
        )


    elif direction == "SELL" and rsi < 50:

        score += 20

        reasons.append(
            "RSI داعم للبيع"
        )



    # MACD

    ema12 = (
        df["Close"]
        .ewm(span=12)
        .mean()
    )


    ema26 = (
        df["Close"]
        .ewm(span=26)
        .mean()
    )


    macd = ema12 - ema26


    if direction == "BUY" and macd.iloc[-1] > 0:

        score += 20

        reasons.append(
            "MACD إيجابي"
        )


    elif direction == "SELL" and macd.iloc[-1] < 0:

        score += 20

        reasons.append(
            "MACD سلبي"
        )



    # Candle confirmation

    if abs(
        float(last["Close"])
        -
        float(last["Open"])
    ) > 0:

        score += 10

        reasons.append(
            "شمعة تأكيد"
        )


    return {

        "direction": direction,

        "score": round(score,1),

        "price": round(price,5),

        "reasons": reasons

    }



# =========================
# RISK LEVELS
# =========================

def levels(price, direction):


    risk = price * 0.003


    if direction == "BUY":

        return {

            "entry": price,

            "sl": price-risk,

            "tp1": price+(risk*2),

            "tp2": price+(risk*3)

        }


    else:

        return {

            "entry": price,

            "sl": price+risk,

            "tp1": price-(risk*2),

            "tp2": price-(risk*3)

        }



# =========================
# CHART
# =========================

def create_chart(df, symbol, signal):


    path = "/tmp/chart.png"


    plt.figure(
        figsize=(10,5)
    )


    plt.plot(
        df["Close"],
        label="Price"
    )


    plt.plot(
        df["EMA20"],
        label="EMA20"
    )


    plt.plot(
        df["EMA50"],
        label="EMA50"
    )


    plt.title(
        f"{symbol} {signal}"
    )


    plt.legend()


    plt.grid()


    plt.savefig(
        path,
        bbox_inches="tight"
    )


    plt.close()


    return path



# =========================
# SCANNER
# =========================

def scan_market():


    for name, ticker in SYMBOLS.items():


        data = get_data(ticker)


        if data is None:

            continue


        result = analyze(data)


        if result["score"] >= MIN_SIGNAL_SCORE:


            trade = levels(

                result["price"],

                result["direction"]

            )


            text = f"""
⭐ ProMax VIP SIGNAL

📌 {name}

🟢 {result['direction']}

💰 Price:
{result['price']}

📊 Strength:
{result['score']}%

📦 Lot:
{LOT}

Entry:
{trade['entry']}

SL:
{trade['sl']}

TP1:
{trade['tp1']}

TP2:
{trade['tp2']}


Analysis:
"""


            for r in result["reasons"]:

                text += f"\n✅ {r}"



            chart = create_chart(

                data,

                name,

                result["direction"]

            )


            send_photo(
                chart,
                text
            )
