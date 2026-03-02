import os
import time
import math
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# =====================
# ENV
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

ACCOUNT_USD = float(os.getenv("ACCOUNT_USD", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.03"))     # 3%
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))        # 7/10

SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))  # صغير لتقريب SL مثل 5$
SR_LEVELS = int(os.getenv("SR_LEVELS", "10"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# فلتر: لا ترسل pending إذا entry بعيد عن السعر الحالي
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.5"))

OFFSETS = {
    "XAU": float(os.getenv("XAU_OFFSET", "0")),
    "BTC": float(os.getenv("BTC_OFFSET", "0")),
    "US100": float(os.getenv("US100_OFFSET", "0")),
    "US30": float(os.getenv("US30_OFFSET", "0")),
    "OIL": float(os.getenv("OIL_OFFSET", "0")),
}

MAX_SL_DISTANCE = {
    "XAU": float(os.getenv("XAU_MAX_SL", "12")),
    "BTC": float(os.getenv("BTC_MAX_SL", "800")),
    "US100": float(os.getenv("US100_MAX_SL", "150")),
    "US30": float(os.getenv("US30_MAX_SL", "300")),
    "OIL": float(os.getenv("OIL_MAX_SL", "2.5")),
}

SYMBOLS = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30": os.getenv("US30_SYMBOL", "^DJI"),
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),
}

