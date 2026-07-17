# -*- coding: utf-8 -*-

import os
import time
import logging
import requests
import pandas as pd
import yfinance as yf

from datetime import datetime


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


# =========================
# TELEGRAM SETTINGS
# =========================

TELEGRAM_TOKEN = "ضع_توكن_البوت_هنا"

TELEGRAM_CHAT_ID = "ضع_ID_هنا"



# =========================
# SYMBOLS
# الأسواق التي يراقبها البوت
# =========================

SYMBOLS = {

    # المعادن
    "GOLD": "GC=F",

    # العملات الرقمية
    "BTC": "BTC-USD",

    # المؤشرات العالمية
    "US100": "^NDX",
    "US30": "^DJI",
    "GER40": "^GDAXI",

    # العملات الرئيسية Forex
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "NZDUSD": "NZDUSD=X"
}



# =========================
# DATA SETTINGS
# =========================

TIMEFRAME = "30m"
PERIOD = "10d"



# =========================
# SEND TELEGRAM
# =========================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(
            url,
            data=data,
            timeout=10
        )

        return response.json()

    except Exception as e:
        logging.error(
            f"Telegram Error: {e}"
        )

        return None



# =========================
# GET MARKET DATA
# =========================

def get_data(symbol):

    try:

        df = yf.download(
            symbol,
            period=PERIOD,
            interval=TIMEFRAME,
            progress=False
        )


        if df.empty:
            logging.warning(
                f"No data for {symbol}"
            )
            return None


        df.dropna(inplace=True)

        return df


    except Exception as e:

        logging.error(
            f"Data Error {symbol}: {e}"
        )

        return None
        # =========================
# TECHNICAL INDICATORS
# =========================


def add_indicators(df):

    # =====================
    # EMA TREND
    # =====================

    df["EMA50"] = (
        df["Close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    df["EMA200"] = (
        df["Close"]
        .ewm(span=200, adjust=False)
        .mean()
    )


    # =====================
    # RSI
    # =====================

    delta = df["Close"].diff()

    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)


    avg_gain = (
        gain.rolling(14)
        .mean()
    )

    avg_loss = (
        loss.rolling(14)
        .mean()
    )


    rs = avg_gain / avg_loss

    df["RSI"] = (
        100 - (100 / (1 + rs))
    )


    # =====================
    # MACD
    # =====================

    ema12 = (
        df["Close"]
        .ewm(
            span=12,
            adjust=False
        )
        .mean()
    )

    ema26 = (
        df["Close"]
        .ewm(
            span=26,
            adjust=False
        )
        .mean()
    )


    df["MACD"] = ema12 - ema26


    df["MACD_SIGNAL"] = (
        df["MACD"]
        .ewm(
            span=9,
            adjust=False
        )
        .mean()
    )


    # =====================
    # ATR (Volatility)
    # =====================

    high_low = (
        df["High"] - df["Low"]
    )

    high_close = (
        abs(
            df["High"] -
            df["Close"].shift()
        )
    )

    low_close = (
        abs(
            df["Low"] -
            df["Close"].shift()
        )
    )


    ranges = pd.concat(
        [
            high_low,
            high_close,
            low_close
        ],
        axis=1
    )


    true_range = (
        ranges.max(axis=1)
    )


    df["ATR"] = (
        true_range
        .rolling(14)
        .mean()
    )


    return df



# =========================
# SUPPORT & RESISTANCE
# =========================


def get_levels(df):

    recent = df.tail(50)


    support = (
        recent["Low"]
        .min()
    )


    resistance = (
        recent["High"]
        .max()
    )


    return support, resistance
    # =========================
# SIGNAL ENGINE
# =========================


