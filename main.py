import
import time
import math
import hashlib
import logging
from typing import Dict, Optional, Tuple, List

import numpy as np
import pandas as pd
import requests
import yfinance as yf


# =========================
# ENV (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

ACCOUNT_USD = float(os.getenv("ACCOUNT_USD", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

# Strictness
BOS_LOOKBACK = int(os.getenv("BOS_LOOKBACK", "10"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))            # 0.6% ATR%
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.5"))

# Market touch rule:
TOUCH_ATR_MULT = float(os.getenv("TOUCH_ATR_MULT", "0.35"))        # لمس المنطقة إذا قرب <= 0.35 ATR

# Cache controls
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

SYMBOLS: Dict[str, str] = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30": os.getenv("US30_SYMBOL", "^DJI"),
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),
}

PERIOD_D1 = os.getenv("PERIOD_D1", "200d")
PERIOD_H4 = os.getenv("PERIOD_H4", "90d")
PERIOD_M30 = os.getenv("PERIOD_M30", "30d")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =========================
# Telegram
# =========================
def tg_send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing BOT_TOKEN or CHAT_ID in env.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


# =========================
# Data fetch (reliable H4)
# =========================
def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    return df

def fetch_ohlc(symbol: str, interval: str, period: str) -> pd.DataFrame:
    if interval == "4h":
        df = yf.download(symbol, period=period, interval="1h", progress=False, auto_adjust=False)
        if df is None or df.empty:
            raise RuntimeError(f"No data for {symbol} interval=1h period={period}")
        df = _normalize_columns(df).dropna()

        df_4h = pd.DataFrame()
        df_4h["Open"] = df["Open"].resample("4H").first()
        df_4h["High"] = df["High"].resample("4H").max()
        df_4h["Low"] = df["Low"].resample("4H").min()
        df_4h["Close"] = df["Close"].resample("4H").last()
        df_4h = df_4h.dropna()
        return df_4h

    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {symbol} interval={interval} period={period}")
    df = _normalize_columns(df).dropna()
    return df


# =========================
# Indicators
# =========================
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
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return float(tr.iloc[-1])
    return float(val)

def trend_score(df: pd.DataFrame) -> int:
    if df is None or df.empty or len(df) < 80:
        return 0
    c = df["Close"]
    e50 = ema(c, 50)
    e200 = ema(c, 200) if len(df) >= 200 else ema(c, 100)

    slope = float(e50.iloc[-1] - e50.iloc[-10]) if len(e50) >= 11 else 0.0
    if (c.iloc[-1] > e50.iloc[-1] > e200.iloc[-1]) and slope > 0:
        return +1
    if (c.iloc[-1] < e50.iloc[-1] < e200.iloc[-1]) and slope < 0:
        return -1
    return 0

def atr_ok_m30(m30: pd.DataFrame) -> bool:
    if m30 is None or m30.empty or len(m30) < 30:
        return True
    a = atr(m30, 14)
    price = float(m30["Close"].iloc[-1])
    if price <= 0:
        return True
    return (a / price) <= MAX_ATR_PCT

def candle_confirm_m30(m30: pd.DataFrame, direction: str) -> bool:
    o = float(m30["Open"].iloc[-1])
    c = float(m30["Close"].iloc[-1])
    return (c > o) if direction == "BUY" else (c < o)

def bos_m30(m30: pd.DataFrame, direction: str) -> bool:
    if m30 is None or m30.empty or len(m30) < BOS_LOOKBACK + 2:
        return False
    d = m30.tail(BOS_LOOKBACK + 2)
    last_close = float(d["Close"].iloc[-1])
    prev_high = float(d["High"].iloc[:-1].max())
    prev_low = float(d["Low"].iloc[:-1].min())
    return (last_close > prev_high) if direction == "BUY" else (last_close < prev_low)


# =========================
# Zones: Order Block + FVG (M30)
# =========================
def find_order_block_m30(m30: pd.DataFrame, direction: str) -> Optional[Tuple[float, float]]:
    if m30 is None or m30.empty or len(m30) < 50:
        return None

    w = m30.tail(140).copy()
    bodies = (w["Close"] - w["Open"]).abs()
    thr = float(bodies.quantile(0.75))

    for i in range(len(w) - 3, 10, -1):
        o = float(w["Open"].iloc[i]); c = float(w["Close"].iloc[i])
        lo = float(w["Low"].iloc[i]); hi = float(w["High"].iloc[i])

        next_body = abs(float(w["Close"].iloc[i+1]) - float(w["Open"].iloc[i+1]))
        if next_body < thr:
            continue

        if direction == "BUY":
            if c < o and float(w["Close"].iloc[i+1]) > float(w["Open"].iloc[i+1]):
                return (min(lo, hi), max(lo, hi))

        if direction == "SELL":
            if c > o and float(w["Close"].iloc[i+1]) < float(w["Open"].iloc[i+1]):
                return (min(lo, hi), max(lo, hi))

    return None

def find_fvg_m30(m30: pd.DataFrame, direction: str, max_items: int = 2) -> List[Tuple[float, float]]:
    if m30 is None or m30.empty or len(m30) < 10:
        return []

    d = m30.tail(400)
    zones: List[Tuple[float, float]] = []
    for i in range(2, len(d)):
        h2 = float(d["High"].iloc[i - 2])
        l2 = float(d["Low"].iloc[i - 2])
        hi = float(d["High"].iloc[i])
        lo = float(d["Low"].iloc[i])

        if direction == "BUY" and lo > h2:
            zones.append((min(h2, lo), max(h2, lo)))

        if direction == "SELL" and hi < l2:
            zones.append((min(hi, l2), max(hi, l2)))

    return zones[-max_items:]

