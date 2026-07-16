import time

from config import SYMBOLS, MIN_SIGNAL_SCORE

from market.data_feed import get_price_data

from analysis.indicators import add_indicators

from analysis.signal_engine import generate_signal

from risk.manager import calculate_levels

from telegram_bot.messages import signal_message


def analyze_symbol(symbol):

    print(f"🔍 تحليل: {symbol}")

    # جلب بيانات السوق
    df = get_price_data(
        symbol,
        "M5"
    )

    if df.empty:
        print("لا توجد بيانات")
        return None


    # إضافة المؤشرات
    df = add_indicators(df)


    last = df.iloc[-1]


    # تحليل المعطيات
    analysis_data = {

        "trend": last["EMA50"] > last["EMA200"],

        "direction":
            "UP"
            if last["EMA50"] > last["EMA200"]
            else "DOWN",

        "rsi":
            last["RSI"] > 50,

        "macd":
            last["MACD"] > last["MACD_SIGNAL"],

        "support": True,

        "candle":
            last["close"] > last["open"]

    }


    # حساب الإشارة
    result = generate_signal(
        analysis_data
    )


    if result["score"] < MIN_SIGNAL_SCORE:

        return None


    # حساب المستويات
    levels = calculate_levels(

        entry=last["close"],

        atr=last["ATR"],

        signal=result["signal"]

    )


    return {
        "symbol": symbol,
        "result": result,
        "levels": levels
    }



def run_engine():

    print(
        "⭐ ProMax VIP Engine Started"
    )


    while True:

        for symbol in SYMBOLS:

            try:

                signal = analyze_symbol(symbol)


                if signal:

                    print(
                        signal
                    )

                    # هنا يتم إرسال Telegram
                    # send_message()


            except Exception as error:

                print(
                    "Error:",
                    error
                )


        # إعادة الفحص كل 5 دقائق
        time.sleep(300)



if __name__ == "__main__":

    run_engine()