def analyze_market(df, name):

    df = add_indicators(df)

    last = df.iloc[-1]


    score_buy = 0
    score_sell = 0


    reasons_buy = []
    reasons_sell = []



    # =====================
    # TREND FILTER EMA
    # =====================

    if last["Close"] > last["EMA50"]:
        score_buy += 1
        reasons_buy.append("السعر فوق EMA50")

    else:
        score_sell += 1
        reasons_sell.append("السعر تحت EMA50")



    if last["EMA50"] > last["EMA200"]:
        score_buy += 2
        reasons_buy.append("اتجاه صاعد قوي")

    elif last["EMA50"] < last["EMA200"]:
        score_sell += 2
        reasons_sell.append("اتجاه هابط قوي")



    # =====================
    # RSI FILTER
    # =====================

    if 40 < last["RSI"] < 70:

        score_buy += 1
        reasons_buy.append(
            "RSI يدعم الصعود"
        )

    elif 30 < last["RSI"] < 60:

        score_sell += 1
        reasons_sell.append(
            "RSI يدعم الهبوط"
        )



    # =====================
    # MACD CONFIRMATION
    # =====================

    if last["MACD"] > last["MACD_SIGNAL"]:

        score_buy += 2
        reasons_buy.append(
            "MACD إيجابي"
        )

    else:

        score_sell += 2
        reasons_sell.append(
            "MACD سلبي"
        )



    # =====================
    # FINAL DECISION
    # =====================

    confidence = max(
        score_buy,
        score_sell
    )


    if score_buy >= 5:

        signal = "BUY"
        reasons = reasons_buy


    elif score_sell >= 5:

        signal = "SELL"
        reasons = reasons_sell


    else:

        signal = "WAIT"
        reasons = [
            "لا توجد شروط كافية للدخول"
        ]



    support, resistance = get_levels(df)


    result = {

        "symbol": name,

        "signal": signal,

        "confidence": confidence,

        "price": round(
            float(last["Close"]),
            5
        ),

        "rsi": round(
            float(last["RSI"]),
            2
        ),

        "support": round(
            float(support),
            5
        ),

        "resistance": round(
            float(resistance),
            5
        ),

        "reasons": reasons

    }


    return result
    # =========================
# FORMAT SIGNAL MESSAGE
# =========================


def format_message(result):

    if result["signal"] == "WAIT":
        return None


    emoji = "🟢" if result["signal"] == "BUY" else "🔴"


    message = f"""
<b>📊 PRO MAX SIGNAL</b>

{emoji} <b>{result['signal']}</b>
━━━━━━━━━━━━━━

📌 الأصل: {result['symbol']}

💰 السعر:
{result['price']}

📈 قوة الإشارة:
{result['confidence']}/7

📊 RSI:
{result['rsi']}

🟦 الدعم:
{result['support']}

🟥 المقاومة:
{result['resistance']}


<b>التحليل:</b>
"""


    for reason in result["reasons"]:
        message += f"\n✅ {reason}"


    message += """

━━━━━━━━━━━━━━
⚠️ إشارة تحليلية فقط
إدارة رأس المال ضرورية
"""


    return message



# =========================
# SCANNER
# =========================


def scan_markets():


    for name, symbol in SYMBOLS.items():

        logging.info(
            f"Analyzing {name}"
        )


        data = get_data(symbol)


        if data is None:
            continue


        result = analyze_market(
            data,
            name
        )


        message = format_message(
            result
        )


        if message:

            send_telegram(
                message
            )

            logging.info(
                f"Signal sent {name}"
            )



# =========================
# MAIN LOOP
# =========================


if __name__ == "__main__":


    while True:

        try:

            scan_markets()


            # إعادة الفحص كل 30 دقيقة

            time.sleep(
                1800
            )


        except Exception as e:

            logging.error(e)

            time.sleep(
                60
            )
            # =========================
# SIGNAL MEMORY
# منع تكرار نفس الإشارة
# =========================

last_signals = {}


def check_duplicate(symbol, signal):

    key = symbol

    if key in last_signals:

        if last_signals[key] == signal:
            return True


    last_signals[key] = signal

    return False



# =========================
# RISK MANAGEMENT
# ATR BASED SL / TP
# =========================


def calculate_targets(df, signal):

    last = df.iloc[-1]


    price = float(last["Close"])

    atr = float(last["ATR"])


    if signal == "BUY":

        stop_loss = price - (atr * 2)

        take_profit = price + (atr * 3)


    else:

        stop_loss = price + (atr * 2)

        take_profit = price - (atr * 3)



    return (
        round(stop_loss, 5),
        round(take_profit, 5)
    )



