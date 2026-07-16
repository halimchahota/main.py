import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)


# إعداد السجل
logging.basicConfig(
    level=logging.INFO
)


# جلب التوكن من Railway Variables
TOKEN = os.getenv("BOT_TOKEN")


if not TOKEN:
    raise ValueError(
        "BOT_TOKEN غير موجود في Railway Variables"
    )


SYMBOLS = [
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "BTCUSD",
    "US100",
    "US30",
    "GER40",
    "WTI"
]


MIN_SIGNAL = 70
LOT = 0.01



def analyze_market(symbol):

    score = 0
    reasons = []


    # اختبار مبدئي
    conditions = {

        "trend": True,
        "rsi": True,
        "macd": True,
        "candle": True

    }


    if conditions["trend"]:
        score += 30
        reasons.append("الاتجاه متوافق")


    if conditions["rsi"]:
        score += 20
        reasons.append("RSI داعم")


    if conditions["macd"]:
        score += 20
        reasons.append("MACD داعم")


    if conditions["candle"]:
        score += 30
        reasons.append("تأكيد شمعة")


    signal = "BUY" if score >= MIN_SIGNAL else "WAIT"


    return {
        "symbol": symbol,
        "signal": signal,
        "score": score,
        "reasons": reasons
    }



def trade_levels():

    entry = 100
    sl = 98
    tp1 = 103
    tp2 = 104

    return entry, sl, tp1, tp2



async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "⭐ ProMax VIP يعمل على Railway\n\n"
        "/analyze لتحليل الأسواق"
    )



async def analyze(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = "⭐ ProMax VIP SIGNALS\n\n"


    for symbol in SYMBOLS:

        result = analyze_market(symbol)


        if result["score"] >= MIN_SIGNAL:

            entry, sl, tp1, tp2 = trade_levels()


            message += f"""
📌 {symbol}

🟢 {result['signal']}

القوة:
{result['score']}%

Lot:
{LOT}

Entry:
{entry}

SL:
{sl}

TP1:
{tp1}

TP2:
{tp2}

"""


    await update.message.reply_text(message)



def main():

    app = Application.builder()\
        .token(TOKEN)\
        .build()


    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(
        CommandHandler(
            "analyze",
            analyze
        )
    )


    print(
        "⭐ ProMax VIP Railway Running"
    )


    app.run_polling()



if __name__ == "__main__":
    main()
