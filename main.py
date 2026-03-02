import os
import time
import math
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# =====================
# RAILWAY ENV VARS
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "120"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

# Risk management (Portfolio $100 default)
ACCOUNT_USD = float(os.getenv("ACCOUNT_USD", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.03"))  # 3%
MIN_SCORE = int(os.getenv("MIN_SCORE", "8"))     # قوة الإشارة 8/10

# SL buffer using ATR
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.25"))

# Fallback RR (if not enough S/R levels)
RR_TP = [1.0, 2.0, 3.0]

# how many SR levels to keep
SR_LEVELS = int(os.getenv("SR_LEVELS", "6"))

# Dedup + cooldown
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# Optional offsets to better match broker quotes
OFFSETS = {
    "XAU": float(os.getenv("XAU_OFFSET", "0")),
    "BTC": float(os.getenv("BTC_OFFSET", "0")),
    "US100": float(os.getenv("US100_OFFSET", "0")),
    "US30": float(os.getenv("US30_OFFSET", "0")),
    "OIL": float(os.getenv("OIL_OFFSET", "0")),
}

# Max SL distance filter (important for $100 account)
MAX_SL_DISTANCE = {
    "XAU": float(os.getenv("XAU_MAX_SL", "12")),
    "BTC": float(os.getenv("BTC_MAX_SL", "800")),
    "US100": float(os.getenv("US100_MAX_SL", "150")),
    "US30": float(os.getenv("US30_MAX_SL", "300")),
    "OIL": float(os.getenv("OIL_MAX_SL", "2.5")),
}

# Default Yahoo symbols (can change via Variables)
SYMBOLS = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),      # Gold futures
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),  # Nasdaq futures
    "US30": os.getenv("US30_SYMBOL", "^DJI"),    # Dow Jones
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),      # Crude oil futures
}