# =========================
# VIP ANALYSIS
# =========================


def vip_analyze(df, name):

    df = add_indicators(df)


    result = analyze_market(
        df,
        name
    )


    if result["signal"] != "WAIT":


        if check_duplicate(
            name,
            result["signal"]
        ):

            result["signal"] = "WAIT"



    if result["signal"] != "WAIT":


        sl, tp = calculate_targets(
            df,
            result["signal"]
        )


        result["stop_loss"] = sl

        result["take_profit"] = tp


    return result



# =========================
# VIP MESSAGE
# =========================


def vip_message(result):


    if result["signal"] == "WAIT":

        return None



    emoji = (
        "🟢"
        if result["signal"] == "BUY"
        else "🔴"
    )


    text = f"""

<b>👑 PRO MAX VIP SIGNAL</b>

{emoji} <b>{result['signal']}</b>

━━━━━━━━━━━━

📌 الأصل:
{result['symbol']}

💰 الدخول:
{result['price']}

🛑 وقف الخسارة:
{result['stop_loss']}

🎯 الهدف:
{result['take_profit']}


📊 الثقة:
{result['confidence']}/7

📈 RSI:
{result['rsi']}


<b>التحليل:</b>
"""


    for r in result["reasons"]:

        text += f"\n✅ {r}"


    text += """

━━━━━━━━━━━━

⚠️ ليست توصية مالية
"""


    return text
    # =========================
# VIP MARKET SCANNER
# =========================


def scan_markets():


    for name, symbol in SYMBOLS.items():


        logging.info(
            f"VIP Scanning: {name}"
        )


        data = get_data(symbol)


        if data is None:
            continue



        try:

            result =# =========================
# FINAL VIP ANALYSIS
# =========================


def vip_analyze(df, name):


    # التحليل الأساسي

    df = add_indicators(df)


    result = analyze_market(
        df,
        name
    )


    # فلتر الجودة المتقدم

    result = quality_check(
        df,
        result,
        SYMBOLS[name]
    )



    # إذا أصبحت الإشارة ضعيفة

    if result["signal"] == "WAIT":

        return result



    # منع تكرار الإشارة

    if check_duplicate(
        name,
        result["signal"]
    ):

        result["signal"] = "WAIT"

        return result



    # حساب الهدف ووقف الخسارة

    sl, tp =# =========================
# SMART SL / TP SYSTEM
# حسب نوع السوق
# =========================


def calculate_targets(df, signal, name):


    last = df.iloc[-1]


    price = float(
        last["Close"]
    )


    atr = float(
        last["ATR"]
    )


    settings = get_market_setting(
        name
    )


    sl_multiplier = settings["atr_sl"]

    tp_multiplier = settings["atr_tp"]



    if signal == "BUY":


        stop_loss = (
            price -
            (atr * sl_multiplier)
        )


        take_profit = (
            price +
            (atr * tp_multiplier)
        )



    elif signal == "SELL":


        stop_loss = (
            price +
            (atr * sl_multiplier)
        )


        take_profit = (
            price -
            (atr * tp_multiplier)
        )


    else:

        return None, None



    return (

        round(
            stop_loss,
            5
        ),

        round(
            take_profit,
            5
        )

    ) (
        df,
        result["signal"]
    )


    result["stop_loss"] = sl

    result["take_profit"] = tp



    return result (
                data,
                name
            )


            message = vip_message(
                result
            )


            if message:


                send_telegram(
                    message
                )


                logging.info(
                    f"VIP Signal Sent: {name}"
                )


            else:

                logging.info(
                    f"No valid signal: {name}"
                )


        except Exception as e:

            logging.error(
                f"Analysis error {name}: {e}"
            )



# =========================
# START BOT
# =========================


def start_bot():

    logging.info(
        "PRO MAX VIP BOT STARTED"
    )


    while True:


        try:

            scan_markets()


            # فحص كل 30 دقيقة

            time.sleep(
                1800
            )


        except Exception as e:


            logging.error(e)


            time.sleep(
                60
            )



