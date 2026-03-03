import os
import time
import math
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

import requests
import yfinance as yf
import pandas as pd


# =========================
# Config (from Environment)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))  # $
RISK_PCT = float(os.getenv("RISK_PCT", "3"))                  # %
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))       # seconds
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

# Send WAIT? (0 = لا, 1 = نعم)
SEND_WAIT = os.getenv("SEND_WAIT", "0").strip() == "1"

# Limit error notifications to avoid spam
ERROR_NOTIFY_COOLDOWN_SEC = int(os.getenv("ERROR_NOTIFY_COOLDOWN_SEC", "900"))  # 15m

# Symbols (Yahoo Finance)
SYMBOLS: Dict[str, str] = {
    "XAU": "GC=F",      # Gold futures
    "BTC": "BTC-USD",
    "US100": "NQ=F",    # Nasdaq futures
    "US30": "^DJI",     # Dow Jones index
    "OIL": "CL=F",      # Crude oil futures
}

# Timeframes used
TF_D1 = ("1d", "90d")
TF_H4 = ("4h", "60d")
TF_M30 = ("30m", "14d")
TF_M15 = ("15m", "7d")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =========================
# Telegram
# =========================
def send_telegram_message(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing BOT_TOKEN or CHAT_ID. Set them in Railway Variables.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logging.error("Telegram send failed: %s", e)
        return False


# =========================
# Market Data
# =========================
def fetch_ohlc(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {symbol} interval={interval} period={period}")

    # Normalize columns (some come as multi-index)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    df = df.dropna()
    # Ensure required columns exist
    needed = {"Open", "High", "Low", "Close"}
    if not needed.issubset(set(df.columns)):
        raise RuntimeError(f"Missing OHLC columns for {symbol}: {df.columns.tolist()}")

    return df


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def trend_score(df: pd.DataFrame) -> int:
    close = df["Close"]
    e20 = ema(close, 20)
    e50 = ema(close, 50)

    if len(e20) < 30:
        return 0

    slope = (e20.iloc[-1] - e20.iloc[-10])  # 10 bars slope
    bullish = (e20.iloc[-1] > e50.iloc[-1]) and (slope > 0)
    bearish = (e20.iloc[-1] < e50.iloc[-1]) and (slope < 0)

    if bullish:
        return +1
    if bearish:
        return -1
    return 0


# =========================
# Levels: Support/Resistance
# =========================
def swing_levels(df: pd.DataFrame, lookback: int = 120) -> Tuple[List[float], List[float]]:
    d = df.tail(lookback).copy()
    highs = d["High"].values
    lows = d["Low"].values

    supports: List[float] = []
    resistances: List[float] = []

    for i in range(2, len(d) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            supports.append(float(lows[i]))
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistances.append(float(highs[i]))

    def dedup(levels: List[float], tol: float) -> List[float]:
        out: List[float] = []
        for x in sorted(levels):
            if not out or abs(x - out[-1]) > tol:
                out.append(x)
        return out

    last_price = float(d["Close"].iloc[-1])
    tol = max(0.001 * last_price, 0.5)

    supports = dedup(supports, tol)
    resistances = dedup(resistances, tol)

    supports_sorted = sorted(supports, key=lambda x: abs(last_price - x))
    resist_sorted = sorted(resistances, key=lambda x: abs(last_price - x))

    return supports_sorted[:3], resist_sorted[:3]


# =========================
# Order Block (simple)
# =========================
@dataclass
class OrderBlock:
    low: float
    high: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


def find_orderblock(df: pd.DataFrame, direction: str) -> Optional[OrderBlock]:
    d = df.tail(120).copy()
    if len(d) < 30:
        return None

    bodies = (d["Close"] - d["Open"]).abs()
    body_thr = bodies.quantile(0.75)

    for i in range(len(d) - 5, 10, -1):
        o, c = float(d["Open"].iloc[i]), float(d["Close"].iloc[i])
        h, l = float(d["High"].iloc[i]), float(d["Low"].iloc[i])

        next_body = abs(float(d["Close"].iloc[i+1]) - float(d["Open"].iloc[i+1]))
        if next_body < body_thr:
            continue

        if direction == "BUY":
            if c < o and float(d["Close"].iloc[i+1]) > float(d["Open"].iloc[i+1]):
                return OrderBlock(low=l, high=h)

        if direction == "SELL":
            if c > o and float(d["Close"].iloc[i+1]) < float(d["Open"].iloc[i+1]):
                return OrderBlock(low=l, high=h)

    return None


# =========================
# FVG (simple)
# =========================
def find_fvgs(df: pd.DataFrame, max_items: int = 2) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    d = df.tail(200).copy()
    bull: List[Tuple[float, float]] = []
    bear: List[Tuple[float, float]] = []

    for i in range(2, len(d)):
        h2 = float(d["High"].iloc[i-2])
        l2 = float(d["Low"].iloc[i-2])
        hi = float(d["High"].iloc[i])
        li = float(d["Low"].iloc[i])

        if li > h2:
            bull.append((h2, li))
        if hi < l2:
            bear.append((hi, l2))

    return bull[-max_items:], bear[-max_items:]


# =========================
# Risk + Targets
# =========================
def calc_levels(direction: str, entry: float, sl: float) -> Tuple[float, float, float]:
    r = abs(entry - sl)
    if r <= 0:
        r = max(entry * 0.001, 0.5)

    if direction == "BUY":
        return entry + 1*r, entry + 2*r, entry + 3*r
    else:
        return entry - 1*r, entry - 2*r, entry - 3*r


# =========================
# Signal Engine
# =========================
def decide_signal(d1: pd.DataFrame, h4: pd.DataFrame, m30: pd.DataFrame, m15: pd.DataFrame) -> Tuple[str, int]:
    t_d1 = trend_score(d1)
    t_h4 = trend_score(h4)
    overall = t_d1 + t_h4

    close15 = m15["Close"]
    e20_15 = ema(close15, 20)

    trigger_up = close15.iloc[-1] > e20_15.iloc[-1]
    trigger_dn = close15.iloc[-1] < e20_15.iloc[-1]

    if overall >= +1 and trigger_up:
        return "BUY", overall
    if overall <= -1 and trigger_dn:
        return "SELL", overall
    return "WAIT", overall


def _fmt_levels(xs: List[float]) -> str:
    return ", ".join([f"{x:.2f}" for x in xs]) if xs else "N/A"


def build_message(name: str, symbol: str) -> Optional[Tuple[str, str]]:
    """
    Returns (message, signature) or None.
    signature = key used to prevent spamming.
    """
    d1 = fetch_ohlc(symbol, *TF_D1)
    h4 = fetch_ohlc(symbol, *TF_H4)
    m30 = fetch_ohlc(symbol, *TF_M30)
    m15 = fetch_ohlc(symbol, *TF_M15)

    price = float(m15["Close"].iloc[-1])
    signal, overall = decide_signal(d1, h4, m30, m15)

    supports, resistances = swing_levels(h4, lookback=140)
    bull_fvg, bear_fvg = find_fvgs(m30, max_items=2)

    if signal == "WAIT":
        if not SEND_WAIT:
            return None
        msg = (
            f"📌 {name} ({symbol})\n"
            f"Trend D1+H4: Overall: {overall}\n"
            f"Signal: ⏳ WAIT\n"
            f"Price: {price:.2f}\n"
        )
        sig = f"{name}|WAIT|{overall}"
        return msg, sig

    if signal == "BUY":
        ob = find_orderblock(m30, "BUY")
        entry_pending = ob.mid if ob else (supports[0] if supports else price)
        entry_market = price

        sl_ref = ob.low if ob else (supports[0] if supports else price)
        sl_market = min(sl_ref, entry_market) - max(entry_market * 0.001, 0.5)
        tp1, tp2, tp3 = calc_levels("BUY", entry_market, sl_market)

        sl_pending = (ob.low if ob else entry_pending) - max(entry_pending * 0.001, 0.5)
        tp1p, tp2p, tp3p = calc_levels("BUY", entry_pending, sl_pending)

        far = abs(price - entry_pending) > max(price * 0.002, 1.0)

        if far:
            msg = (
                f"📌 {name} ({symbol})\n"
                f"Trend D1+H4: Overall: {overall}\n"
                f"Signal: 🟦 PENDING BUY (BuyLimit)\n"
                f"BuyLimit: {entry_pending:.2f}\n"
                f"SL: {sl_pending:.2f}\n"
                f"TP1: {tp1p:.2f} | TP2: {tp2p:.2f} | TP3: {tp3p:.2f}\n\n"
                f"Supports: {_fmt_levels(supports)}\n"
                f"Resistances: {_fmt_levels(resistances)}\n"
                f"OrderBlock: {f'[{ob.low:.2f} - {ob.high:.2f}]' if ob else 'N/A'}\n"
                f"FVG Bull: {', '.join([f'[{a:.2f}-{b:.2f}]' for a,b in bull_fvg]) or 'N/A'}\n"
            )
            sig = f"{name}|PBUY|{overall}|{entry_pending:.2f}|{sl_pending:.2f}|{tp1p:.2f}|{tp2p:.2f}|{tp3p:.2f}"
        else:
            msg = (
                f"📌 {name} ({symbol})\n"
                f"Trend D1+H4: Overall: {overall}\n"
                f"Signal: 🔵 BUY\n"
                f"Entry: {entry_market:.2f}\n"
                f"SL: {sl_market:.2f}\n"
                f"TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n\n"
                f"Pending (Level): BuyLimit {entry_pending:.2f}\n"
                f"SL: {sl_pending:.2f}\n"
                f"TP1: {tp1p:.2f} | TP2: {tp2p:.2f} | TP3: {tp3p:.2f}\n"
            )
            sig = f"{name}|BUY|{overall}|{sl_market:.2f}|{tp1:.2f}|{tp2:.2f}|{tp3:.2f}|{entry_pending:.2f}"

        return msg, sig

    if signal == "SELL":
        ob = find_orderblock(m30, "SELL")
        entry_pending = ob.mid if ob else (resistances[0] if resistances else price)
        entry_market = price

        sl_ref = ob.high if ob else (resistances[0] if resistances else price)
        sl_market = max(sl_ref, entry_market) + max(entry_market * 0.001, 0.5)
        tp1, tp2, tp3 = calc_levels("SELL", entry_market, sl_market)

        sl_pending = (ob.high if ob else entry_pending) + max(entry_pending * 0.001, 0.5)
        tp1p, tp2p, tp3p = calc_levels("SELL", entry_pending, sl_pending)

        far = abs(price - entry_pending) > max(price * 0.002, 1.0)

        if far:
            msg = (
                f"📌 {name} ({symbol})\n"
                f"Trend D1+H4: Overall: {overall}\n"
                f"Signal: 🟥 PENDING SELL (SellLimit)\n"
                f"SellLimit: {entry_pending:.2f}\n"
                f"SL: {sl_pending:.2f}\n"
                f"TP1: {tp1p:.2f} | TP2: {tp2p:.2f} | TP3: {tp3p:.2f}\n\n"
                f"Supports: {_fmt_levels(supports)}\n"
                f"Resistances: {_fmt_levels(resistances)}\n"
                f"OrderBlock: {f'[{ob.low:.2f} - {ob.high:.2f}]' if ob else 'N/A'}\n"
                f"FVG Bear: {', '.join([f'[{a:.2f}-{b:.2f}]' for a,b in bear_fvg]) or 'N/A'}\n"
            )
            sig = f"{name}|PSELL|{overall}|{entry_pending:.2f}|{sl_pending:.2f}|{tp1p:.2f}|{tp2p:.2f}|{tp3p:.2f}"
        else:
            msg = (
                f"📌 {name} ({symbol})\n"
                f"Trend D1+H4: Overall: {overall}\n"
                f"Signal: 🔴 SELL\n"
                f"Entry: {entry_market:.2f}\n"
                f"SL: {sl_market:.2f}\n"
                f"TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n\n"
                f"Pending (Level): SellLimit {entry_pending:.2f}\n"
                f"SL: {sl_pending:.2f}\n"
                f"TP1: {tp1p:.2f} | TP2: {tp2p:.2f} | TP3: {tp3p:.2f}\n"
            )
            sig = f"{name}|SELL|{overall}|{sl_market:.2f}|{tp1:.2f}|{tp2:.2f}|{tp3:.2f}|{entry_pending:.2f}"

        return msg, sig

    return None


# =========================
# Main loop
# =========================
def run():
    if not BOT_TOKEN or not CHAT_ID:
        logging.warning("BOT_TOKEN/CHAT_ID missing. Bot will not send messages until set.")
    else:
        send_telegram_message("✅ Bot started on Railway. Auto-signals running.")

    last_sent_sig: Dict[str, str] = {}
    last_error_sent_at: Dict[str, float] = {}  # per symbol

    while True:
        for name, sym in SYMBOLS.items():
            try:
                built = build_message(name, sym)
                if not built:
                    continue

                msg, sig = built
                if last_sent_sig.get(name) != sig:
                    if send_telegram_message(msg):
                        last_sent_sig[name] = sig
                        logging.info("Sent update for %s", name)

            except Exception as e:
                logging.error("Error on %s (%s): %s", name, sym, e)

                # optional: notify to telegram but with cooldown
                now = time.time()
                last_t = last_error_sent_at.get(name, 0)
                if BOT_TOKEN and CHAT_ID and (now - last_t) >= ERROR_NOTIFY_COOLDOWN_SEC:
                    send_telegram_message(f"⚠️ Error on {name} ({sym}): {str(e)[:150]}")
                    last_error_sent_at[name] = now

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