# Period/interval per timeframe
TF_MAP = {
    "D1": ("120d", "1d"),
    "H4": ("60d", "4h"),
    "M30": ("14d", "30m"),
    "M15": ("14d", "15m"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =====================
# TELEGRAM
# =====================
def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.warning("Missing BOT_TOKEN or CHAT_ID. Set them in Railway Variables.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


# =====================
# DATA
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
            raise ValueError(f"Missing column {c} for {symbol}")
    return df


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> float:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    val = tr.rolling(n).mean().iloc[-1]
    if val is None or math.isnan(val):
        return float(tr.iloc[-1])
    return float(val)


# =====================
# TREND (D1 + H4)
# =====================
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


# =====================
# SUPPORT/RESISTANCE (Pivot)
# =====================
def pivots_sr(df: pd.DataFrame, left: int = 3, right: int = 3, max_levels: int = 6):
    h = df["High"].values
    l = df["Low"].values
    idx = df.index

    piv_high = []
    piv_low = []

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


# =====================
# FVG (simple ICT 3-candle gap)
# =====================
def find_fvg(df: pd.DataFrame, max_zones: int = 2):
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


# =====================
# ORDER BLOCK (heuristic)
# =====================
def find_order_block(df: pd.DataFrame, direction: str):
    w = df.tail(60).copy()
    w["body"] = (w["Close"] - w["Open"]).abs()
    w["range"] = (w["High"] - w["Low"]).replace(0, np.nan)
    w["body_ratio"] = (w["body"] / w["range"]).fillna(0)

    body_med = float(np.median(w["body"].values)) if len(w) else 0.0

    def is_strong_impulse(row) -> bool:
        return (row["body"] > 1.5 * body_med) and (row["body_ratio"] > 0.55)

    for i in range(len(w) - 2, 2, -1):
        row = w.iloc[i]
        if not is_strong_impulse(row):
            continue

        impulse_up = row["Close"] > row["Open"]
        impulse_down = row["Close"] < row["Open"]

        if direction == "BUY" and impulse_up:
            for j in range(i - 1, max(i - 15, 0), -1):
                r = w.iloc[j]
                if r["Close"] < r["Open"]:
                    return (w.index[j], float(r["Low"]), float(r["High"]))

        if direction == "SELL" and impulse_down:
            for j in range(i - 1, max(i - 15, 0), -1):
                r = w.iloc[j]
                if r["Close"] > r["Open"]:
                    return (w.index[j], float(r["Low"]), float(r["High"]))

    return None


# =====================
# SL / TP selection
# =====================
def pick_sl(direction: str, entry: float, ob, supports: list, resistances: list, atr_val: float, sl_buffer_atr: float):
    if direction == "BUY":
        if ob:
            sl_base = ob[1]  # OB low
        else:
            sl_base = max([s for s in supports if s < entry], default=entry - atr_val)
        return sl_base - (atr_val * sl_buffer_atr)

    if ob:
        sl_base = ob[2]  # OB high
    else:
        sl_base = min([r for r in resistances if r > entry], default=entry + atr_val)
    return sl_base + (atr_val * sl_buffer_atr)


def pick_tp_levels(direction: str, entry: float, supports: list, resistances: list, n: int = 3):
    if direction == "BUY":
        candidates = sorted([r for r in resistances if r > entry])
        return candidates[:n]
    else:
        candidates = sorted([s for s in supports if s < entry], reverse=True)
        return candidates[:n]


# =====================
# SCORE (Confluence)
# =====================
def in_zone(price, low, high):
    return low <= price <= high

def calc_score(direction, price, supports, resistances, ob, fvgs, td, th, atr_val):
    score = 0

    # (1) Strong trend D1+H4 = 2 points
    if (td + th) in (-2, 2):
        score += 2

    # (2) Near SR = 2 points
    nearest_sup = max([s for s in supports if s < price], default=None)
    nearest_res = min([r for r in resistances if r > price], default=None)

    near_sr = False
    if direction == "BUY" and nearest_sup is not None:
        if abs(price - nearest_sup) <= (0.8 * atr_val):
            near_sr = True
    if direction == "SELL" and nearest_res is not None:
        if abs(nearest_res - price) <= (0.8 * atr_val):
            near_sr = True
    if near_sr:
        score += 2

    # (3) In Order Block = 2 points
    if ob:
        _, ob_low, ob_high = ob
        if in_zone(price, ob_low, ob_high):
            score += 2

    # (4) In FVG = 2 points
    if fvgs:
        for t, _, a1, a2 in fvgs:
            low, high = min(a1, a2), max(a1, a2)
            if in_zone(price, low, high):
                score += 2
                break

    # (5) Volatility placeholder = 2 points
    score += 2

    return score


# =====================
# BUILD SIGNAL (per asset)
# =====================
def build_signal(symbol_key: str):
    sym = SYMBOLS[symbol_key]
    offset = OFFSETS.get(symbol_key, 0.0)

    d1 = fetch_ohlc(sym, *TF_MAP["D1"])
    h4 = fetch_ohlc(sym, *TF_MAP["H4"])
    m30 = fetch_ohlc(sym, *TF_MAP["M30"])
    m15 = fetch_ohlc(sym, *TF_MAP["M15"])

    for df in [d1, h4, m30, m15]:
        df["Open"] = df["Open"] + offset
        df["High"] = df["High"] + offset
        df["Low"] = df["Low"] + offset
        df["Close"] = df["Close"] + offset

    td = trend_score(d1)
    th = trend_score(h4)
    overall = td + th

    # Only strong trend (±2)
    if overall not in (-2, 2):
        return None

    direction = "BUY" if overall > 0 else "SELL"

    supports, resistances = pivots_sr(m30, left=3, right=3, max_levels=SR_LEVELS)
    fvgs = find_fvg(m15, max_zones=2)
    ob = find_order_block(m15, direction=direction)

    price = float(m15["Close"].iloc[-1])
    a = atr(m15, 14)

    # Score filter
    score = calc_score(direction, price, supports, resistances, ob, fvgs, td, th, a)
    if score < MIN_SCORE:
        return None

    # Entry (prefer OB mid, else FVG mid, else market)
    entry = price
    entry_reason = "Market"
    buy_limit = None
    sell_limit = None

    if direction == "BUY":
        if ob:
            _, ob_low, ob_high = ob
            entry = (ob_low + ob_high) / 2.0
            entry_reason = "OrderBlock(mid)"
            buy_limit = entry
        else:
            bull = [z for z in fvgs if z[0] == "BULL_FVG"]
            if bull:
                _, _, z1, z2 = bull[-1]
                entry = (z1 + z2) / 2.0
                entry_reason = "FVG(mid)"
                buy_limit = entry
    else:
        if ob:
            _, ob_low, ob_high = ob
            entry = (ob_low + ob_high) / 2.0
            entry_reason = "OrderBlock(mid)"
            sell_limit = entry
        else:
            bear = [z for z in fvgs if z[0] == "BEAR_FVG"]
            if bear:
                _, _, z1, z2 = bear[-1]
                entry = (z1 + z2) / 2.0
                entry_reason = "FVG(mid)"
                sell_limit = entry

    # SL (OB first else SR + ATR buffer)
    sl = pick_sl(direction, entry, ob, supports, resistances, a, SL_BUFFER_ATR)
    sl_distance = abs(entry - sl)

    # SL distance filter for small account
    if sl_distance > MAX_SL_DISTANCE.get(symbol_key, 999999):
        return None

    risk = max(0.0001, sl_distance)

    # TP from SR + fallback RR
    tps = pick_tp_levels(direction, entry, supports, resistances, n=3)
    if len(tps) < 3:
        if direction == "BUY":
            rr_fallback = [entry + rr * risk for rr in RR_TP]
        else:
            rr_fallback = [entry - rr * risk for rr in RR_TP]
        while len(tps) < 3:
            tps.append(rr_fallback[len(tps)])

    nearest_sup = max([s for s in supports if s < price], default=None)
    nearest_res = min([r for r in resistances if r > price], default=None)

    def r2(x): return round(float(x), 2)

    risk_usd = round(ACCOUNT_USD * RISK_PCT, 2)
    sig_word = "BUY" if direction == "BUY" else "SELL"
    emoji = "🔵" if direction == "BUY" else "🔴"

    msg = []
    msg.append(f"📌 {symbol_key} ({sym})")
    msg.append(f"Trend D1/H4: {td:+d} / {th:+d}  => Overall: {overall:+d}")
    msg.append("")
    msg.append(f"{emoji} {sig_word}: {r2(entry)}")
    msg.append(f"SL: {r2(sl)}")
    msg.append(f"TP1: {r2(tps[0])}")
    msg.append(f"TP2: {r2(tps[1])}")
    msg.append(f"TP3: {r2(tps[2])}")
    msg.append(f"Mode: {entry_reason}")
    msg.append("")
    msg.append(f"Risk: {int(RISK_PCT*100)}% (~${risk_usd})")
    msg.append(f"SL Distance: {r2(sl_distance)}")
    msg.append(f"Confidence: {score}/10")
    msg.append("")

    if buy_limit or sell_limit:
        msg.append("⏳ Pending Orders:")
        if buy_limit:
            msg.append(f"Buy Limit: {r2(buy_limit)}")
        if sell_limit:
            msg.append(f"Sell Limit: {r2(sell_limit)}")
        msg.append("")

    msg.append("📍 Levels:")
    if nearest_sup is not None:
        msg.append(f"Nearest Support: {r2(nearest_sup)}")
    if nearest_res is not None:
        msg.append(f"Nearest Resistance: {r2(nearest_res)}")
    msg.append("")

    if ob:
        ts, ob_low, ob_high = ob
        msg.append(f"🧱 Order Block: [{r2(ob_low)} - {r2(ob_high)}]")
    if fvgs:
        for t, _, a1, a2 in fvgs:
            msg.append(f"🕳️ {t}: [{r2(a1)} - {r2(a2)}]")

    return "\n".join(msg), (sig_word, r2(entry))


# =====================
# MAIN LOOP
# =====================
def main():
    last_sent = {}
    last_time = {}
    backoff = 1

    while True:
        try:
            now = time.time()

            for key in SYMBOLS.keys():
                result = build_signal(key)
                if not result:
                    continue

                msg, sig_key = result

                prev_t = last_time.get(key, 0)
                if (now - prev_t) < (COOLDOWN_MINUTES * 60):
                    continue

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