# =========================
# RUN
# =========================

if __name__ == "__main__":

    start_bot()
    # =========================
# ADVANCED FILTERS
# =========================


def candle_confirmation(df, signal):

    last = df.iloc[-1]
    previous = df.iloc[-2]


    # شمعة صاعدة قوية

    bullish = (
        last["Close"] > last["Open"]
        and
        last["Close"] > previous["Close"]
    )


    # شمعة هابطة قوية

    bearish = (
        last["Close"] < last["Open"]
        and
        last["Close"] < previous["Close"]
    )


    if signal == "BUY" and bullish:
        return True


    if signal == "SELL" and bearish:
        return True


    return False



# =========================
# HIGHER TIMEFRAME TREND
# =========================


def higher_timeframe_filter(symbol, signal):

    try:

        df = yf.download(
            symbol,
            period="30d",
            interval="1h",
            progress=False
        )


        if df.empty:
            return False


        ema50 = (
            df["Close"]
            .ewm(span=50)
            .mean()
            .iloc[-1]
        )


        ema200 = (
            df["Close"]
            .ewm(span=200)
            .mean()
            .iloc[-1]
        )


        price = (
            float(df["Close"].iloc[-1])
        )


        if signal == "BUY":

            return (
                price > ema50
                and ema50 > ema200
            )


        if signal == "SELL":

            return (
                price < ema50
                and ema50 < ema200
            )


    except Exception:

        return False



    return False



# =========================
# FINAL QUALITY CHECK
# =========================


def quality_check(df, result, symbol):


    if result["signal"] == "WAIT":

        return result



    score = result["confidence"]



    # تأكيد الشمعة

    if candle_confirmation(
        df,
        result["signal"]
    ):

        score += 1

        result["reasons"].append(
            "تأكيد حركة الشمعة"
        )


    else:

        score -= 1



    # اتجاه الساعة

    if higher_timeframe_filter(
        symbol,
        result["signal"]
    ):

        score += 2

        result["reasons"].append(
            "اتجاه الساعة مؤكد"
        )

    else:

        score -= 1



    result["confidence"] = score



    # لا نرسل إلا القوي

    if score < 6:

        result["signal"] = "WAIT"



    return result
    # =========================
# MARKET SETTINGS
# إعدادات خاصة لكل سوق
# =========================


MARKET_SETTINGS = {


    # الذهب
    "GOLD": {

        "min_score": 6,
        "atr_sl": 2.2,
        "atr_tp": 3.5
    },


    # البيتكوين
    "BTC": {

        "min_score": 7,
        "atr_sl": 2.8,
        "atr_tp": 4
    },


    # المؤشرات
    "US100": {

        "min_score": 6,
        "atr_sl": 2,
        "atr_tp": 3
    },

    "US30": {

        "min_score": 6,
        "atr_sl": 2,
        "atr_tp": 3
    },


    "GER40": {

        "min_score": 6,
        "atr_sl": 2,
        "atr_tp": 3
    },


    # العملات الرئيسية

    "EURUSD": {

        "min_score": 6,
        "atr_sl": 1.8,
        "atr_tp": 2.8
    },

    "GBPUSD": {

        "min_score": 6,
        "atr_sl": 2,
        "atr_tp": 3
    },


    "USDJPY": {

        "min_score": 6,
        "atr_sl": 1.8,
        "atr_tp": 2.8
    },


    "AUDUSD": {

        "min_score": 6,
        "atr_sl": 1.8,
        "atr_tp": 2.8
    },


    "USDCAD": {

        "min_score": 6,
        "atr_sl": 1.8,
        "atr_tp": 2.8
    },


    "USDCHF": {

        "min_score": 6,
        "atr_sl": 1.8,
        "atr_tp": 2.8
    },


    "NZDUSD": {

        "min_score": 6,
        "atr_sl": 1.8,
        "atr_tp": 2.8
    }

}



# =========================
# GET MARKET CONFIG
# =========================


def get_market_setting(name):

    return MARKET_SETTINGS.get(
        name,
        {
            "min_score": 6,
            "atr_sl": 2,
            "atr_tp": 3
        }
    )
