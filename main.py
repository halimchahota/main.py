# ProMax VIP V1 - Single File

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)


# ==========================
# الإعدادات
# ==========================

BOT_TOKEN = "8318064533:AAGemv00llTYPiu3-5jQpRb_m-h_TWc7D1U"

LOT = 0.01

MIN_SIGNAL = 70


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


# ==========================
# محرك التحليل
# ==========================

def analyze_market(symbol):

    score = 0
    reasons = []


    # بيانات تجريبية
    trend = True
    rsi = True
    macd = True
    candle = True


    if trend:
        score += 30
        reasons.append(
            "اتجاه السوق متوافق"
        )


    if rsi:
        score += 20
        reasons.append(
            "RSI داعم"
        )


    if macd:
        score += 20
        reasons.append(
            "MACD داعم"
        )


    if candle:
        score += 30
        reasons.append(
            "شمعة تأكيد"
        )


    if score >= MIN_SIGNAL:

        signal = "BUY"

    else:

        signal = "WAIT"



    return {

        "symbol": symbol,
        "signal": signal,
        "score": score,
        "reasons": reasons

    }



# ==========================
# إدارة الصفقة
# ==========================

def calculate_trade(signal):

    entry = 100
    atr = 2


    if signal == "BUY":

        return {

            "entry": entry,
            "sl": entry - atr,
            "tp1": entry + atr * 1.5,
            "tp2": entry + atr * 2

        }


    return None



# ==========================
# Telegram
# ==========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
"""
⭐ ProMax VIP V1

بوت التحليل اليومي

/analyze
للحصول على الإشارات
"""
)



async def analyze(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):


    msg = "⭐ ProMax VIP SIGNALS\n\n"


    for symbol in SYMBOLS:


        result = analyze_market(symbol)


        if result["score"] >= MIN_SIGNAL:


            trade = calculate_trade(
                result["signal"]
            )


            msg += f"""
📌 {symbol}

🟢 {result['signal']}

القوة:
{result['score']}%

Lot:
{LOT}

Entry:
{trade['entry']}

SL:
{trade['sl']}

TP1:
{trade['tp1']}

TP2:
{trade['tp2']}

التحليل:
"""


            for r in result["reasons"]:

                msg += f"✅ {r}\n"


            msg += "\n"



    await update.message.reply_text(
        msg
    )



# ==========================
# تشغيل البوت
# ==========================

def main():


    app = Application.builder().token(
        BOT_TOKEN
    ).build()


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
        "⭐ ProMax VIP Started"
    )


    app.run_polling()



if __name__ == "__main__":

    main()