TF_MAP = {
    "D1": ("120d", "1d"),
    "H4": ("60d", "4h"),
    "M30": ("14d", "30m"),
    "M15": ("14d", "15m"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =====================
# Telegram
# =====================
def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.warning("Missing BOT_TOKEN or CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


# =====================
# Data + indicators
# =====================
def fetch_ohlc(symbol: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df is None or df.empty:
        raise ValueError(f"No data for {symbol} {period} {interval}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna()
    for c in ["Open", "High", "Low", "Close"]:
        if c not in df.columns:
            raise ValueError(f"Missing {c} in {symbol}")
    return df

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def atr(df: pd.DataFrame, n: int = 14) -> float:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    val = tr.rolling(n).mean().iloc[-1]
    return float(tr.iloc[-1]) if (val is None or math.isnan(val)) else float(val)

def trend_score(df: pd.DataFrame) -> int:
    c = df["Close"]
    e50 = ema(c, 50).iloc[-1]
    e200 = ema(c, 200).iloc[-1] if len(df) >= 200 else ema(c, 100).iloc[-1]
    last = c.iloc[-1]
    if last > e50 and last > e200:
        return +1
    if last < e50 and last < e200:
        return -1
    return 0

def pivots_sr(df: pd.DataFrame, left: int = 3, right: int = 3, max_levels: int = 10):
    h = df["High"].values
    l = df["Low"].values
    idx = df.index
    piv_high, piv_low = [], []
    start = max(left, 0)
    end = len(df) - right

    for i in range(start, end):
        if h[i] > np.max(h[i-left:i]) and h[i] > np.max(h[i+1:i+1+right]):
            piv_high.append((idx[i], float(h[i])))
        if l[i] < np.min(l[i-left:i]) and l[i] < np.min(l[i+1:i+1+right]):
            piv_low.append((idx[i], float(l[i])))

    resistances = sorted(list({round(p[1], 2) for p in piv_high}), reverse=True)[:max_levels]
    supports = sorted(list({round(p[1], 2) for p in piv_low}))[:max_levels]
    return supports, resistances

def find_fvg(df: pd.DataFrame, max_zones: int = 1):
    zones = []
    H = df["High"].values
    L = df["Low"].values
    idx = df.index
    for i in range(2, len(df)):
        c1 = i - 2
        c3 = i
        if H[c1] < L[c3]:
            zones.append(("BULL_FVG", idx[c3], float(H[c1]), float(L[c3])))
        if L[c1] > H[c3]:
            zones.append(("BEAR_FVG", idx[c3], float(H[c3]), float(L[c1])))
    return zones[-max_zones:]

def find_order_block(df: pd.DataFrame, direction: str):
    w = df.tail(60).copy()
    w["body"] = (w["Close"] - w["Open"]).abs()
    w["range"] = (w["High"] - w["Low"]).replace(0, np.nan)
    w["body_ratio"] = (w["body"] / w["range"]).fillna(0)
    body_med = float(np.median(w["body"].values)) if len(w) else 0.0

    def strong(row) -> bool:
        return (row["body"] > 1.5 * body_med) and (row["body_ratio"] > 0.55)

    for i in range(len(w) - 2, 2, -1):
        row = w.iloc[i]
        if not strong(row):
            continue

        impulse_up = row["Close"] > row["Open"]
        impulse_down = row["Close"] < row["Open"]

        if direction == "BUY" and impulse_up:
            for j in range(i - 1, max(i - 15, 0), -1):
                r = w.iloc[j]
                if r["Close"] < r["Open"]:
                    return ("BULL_OB", w.index[j], float(r["Low"]), float(r["High"]))
        if direction == "SELL" and impulse_down:
            for j in range(i - 1, max(i - 15, 0), -1):
                r = w.iloc[j]
                if r["Close"] > r["Open"]:
                    return ("BEAR_OB", w.index[j], float(r["Low"]), float(r["High"]))
    return None

def pick_tp_from_sr(direction: str, entry: float, supports: list, resistances: list, n: int = 3):
    if direction == "BUY":
        return sorted([r for r in resistances if r > entry])[:n]
    else:
        return sorted([s for s in supports if s < entry], reverse=True)[:n]

def calc_score(direction, price, supports, resistances, ob, fvgs, td, th, atr_val):
    score = 0
    if (td + th) in (-2, 2):
        score += 2

    nearest_sup = max([s for s in supports if s < price], default=None)
    nearest_res = min([r for r in resistances if r > price], default=None)

    near_sr = False
    if direction == "BUY" and nearest_sup is not None and abs(price - nearest_sup) <= 0.8 * atr_val:
        near_sr = True
    if direction == "SELL" and nearest_res is not None and abs(nearest_res - price) <= 0.8 * atr_val:
        near_sr = True
    if near_sr:
        score += 2

    in_ob = False
    if ob:
        _, _, lo, hi = ob
        if lo <= price <= hi:
            in_ob = True
            score += 2

    in_fvg = False
    if fvgs:
        for _, _, a1, a2 in fvgs:
            lo, hi = min(a1, a2), max(a1, a2)
            if lo <= price <= hi:
                in_fvg = True
                score += 2
                break

    # فلتر تذبذب (نعطي 2 نقاط دائمًا هنا، والفلترة الأساسية تتم عبر SL/Pending distance)
    score += 2
    return score, in_ob, in_fvg, nearest_sup, nearest_res


# =====================
# Build message
# =====================
def build_market_message(symbol_key, sym, direction, price, sl, tps, score, td, th):
    risk_usd = round(ACCOUNT_USD * RISK_PCT, 2)
    def r2(x): return round(float(x), 2)
    emoji = "🔵" if direction == "BUY" else "🔴"
    return (
        f"📌 {symbol_key} ({sym})\n"
        f"Trend D1/H4: {td:+d} / {th:+d}  => Overall: {(td+th):+d}\n\n"
        f"{emoji} {direction}: {r2(price)}\n"
        f"SL: {r2(sl)}\n"
        f"TP1: {r2(tps[0])}\n"
        f"TP2: {r2(tps[1])}\n"
        f"TP3: {r2(tps[2])}\n\n"
        f"Risk: {int(RISK_PCT*100)}% (~${risk_usd})\n"
        f"Confidence: {score}/10"
    )

def build_pending_message(symbol_key, sym, direction, entry, sl, tps, score, td, th, zone_name, zone_low, zone_high):
    risk_usd = round(ACCOUNT_USD * RISK_PCT, 2)
    def r2(x): return round(float(x), 2)
    emoji = "🟦" if direction == "BUY" else "🟥"
    order_name = "Buy Limit" if direction == "BUY" else "Sell Limit"
    return (
        f"📌 {symbol_key} ({sym})\n"
        f"Trend D1/H4: {td:+d} / {th:+d}  => Overall: {(td+th):+d}\n\n"
        f"{emoji} {order_name}: {r2(entry)}\n"
        f"SL: {r2(sl)}\n"
        f"TP1: {r2(tps[0])}\n"
        f"TP2: {r2(tps[1])}\n"
        f"TP3: {r2(tps[2])}\n\n"
        f"Zone: {zone_name} [{r2(zone_low)} - {r2(zone_high)}]\n"
        f"Risk: {int(RISK_PCT*100)}% (~${risk_usd})\n"
        f"Confidence: {score}/10"
    )


def build_signal(symbol_key: str):
    sym = SYMBOLS[symbol_key]
    offset = OFFSETS.get(symbol_key, 0.0)

    d1 = fetch_ohlc(sym, *TF_MAP["D1"])
    h4 = fetch_ohlc(sym, *TF_MAP["H4"])
    m30 = fetch_ohlc(sym, *TF_MAP["M30"])
    m15 = fetch_ohlc(sym, *TF_MAP["M15"])

    # Offset
    for df in (d1, h4, m30, m15):
        df["Open"] += offset
        df["High"] += offset
        df["Low"] += offset
        df["Close"] += offset

    td = trend_score(d1)
    th = trend_score(h4)
    overall = td + th

    # ✅ فلتر صارم: فقط ±2
    if overall not in (-2, 2):
        return None

    direction = "BUY" if overall > 0 else "SELL"

    supports, resistances = pivots_sr(m30, left=3, right=3, max_levels=SR_LEVELS)
    fvgs = find_fvg(m15, max_zones=1)
    ob = find_order_block(m15, direction=direction)

    price = float(m15["Close"].iloc[-1])
    a = atr(m15, 14)

    score, in_ob, in_fvg, nearest_sup, nearest_res = calc_score(direction, price, supports, resistances, ob, fvgs, td, th, a)
    if score < MIN_SCORE:
        return None

    def r2(x): return round(float(x), 2)

    # =====================
    # قرار: Market أو Pending فقط
    # =====================

    # 1) إذا عندنا Zone قوية (OB أو FVG) نرسل Pending لكن بشرط أن تكون قريبة من السعر
    zone = None
    if ob:
        zone = ("OrderBlock", ob[2], ob[3])  # (name, low, high)
    elif fvgs:
        t, _, a1, a2 = fvgs[-1]
        zone = ("FVG", min(a1, a2), max(a1, a2))

    if zone:
        zone_name, zlow, zhigh = zone
        entry = (zlow + zhigh) / 2.0
        distance = abs(entry - price)

        # ✅ فلتر مسافة (يمنع مشكلة النفط)
        if distance <= (MAX_PENDING_DISTANCE_ATR * a):
            # SL: خارج الزون مع buffer
            if direction == "BUY":
                sl = (zlow - a * SL_BUFFER_ATR)
            else:
                sl = (zhigh + a * SL_BUFFER_ATR)

            sl_distance = abs(entry - sl)
            if sl_distance > MAX_SL_DISTANCE.get(symbol_key, 999999):
                return None

            tps = pick_tp_from_sr(direction, entry, supports, resistances, n=3)
            if len(tps) < 3:
                # fallback بسيط
                risk = max(0.0001, sl_distance)
                if direction == "BUY":
                    tps = [entry + risk, entry + 2*risk, entry + 3*risk]
                else:
                    tps = [entry - risk, entry - 2*risk, entry - 3*risk]

            msg = build_pending_message(
                symbol_key, sym, direction,
                entry=entry, sl=sl, tps=tps,
                score=score, td=td, th=th,
                zone_name=zone_name, zone_low=zlow, zone_high=zhigh
            )
            key = (direction, r2(entry), "PENDING")
            return msg, key

    # 2) غير ذلك: Market signal عند السعر الحالي
    entry = price
    if direction == "BUY":
        base = nearest_sup if nearest_sup is not None else (entry - a)
        sl = base - a * SL_BUFFER_ATR
    else:
        base = nearest_res if nearest_res is not None else (entry + a)
        sl = base + a * SL_BUFFER_ATR

    sl_distance = abs(entry - sl)
    if sl_distance > MAX_SL_DISTANCE.get(symbol_key, 999999):
        return None

    tps = pick_tp_from_sr(direction, entry, supports, resistances, n=3)
    if len(tps) < 3:
        risk = max(0.0001, sl_distance)
        if direction == "BUY":
            tps = [entry + risk, entry + 2*risk, entry + 3*risk]
        else:
            tps = [entry - risk, entry - 2*risk, entry - 3*risk]

    msg = build_market_message(symbol_key, sym, direction, price=entry, sl=sl, tps=tps, score=score, td=td, th=th)
    key = (direction, r2(entry), "MARKET")
    return msg, key


def main():
    last_sent = {}
    last_time = {}
    backoff = 1

    while True:
        try:
            now = time.time()

            for key in SYMBOLS.keys():
                res = build_signal(key)
                if not res:
                    continue

                msg, sig_key = res

                # cooldown
                prev_t = last_time.get(key, 0)
                if (now - prev_t) < (COOLDOWN_MINUTES * 60):
                    continue

                # dedupe
                if last_sent.get(key) == sig_key:
                    continue

                if send_telegram(msg):
                    last_sent[key] = sig_key
                    last_time[key] = now

            backoff = 1
            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            logging.error("Loop error: %s", e)
            time.sleep(min(60, backoff))
            backoff = min(300, backoff * 2)


if __name__ == "__main__":
    main()
