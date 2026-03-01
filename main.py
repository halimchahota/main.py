import os, requests
from flask import Flask, request, jsonify

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)

def ema(values, period=50):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def trend_from_ema(candles, period=50):
    closes = [x["c"] for x in candles]
    e = ema(closes[-period*2:], period=period)  # آخر جزء للحساب
    if e is None:
        return "NEUTRAL"
    last = closes[-1]
    if last > e:
        return "BULL"
    if last < e:
        return "BEAR"
    return "NEUTRAL"

def swing_levels(candles, left=2, right=2, max_levels=6):
    """دعم/مقاومة من قمم/قيعان محلية"""
    highs = []
    lows = []
    n = len(candles)
    for i in range(left, n-right):
        h = candles[i]["h"]
        l = candles[i]["l"]
        is_high = all(h > candles[i-j]["h"] for j in range(1, left+1)) and all(h > candles[i+j]["h"] for j in range(1, right+1))
        is_low  = all(l < candles[i-j]["l"] for j in range(1, left+1)) and all(l < candles[i+j]["l"] for j in range(1, right+1))
        if is_high:
            highs.append(h)
        if is_low:
            lows.append(l)

    # تنقية بسيطة: مستويات فريدة تقريبًا
    def unique_sorted(vals, eps=0.0001):
        vals = sorted(vals)
        out = []
        for v in vals:
            if not out or abs(v - out[-1]) > eps * max(1.0, abs(v)):
                out.append(v)
        return out

    res = unique_sorted(highs)[-max_levels:]
    sup = unique_sorted(lows)[:max_levels]
    return sup, res

def detect_fvg(candles):
    """
    FVG ثلاث شمعات (ICT style):
    Bullish FVG: low[2] > high[0]  => gap بين high[0] و low[2]
    Bearish FVG: high[2] < low[0]  => gap بين high[2] و low[0]
    نرجع آخر فجوة فقط.
    """
    if len(candles) < 3:
        return None

    last = None
    for i in range(2, len(candles)):
        c0 = candles[i-2]
        c2 = candles[i]
        if c2["l"] > c0["h"]:
            last = {"type": "BULL_FVG", "low": c0["h"], "high": c2["l"], "t": c2["t"]}
        elif c2["h"] < c0["l"]:
            last = {"type": "BEAR_FVG", "low": c2["h"], "high": c0["l"], "t": c2["t"]}
    return last

def detect_order_block(candles, lookback=60):
    """
    تبسيط عملي:
    Bullish OB: آخر شمعة هابطة قبل اندفاع صاعد قوي (close أعلى من high الشمعة الهابطة)
    Bearish OB: آخر شمعة صاعدة قبل اندفاع هابط قوي (close أدنى من low الشمعة الصاعدة)
    نرجع آخر OB فقط.
    """
    if len(candles) < 5:
        return None

    start = max(0, len(candles)-lookback)
    last = None
    for i in range(start+1, len(candles)-1):
        prev = candles[i]
        nxt = candles[i+1]

        # Bullish OB
        if prev["c"] < prev["o"] and nxt["c"] > prev["h"]:
            last = {"type": "BULL_OB", "low": prev["l"], "high": prev["h"], "t": prev["t"]}

        # Bearish OB
        if prev["c"] > prev["o"] and nxt["c"] < prev["l"]:
            last = {"type": "BEAR_OB", "low": prev["l"], "high": prev["h"], "t": prev["t"]}

    return last

