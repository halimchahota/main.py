# -*- coding: utf-8 -*-

import os
import time
import logging
import requests
import pandas as pd
import numpy as np

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
# RAILWAY VARIABLES
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TWELVE_API_KEY = os.getenv(
    "TWELVE_API_KEY",
    ""
).strip()


CHECK_INTERVAL = int(
    os.getenv(
        "CHECK_INTERVAL_SEC",
        "300"
    )
)


MIN_SIGNAL = float(
    os.getenv(
        "MIN_SIGNAL_SCORE",
        "70"
    )
)


LOT = 0.01


# =========================
# MARKETS
# =========================

SYMBOLS = {

    "XAUUSD": "XAU/USD",

    "EURUSD": "EUR/USD",

    "GBPUSD": "GBP/USD",

    "USDJPY": "USD/JPY",

    "BTCUSD": "BTC/USD",

    "US100": "IXIC",

    "US30": "DJI",

    "GER40": "DAX",

    "WTI": "WTI"

}


# =========================
# TELEGRAM
# =========================

def send_text(message):

    if not BOT_TOKEN or not CHAT_ID:
        return


    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )


    try:

        requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=15
        )


    except Exception as e:

        logger.error(e)



def send_image(path, caption):

    if not BOT_TOKEN or not CHAT_ID:
        return


    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendPhoto"
    )


    try:

        with open(path, "rb") as img:

            requests.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "caption": caption
                },
                files={
                    "photo": img
                },
                timeout=30
            )


    except Exception as e:

        logger.error(e)



# =========================
# TWELVE DATA CANDLES
# =========================

def get_candles(symbol):

    url = "https://api.twelvedata.com/time_series"


    params = {

        "symbol": symbol,

        "interval": "1h",

        "outputsize": 200,

        "apikey": TWELVE_API_KEY

    }


    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )


        data = response.json()


        if "values" not in data:

            logger.warning(
                f"No data {symbol}: {data}"
            )

            return None



        df = pd.DataFrame(
            data["values"]
        )


        df = df.rename(
            columns={

                "datetime": "time",

                "open": "open",

                "high": "high",

                "low": "low",

                "close": "close"

            }
        )


        for col in [
            "open",
            "high",
            "low",
            "close"
        ]:

            df[col] = (
                pd.to_numeric(
                    df[col]
                )
            )


        df = df.sort_values(
            "time"
        )


        return df



    except Exception as e:

        logger.error(
            f"API ERROR {symbol}: {e}"
        )

        return None
        # =========================
# INDICATORS
# =========================

def add_indicators(df):

    df["EMA20"] = (
        df["close"]
        .ewm(span=20)
        .mean()
    )

    df["EMA50"] = (
        df["close"]
        .ewm(span=50)
        .mean()
    )


    # RSI

    delta = df["close"].diff()


    gain = (
        delta.clip(lower=0)
        .rolling(14)
        .mean()
    )


    loss = (
        -delta.clip(upper=0)
        .rolling(14)
        .mean()
    )


    rs = gain / loss


    df["RSI"] = (
        100 - (100/(1+rs))
    )


    # MACD

    ema12 = (
        df["close"]
        .ewm(span=12)
        .mean()
    )

    ema26 = (
        df["close"]
        .ewm(span=26)
        .mean()
    )


    df["MACD"] = (
        ema12 - ema26
    )


    # ATR

    high_low = (
        df["high"]
        -
        df["low"]
    )


    df["ATR"] = (
        high_low
        .rolling(14)
        .mean()
    )


    return df



# =========================
# ANALYSIS
# =========================

def analyze(df):

    df = add_indicators(df)


    last = df.iloc[-1]


    score = 0

    reasons = []


    price = float(
        last["close"]
    )


    # Trend

    if last["EMA20"] > last["EMA50"]:

        direction = "BUY"

        score += 30

        reasons.append(
            "Trend صاعد"
        )


    else:

        direction = "SELL"

        score += 30

        reasons.append(
            "Trend هابط"
        )



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

    macd = float(
        last["MACD"]
    )


    if direction == "BUY" and macd > 0:

        score += 20

        reasons.append(
            "MACD إيجابي"
        )


    elif direction == "SELL" and macd < 0:

        score += 20

        reasons.append(
            "MACD سلبي"
        )



    # Candle confirmation

    if last["close"] > last["open"]:

        score += 10

        reasons.append(
            "شمعة صاعدة"
        )

    else:

        score += 10

        reasons.append(
            "شمعة هابطة"
        )


    return {

        "direction": direction,

        "price": round(
            price,
            5
        ),

        "score": round(
            score,
            1
        ),

        "reasons": reasons,

        "df": df

    }



# =========================
# SL TP
# =========================

def calculate_levels(
    price,
    direction
):

    distance = price * 0.003


    if direction == "BUY":

        return (

            price,

            price-distance,

            price+(distance*2),

            price+(distance*3)

        )


    else:

        return (

            price,

            price+distance,

            price-(distance*2),

            price-(distance*3)

        )



# =========================
# CHART
# =========================

def create_chart(
    df,
    symbol,
    direction
):

    path = (
        f"/tmp/{symbol}.png"
    )


    plt.figure(
        figsize=(12,6)
    )


    plt.plot(
        df["close"],
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
        f"{symbol} {direction}"
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
# MARKET SCANNER
# =========================

def scan_market():

    for name, symbol in SYMBOLS.items():

        logger.info(
            f"Checking {name}"
        )


        df = get_candles(symbol)


        if df is None:

            continue


        result = analyze(df)


        if result["score"] < MIN_SIGNAL:

            continue



        entry, sl, tp1, tp2 = calculate_levels(

            result["price"],

            result["direction"]

        )



        message = f"""
⭐ ProMax VIP SIGNAL

📌 {name}

🟢 {result['direction']}

💰 Price:
{result['price']}

📊 Strength:
{result['score']}%

📦 Lot:
{LOT}

🎯 Entry:
{entry}

🛑 Stop Loss:
{sl}

✅ TP1:
{tp1}

✅ TP2:
{tp2}


📈 Analysis:
"""


        for item in result["reasons"]:

            message += (
                f"\n✅ {item}"
            )



        chart = create_chart(

            result["df"],

            name,

            result["direction"]

        )


        send_image(

            chart,

            message

        )



# =========================
# START LOOP
# =========================

def main():

    logger.info(
        "⭐ ProMax VIP Started"
    )


    if not TWELVE_API_KEY:

        logger.error(
            "Missing TWELVE_API_KEY"
        )

        return



    while True:


        try:

            scan_market()


        except Exception as e:

            logger.error(
                f"ERROR: {e}"
            )



        time.sleep(
            CHECK_INTERVAL
        )



# =========================
# RUN
# =========================

if __name__ == "__main__":

    main()
