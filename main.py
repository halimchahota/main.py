import os
import time
import math
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# =====================
# CONFIG
# =====================
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Optional: if you want to "shift" a symbol price to match broker (XM etc.)
# Example: GOLD_OFFSET = -10.5  (add to fetched price)
OFFSETS = {
    "XAU": float(os.getenv("XAU_OFFSET", "0")),
    "BTC": float(os.getenv("BTC_OFFSET", "0")),
    "US100": float(os.getenv("US100_OFFSET", "0")),
    "US30": float(os.getenv("US30_OFFSET", "0")),
    "OIL": float(os.getenv("OIL_OFFSET", "0")),
}

# Symbols (can change later)
SYMBOLS = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),     # Gold futures (stable for OHLC)
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"), # Nasdaq futures
    "US30": os.getenv("US30_SYMBOL", "^DJI"),   # Dow Jones
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),     # Crude oil futures
}

# Timeframes
TF_MAP = {
    "D1": ("60d", "1d"),
    "H4": ("30d", "4h"),
    "M30": ("7d", "30m"),
    "M15": ("7d", "15m"),
}

# Risk / targets defaults
RR_TP = [1.0, 2.0, 3.0]  # TP1/TP2/TP3 multiples of risk
SL_BUFFER_ATR = 0.25     # SL buffer as fraction of ATR
ENTRY_MODE = os.getenv("ENTRY_MODE", "market")  # market or limit (we send both anyway)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =====================
# TELEGRAM
# =====================
def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.warning("BOT_TOKEN or CHAT_ID missing. Skipping telegram send.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram send error: %s", e)
        return False