def build_trade_plan(symbol, d1, h4, m30, m15):
    # اتجاه عام: D1 + H4
    tr_d1 = trend_from_ema(d1, 50)
    tr_h4 = trend_from_ema(h4, 50)

    # دخول أدق: M30 + M15
    tr_m30 = trend_from_ema(m30, 50)
    tr_m15 = trend_from_ema(m15, 50)

    price = m15[-1]["c"]

    # دعم/مقاومة (من H4 أفضل)
    sup, res = swing_levels(h4, left=2, right=2, max_levels=5)
    nearest_sup = max([s for s in sup if s < price], default=None)
    nearest_res = min([r for r in res if r > price], default=None)

    fvg = detect_fvg(m15)
    ob  = detect_order_block(m15)

    # قرار بسيط (قابل للتطوير):
    bias = "NEUTRAL"
    if tr_d1 == "BULL" and tr_h4 == "BULL":
        bias = "BUY"
    elif tr_d1 == "BEAR" and tr_h4 == "BEAR":
        bias = "SELL"

    # Entry/SL/TP:
    entry = round(price, 2)

    # لو عندك دعم/مقاومة استعملها في SL/TP
    if bias == "BUY":
        sl = round((nearest_sup if nearest_sup else price - 3), 2)
        tp1 = round((nearest_res if nearest_res else price + 10), 2)
        tp2 = round(tp1 + (tp1-entry)*0.8, 2)
        tp3 = round(tp1 + (tp1-entry)*1.3, 2)
        buy_limit  = round((ob["low"] if ob and ob["type"] == "BULL_OB" else entry - 2), 2)
        sell_limit = round(entry + 15, 2)
    elif bias == "SELL":
        sl = round((nearest_res if nearest_res else price + 3), 2)
        tp1 = round((nearest_sup if nearest_sup else price - 10), 2)
        tp2 = round(tp1 - (entry-tp1)*0.8, 2)
        tp3 = round(tp1 - (entry-tp1)*1.3, 2)
        sell_limit = round((ob["high"] if ob and ob["type"] == "BEAR_OB" else entry + 2), 2)
        buy_limit  = round(entry - 15, 2)
    else:
        return None

    return {
        "bias": bias,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "buy_limit": buy_limit,
        "sell_limit": sell_limit,
        "trend": {"D1": tr_d1, "H4": tr_h4, "M30": tr_m30, "M15": tr_m15},
        "sr": {"support": sup, "resistance": res, "nearest_sup": nearest_sup, "nearest_res": nearest_res},
        "fvg": fvg,
        "ob": ob
    }

def format_message(symbol, plan):
    t = plan["trend"]
    sr = plan["sr"]
    fvg = plan["fvg"]
    ob = plan["ob"]

    lines = []
    emoji = "🔵" if plan["bias"] == "BUY" else "🔴"
    lines.append(f"{emoji} {plan['bias']} {symbol}")
    lines.append("")
    lines.append(f"Entry: {plan['entry']}")
    lines.append(f"SL: {plan['sl']}")
    lines.append(f"TP1: {plan['tp1']}")
    lines.append(f"TP2: {plan['tp2']}")
    lines.append(f"TP3: {plan['tp3']}")
    lines.append("")
    lines.append("Pending Orders:")
    lines.append(f"Buy Limit: {plan['buy_limit']}")
    lines.append(f"Sell Limit: {plan['sell_limit']}")
    lines.append("")
    lines.append(f"Trend D1/H4/M30/M15: {t['D1']} | {t['H4']} | {t['M30']} | {t['M15']}")
    lines.append(f"Nearest Support: {sr['nearest_sup']}")
    lines.append(f"Nearest Resistance: {sr['nearest_res']}")

    if fvg:
        lines.append(f"FVG: {fvg['type']} zone [{round(fvg['low'],2)} - {round(fvg['high'],2)}]")
    if ob:
        lines.append(f"Order Block: {ob['type']} zone [{round(ob['low'],2)} - {round(ob['high'],2)}]")

    return "\n".join(lines)

@app.route("/candles", methods=["POST"])
def candles():
    data = request.json
    symbols = data.get("symbols", {})

    for sym, tfs in symbols.items():
        d1 = tfs.get("D1")
        h4 = tfs.get("H4")
        m30 = tfs.get("M30")
        m15 = tfs.get("M15")

        if not all([d1, h4, m30, m15]):
            continue

        plan = build_trade_plan(sym, d1, h4, m30, m15)
        if plan:
            msg = format_message(sym, plan)
            send_telegram(msg)

    return jsonify({"status": "ok"})

@app.route("/")
def home():
    return "SMART BOT RUNNING"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