def zone_mid(zone: Tuple[float, float]) -> float:
    return (zone[0] + zone[1]) / 2.0

def price_touches_zone(price: float, zone: Tuple[float, float], a30: float) -> bool:
    # داخل الزون أو قريب منها بقدر ATR*TOUCH_ATR_MULT
    low, high = zone
    if low <= price <= high:
        return True
    return abs(price - zone_mid(zone)) <= (TOUCH_ATR_MULT * a30)


# =========================
# TP levels (big targets)
# =========================
def calc_tps(direction: str, entry: float, sl: float) -> Tuple[float, float, float]:
    # Bigger targets: 3R / 5R / 8R
    R = abs(entry - sl)
    if R <= 0:
        R = max(entry * 0.001, 0.5)

    if direction == "BUY":
        return entry + 3*R, entry + 5*R, entry + 8*R
    else:
        return entry - 3*R, entry - 5*R, entry - 8*R


# =========================
# Signal engine (M30 pro + Market/Pending)
# =========================
def build_signal(name: str, symbol: str) -> Optional[str]:
    d1 = fetch_ohlc(symbol, "1d", PERIOD_D1)
    h4 = fetch_ohlc(symbol, "4h", PERIOD_H4)
    m30 = fetch_ohlc(symbol, "30m", PERIOD_M30)

    td = trend_score(d1)
    th = trend_score(h4)
    overall = td + th

    # Strict: only +2 / -2
    if overall not in (-2, 2):
        return None

    direction = "BUY" if overall == 2 else "SELL"

    # M30 pro filters
    if not atr_ok_m30(m30):
        return None
    if not candle_confirm_m30(m30, direction):
        return None
    if not bos_m30(m30, direction):
        return None

    price = float(m30["Close"].iloc[-1])
    a30 = atr(m30, 14)

    ob = find_order_block_m30(m30, direction)
    fvgs = find_fvg_m30(m30, direction, max_items=2)

    best_zone = None
    best_name = None
    if ob:
        best_zone = ob
        best_name = "OrderBlock"
    elif fvgs:
        best_zone = fvgs[-1]
        best_name = "FVG"
    else:
        return None

    risk_usd = round(ACCOUNT_USD * (RISK_PCT / 100.0), 2)
    emoji = "🔵" if direction == "BUY" else "🔴"

    # ✅ Market إذا لمس المنطقة
    if price_touches_zone(price, best_zone, a30):
        entry = price  # Market at current M30 close
        if direction == "BUY":
            sl = best_zone[0] - (a30 * SL_BUFFER_ATR)
        else:
            sl = best_zone[1] + (a30 * SL_BUFFER_ATR)

        tp1, tp2, tp3 = calc_tps(direction, entry, sl)

        msg = (
            f"📌 {name} ({symbol})\n"
            f"Trend D1/H4: {td:+d} / {th:+d} => Overall: {overall:+d}\n\n"
            f"{emoji} {direction} (MARKET): {entry:.2f}\n"
            f"SL: {sl:.2f}\n"
            f"TP1: {tp1:.2f}\n"
            f"TP2: {tp2:.2f}\n"
            f"TP3: {tp3:.2f}\n\n"
            f"Zone: {best_name} [{best_zone[0]:.2f} - {best_zone[1]:.2f}]\n"
            f"Risk: {int(RISK_PCT)}% (~${risk_usd})\n"
            f"TF: M30 Pro (Candle+BOS)"
        )
        return msg

    # ✅ وإلا Pending عند منتصف المنطقة (إذا ليست بعيدة جدًا)
    entry = zone_mid(best_zone)
    if abs(entry - price) > (MAX_PENDING_DISTANCE_ATR * a30):
        return None

    if direction == "BUY":
        sl = best_zone[0] - (a30 * SL_BUFFER_ATR)
        order_name = "Buy Limit"
    else:
        sl = best_zone[1] + (a30 * SL_BUFFER_ATR)
        order_name = "Sell Limit"

    tp1, tp2, tp3 = calc_tps(direction, entry, sl)

    msg = (
        f"📌 {name} ({symbol})\n"
        f"Trend D1/H4: {td:+d} / {th:+d} => Overall: {overall:+d}\n\n"
        f"{'🟢' if direction=='BUY' else '🔴'} {order_name}: {entry:.2f}\n"
        f"SL: {sl:.2f}\n"
        f"TP1: {tp1:.2f}\n"
        f"TP2: {tp2:.2f}\n"
        f"TP3: {tp3:.2f}\n\n"
        f"Zone: {best_name} [{best_zone[0]:.2f} - {best_zone[1]:.2f}]\n"
        f"Risk: {int(RISK_PCT)}% (~${risk_usd})\n"
        f"TF: M30 Pro (Candle+BOS)"
    )
    return msg


# =========================
# Cache / Anti-spam
# =========================
def msg_hash(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()

def main():
    if BOT_TOKEN and CHAT_ID:
        tg_send("✅ Bot started (M30 Pro: Market on Touch + Pending OB/FVG)")

    last_hash: Dict[str, str] = {}
    last_time: Dict[str, float] = {}

    while True:
        try:
            now = time.time()

            for name, sym in SYMBOLS.items():
                prev = last_time.get(name, 0.0)
                if (now - prev) < (COOLDOWN_MINUTES * 60):
                    continue

                try:
                    msg = build_signal(name, sym)
                except Exception as e:
                    logging.error("Build error %s (%s): %s", name, sym, e)
                    continue

                if not msg:
                    continue

                h = msg_hash(msg)
                if last_hash.get(name) == h:
                    continue

                if tg_send(msg):
                    last_hash[name] = h
                    last_time[name] = now
                    logging.info("Sent %s", name)
                    time.sleep(1)

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            logging.error("Main loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