# =====================
# DATA
# =====================
def fetch_ohlc(symbol: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df is None or df.empty:
        raise ValueError(f"No data for {symbol} {period} {interval}")
    # Normalize columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna()
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
    return float(val) if not math.isnan(val) else float(tr.iloc[-1])


# =====================
# S/R (Pivot)
# =====================
def pivots_sr(df: pd.DataFrame, lookback: int = 50, left: int = 3, right: int = 3):
    """
    Basic pivot highs/lows to approximate support/resistance.
    Returns last few supports and resistances.
    """
    h = df["High"].values
    l = df["Low"].values
    idx = df.index

    piv_high = []
    piv_low = []

    start = max(left, 0)
    end = len(df) - right
    for i in range(start, end):
        win_left_h = h[i-left:i]
        win_right_h = h[i+1:i+1+right]
        if h[i] > np.max(win_left_h) and h[i] > np.max(win_right_h):
            piv_high.append((idx[i], float(h[i])))

        win_left_l = l[i-left:i]
        win_right_l = l[i+1:i+1+right]
        if l[i] < np.min(win_left_l) and l[i] < np.min(win_right_l):
            piv_low.append((idx[i], float(l[i])))

    # Keep recent
    piv_high = piv_high[-10:]
    piv_low = piv_low[-10:]

    # Filter to last lookback window only
    cutoff = df.index[-lookback] if len(df) > lookback else df.index[0]
    piv_high = [p for p in piv_high if p[0] >= cutoff]
    piv_low = [p for p in piv_low if p[0] >= cutoff]

    resistances = sorted(list({round(p[1], 2) for p in piv_high}), reverse=True)[:3]
    supports = sorted(list({round(p[1], 2) for p in piv_low}))[:3]
    return supports, resistances


# =====================
# FVG (ICT-ish)
# =====================
def find_fvg(df: pd.DataFrame, max_zones: int = 2):
    """
    Simple 3-candle FVG:
    Bullish FVG if candle1 high < candle3 low  => gap [candle1 high, candle3 low]
    Bearish FVG if candle1 low  > candle3 high => gap [candle3 high, candle1 low]
    Returns recent zones.
    """
    zones = []
    H = df["High"].values
    L = df["Low"].values
    idx = df.index

    for i in range(2, len(df)):
        c1 = i - 2
        c3 = i
        # Bullish
        if H[c1] < L[c3]:
            zones.append(("BULL_FVG", idx[c3], float(H[c1]), float(L[c3])))
        # Bearish
        if L[c1] > H[c3]:
            zones.append(("BEAR_FVG", idx[c3], float(H[c3]), float(L[c1])))

    zones = zones[-10:]  # last
    # Keep only most recent per type
    out = []
    for t in ["BULL_FVG", "BEAR_FVG"]:
        zt = [z for z in zones if z[0] == t]
        out.extend(zt[-max_zones:])
    return out[-max_zones:]


# =====================
# Order Block (simple heuristic)
# =====================
def find_order_block(df: pd.DataFrame, direction: str):
    """
    Heuristic:
    - For BUY: find last bearish candle before a strong bullish impulse (big body) in last ~30 bars
    - For SELL: find last bullish candle before a strong bearish impulse
    Returns OB zone (low/high) and timestamp.
    """
    df = df.copy()
    df["body"] = (df["Close"] - df["Open"]).abs()
    df["range"] = (df["High"] - df["Low"]).replace(0, np.nan)
    df["body_ratio"] = (df["body"] / df["range"]).fillna(0)

    window = df.tail(40)
    bodies = window["body"].values
    body_med = np.median(bodies) if len(bodies) else 0

    # Strong impulse = body > 1.5 * median and body_ratio > 0.55
    for i in range(len(window)-1, 2, -1):
        row = window.iloc[i]
        if row["body"] > 1.5 * body_med and row["body_ratio"] > 0.55:
            # impulse candle direction
            impulse_up = row["Close"] > row["Open"]
            impulse_down = row["Close"] < row["Open"]

            if direction == "BUY" and impulse_up:
                # search backwards for last bearish candle
                for j in range(i-1, max(i-12, 0), -1):
                    r = window.iloc[j]
                    if r["Close"] < r["Open"]:
                        return (window.index[j], float(r["Low"]), float(r["High"]))
            if direction == "SELL" and impulse_down:
                # search backwards for last bullish candle
                for j in range(i-1, max(i-12, 0), -1):
                    r = window.iloc[j]
                    if r["Close"] > r["Open"]:
                        return (window.index[j], float(r["Low"]), float(r["High"]))

    return None


# =====================
# Trend (Daily + H4)
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
# Signal builder
# =====================
def build_signal(symbol_key: str):
    sym = SYMBOLS[symbol_key]
    offset = OFFSETS.get(symbol_key, 0.0)

    d1 = fetch_ohlc(sym, *TF_MAP["D1"])
    h4 = fetch_ohlc(sym, *TF_MAP["H4"])
    m30 = fetch_ohlc(sym, *TF_MAP["M30"])
    m15 = fetch_ohlc(sym, *TF_MAP["M15"])

    # Apply offsets (for display only)
    for df in [d1, h4, m30, m15]:
        df["Open"] = df["Open"] + offset
        df["High"] = df["High"] + offset
        df["Low"] = df["Low"] + offset
        df["Close"] = df["Close"] + offset

    td = trend_score(d1)
    th = trend_score(h4)
    overall = td + th  # -2..+2

    # Only trade if strong
    if overall == 0:
        return None  # neutral

    direction = "BUY" if overall > 0 else "SELL"

    # Levels from M30 (more stable)
    supports, resistances = pivots_sr(m30, lookback=80, left=3, right=3)

    # FVG from M15 (entry precision)
    fvgs = find_fvg(m15, max_zones=2)

    # OB from M15 based on direction
    ob = find_order_block(m15, direction=direction)

    last_price = float(m15["Close"].iloc[-1])
    current_atr = atr(m15, 14)

    # Entry logic:
    # - If we have OB, prefer limit entry at mid of OB
    # - Else if FVG exists, limit at mid of latest FVG
    # - Else market
    entry = last_price
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

    # SL/TP
    if direction == "BUY":
        sl_base = (ob[1] if ob else (min(supports) if supports else last_price - current_atr))
        sl = sl_base - (current_atr * SL_BUFFER_ATR)
        risk = max(0.0001, entry - sl)
        tps = [entry + rr * risk for rr in RR_TP]
    else:
        sl_base = (ob[2] if ob else (max(resistances) if resistances else last_price + current_atr))
        sl = sl_base + (current_atr * SL_BUFFER_ATR)
        risk = max(0.0001, sl - entry)
        tps = [entry - rr * risk for rr in RR_TP]

    # Round display (2 decimals default)
    def r2(x): return round(float(x), 2)

    # Pick nearest S/R for info
    sr_info = ""
    if supports:
        sr_info += f"Supports: {', '.join(map(lambda x: str(r2(x)), supports))}\n"
    if resistances:
        sr_info += f"Resistances: {', '.join(map(lambda x: str(r2(x)), resistances))}\n"

    fvg_info = ""
    if fvgs:
        lines = []
        for t, ts, a, b in fvgs[-2:]:
            lines.append(f"{t}: [{r2(a)} - {r2(b)}]")
        fvg_info = "FVG:\n" + "\n".join(lines) + "\n"

    ob_info = ""
    if ob:
        ts, ob_low, ob_high = ob
        ob_info = f"OrderBlock: [{r2(ob_low)} - {r2(ob_high)}]\n"

    # Pending orders suggestion:
    pending_info = ""
    if buy_limit:
        pending_info += f"Buy Limit: {r2(buy_limit)}\n"
    if sell_limit:
        pending_info += f"Sell Limit: {r2(sell_limit)}\n"

    # Compose message
    msg = []
    msg.append(f"📌 {symbol_key} ({sym})")
    msg.append(f"Trend D1+H4: {td:+d} / {th:+d}  => Overall: {overall:+d}")
    msg.append(f"Signal: {'🔵 BUY' if direction=='BUY' else '🔴 SELL'}")
    msg.append(f"Price: {r2(last_price)}")
    msg.append("")
    msg.append(f"Entry ({entry_reason}): {r2(entry)}")
    msg.append(f"SL: {r2(sl)}")
    msg.append(f"TP1: {r2(tps[0])}  | TP2: {r2(tps[1])}  | TP3: {r2(tps[2])}")
    msg.append("")
    if pending_info:
        msg.append("Pending Orders:")
        msg.append(pending_info.strip())
        msg.append("")
    if sr_info:
        msg.append(sr_info.strip())
        msg.append("")
    if ob_info:
        msg.append(ob_info.strip())
    if fvg_info:
        msg.append(fvg_info.strip())

    return "\n".join(msg)


# =====================
# MAIN LOOP
# =====================
def main():
    last_sent = {}  # symbol_key -> ("BUY"/"SELL", rounded_entry)
    backoff = 1

    while True:
        try:
            for k in SYMBOLS.keys():
                sig = build_signal(k)
                if not sig:
                    continue

                # de-duplicate: hash by first lines (signal + entry)
                lines = sig.splitlines()
                direction_line = [l for l in lines if l.startswith("Signal:")]
                entry_line = [l for l in lines if l.startswith("Entry")]
                key = (direction_line[0] if direction_line else "", entry_line[0] if entry_line else "")

                if last_sent.get(k) != key:
                    send_telegram(sig)
                    last_sent[k] = key

            backoff = 1
            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            logging.error("Loop error: %s", e)
            time.sleep(min(60, backoff))
            backoff = min(300, backoff * 2)


if __name__ == "__main__":
    main()
