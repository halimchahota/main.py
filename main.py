import os
import time
import json
import math
import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
import requests


# =========================
# Config from Environment
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()   # ex: "@abdel_tra" OR "-1001234567890"

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# Strategy knobs (ATR-based)
TOUCH_ATR_MULT = float(os.getenv("TOUCH_ATR_MULT", "0.35"))
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.5"))
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))  # skip if ATR% too high (noise/vol spike)

# Symbols
SYMBOLS = {
    "XAU": os.getenv("SYMBOL_XAU", "GC=F"),
    "BTC": os.getenv("SYMBOL_BTC", "BTC-USD"),
    "US100": os.getenv("SYMBOL_US100", "NQ=F"),
    "US30": os.getenv("SYMBOL_US30", "^DJI"),
    "OIL": os.getenv("SYMBOL_OIL", "CL=F"),
}

STATE_FILE = os.getenv("STATE_FILE", "/tmp/bot_state.json")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)


# =========================
# Telegram
# =========================
def tg_send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("BOT_TOKEN or CHAT_ID missing. Set them in Railway Variables.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram send failed: %s", e)
        return False


# =========================
# State (cooldown)
# =========================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_sent": {}}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        logging.warning("Failed to save state: %s", e)


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def cooldown_ok(state: dict, key: str) -> bool:
    last = state.get("last_sent", {}).get(key)
    if not last:
        return True
    return (now_ts() - int(last)) >= COOLDOWN_MINUTES * 60


def mark_sent(state: dict, key: str) -> None:
    state.setdefault("last_sent", {})[key] = now_ts()


# =========================
# Market data helpers
# =========================
def yf_download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df is None or df.empty:
        return pd.DataFrame()
    # Normalize columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str.title)
    for col in ["Open", "High", "Low", "Close"]:
        if col not in df.columns:
            return pd.DataFrame()
    df = df.dropna()
    return df


def resample_ohlc(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    o = df_1h["Open"].resample(rule).first()
    h = df_1h["High"].resample(rule).max()
    l = df_1h["Low"].resample(rule).min()
    c = df_1h["Close"].resample(rule).last()
    out = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}).dropna()
    return out


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


# =========================
# S/R pivots
# =========================
def pivot_levels(df: pd.DataFrame, window: int = 3, top_n: int = 3):
    # simple pivots
    highs = df["High"]
    lows = df["Low"]

    ph = (highs.shift(window) < highs) & (highs.shift(-window) < highs)
    pl = (lows.shift(window) > lows) & (lows.shift(-window) > lows)

    piv_h = df.loc[ph, "High"].tail(30).tolist()
    piv_l = df.loc[pl, "Low"].tail(30).tolist()

    # pick most recent unique-ish
    def uniq_last(vals):
        out = []
        for v in reversed(vals):
            if all(abs(v - x) > (abs(v) * 0.0005 + 1e-9) for x in out):
                out.append(v)
            if len(out) >= top_n:
                break
        return list(reversed(out))

    return uniq_last(piv_l), uniq_last(piv_h)


# =========================
# FVG (ICT-style approximation)
# =========================
def find_last_fvgs(df: pd.DataFrame, lookback: int = 120):
    # bullish: high(i-1) < low(i+1)
    # bearish: low(i-1) > high(i+1)
    d = df.tail(lookback).copy()
    if len(d) < 5:
        return [], []

    bull = []
    bear = []
    h = d["High"].values
    l = d["Low"].values
    idx = d.index.to_list()

    for i in range(1, len(d) - 1):
        if h[i - 1] < l[i + 1]:
            bull.append((idx[i], float(h[i - 1]), float(l[i + 1])))
        if l[i - 1] > h[i + 1]:
            bear.append((idx[i], float(h[i + 1]), float(l[i - 1])))

    return bull[-2:], bear[-2:]


# =========================
# OrderBlock (approximation)
# =========================
def find_order_block(df: pd.DataFrame, direction: str, atr_val: float, lookback: int = 60):
    """
    Approx:
    - For BUY: find last bearish candle before a strong up impulse
    - For SELL: find last bullish candle before a strong down impulse
    """
    d = df.tail(lookback).copy()
    if len(d) < 20 or not atr_val or math.isnan(atr_val):
        return None

    d["Body"] = (d["Close"] - d["Open"]).abs()
    d["Ret"] = d["Close"].pct_change()

    # define impulse as move > 1.2*ATR in next few candles
    impulse_mult = 1.2

    candles = list(d.itertuples())
    for j in range(len(candles) - 6, 5, -1):
        c = candles[j]
        # next move magnitude
        nxt = d.iloc[j+1:j+6]
        if nxt.empty:
            continue
        move_up = (nxt["High"].max() - c.Close)
        move_dn = (c.Close - nxt["Low"].min())

        if direction == "BUY":
            # bearish candle then strong up
            if c.Close < c.Open and move_up >= impulse_mult * atr_val:
                low_ = float(min(c.Open, c.Close, c.Low))
                high_ = float(max(c.Open, c.Close, c.High))
                return (low_, high_)
        else:
            # bullish candle then strong down
            if c.Close > c.Open and move_dn >= impulse_mult * atr_val:
                low_ = float(min(c.Open, c.Close, c.Low))
                high_ = float(max(c.Open, c.Close, c.High))
                return (low_, high_)
    return None


# =========================
# Trend (D1 + H4)
# =========================
def trend_score(df: pd.DataFrame) -> int:
    if df.empty or len(df) < 220:
        return 0
    c = df["Close"]
    e50 = ema(c, 50).iloc[-1]
    e200 = ema(c, 200).iloc[-1]
    last = c.iloc[-1]

    if last > e200 and e50 > e200:
        return +1
    if last < e200 and e50 < e200:
        return -1
    return 0


# =========================
# Signal on M30 with zones
# =========================
def build_trade(symbol_name: str, symbol: str):
    # D1
    d1 = yf_download(symbol, period="180d", interval="1d")
    # H4 (from 1h)
    h1 = yf_download(symbol, period="60d", interval="60m")
    if h1.empty:
        return None
    h4 = resample_ohlc(h1, "4H")

    # M30 (entry timeframe)
    m30 = yf_download(symbol, period="14d", interval="30m")
    if d1.empty or h4.empty or m30.empty:
        return None

    d1_score = trend_score(d1)
    h4_score = trend_score(h4)
    overall = d1_score + h4_score

    last_price = float(m30["Close"].iloc[-1])

    # ATR on M30
    m30_atr = atr(m30, 14).iloc[-1]
    if not m30_atr or math.isnan(m30_atr):
        return None

    atr_pct = m30_atr / last_price if last_price else 0
    if atr_pct > MAX_ATR_PCT:
        # Vol spike - skip
        return {
            "name": symbol_name, "symbol": symbol,
            "skip": True,
            "reason": f"ATR% too high ({atr_pct:.4f} > {MAX_ATR_PCT})",
            "price": last_price
        }

    # Bias from D1+H4
    if overall >= 1:
        bias = "BUY"
    elif overall <= -1:
        bias = "SELL"
    else:
        bias = "NEUTRAL"

    # Zones: supports/resistances + orderblock + fvgs
    supports, resistances = pivot_levels(m30, window=3, top_n=3)
    bull_fvgs, bear_fvgs = find_last_fvgs(m30, lookback=140)

    ob = None
    if bias == "BUY":
        ob = find_order_block(m30, "BUY", float(m30_atr))
    elif bias == "SELL":
        ob = find_order_block(m30, "SELL", float(m30_atr))

    zones = []
    # Add OB
    if ob:
        zlow, zhigh = ob
        zones.append(("OrderBlock", zlow, zhigh))

    # Add last FVGs
    for _, lo, hi in bull_fvgs:
        zones.append(("BULL_FVG", lo, hi))
    for _, lo, hi in bear_fvgs:
        zones.append(("BEAR_FVG", lo, hi))

    # Add SR
    for s in supports:
        zones.append(("Support", s - 0.10 * m30_atr, s + 0.10 * m30_atr))
    for r in resistances:
        zones.append(("Resistance", r - 0.10 * m30_atr, r + 0.10 * m30_atr))

    # Choose best pending entry zone
    max_dist = MAX_PENDING_DISTANCE_ATR * m30_atr

    def zone_mid(z):
        return (z[1] + z[2]) / 2.0

    chosen = None
    if bias == "BUY":
        # pick closest zone below price within max_dist
        candidates = [z for z in zones if zone_mid(z) <= last_price and (last_price - zone_mid(z)) <= max_dist]
        if candidates:
            chosen = sorted(candidates, key=lambda z: (last_price - zone_mid(z)))[0]
    elif bias == "SELL":
        # pick closest zone above price within max_dist
        candidates = [z for z in zones if zone_mid(z) >= last_price and (zone_mid(z) - last_price) <= max_dist]
        if candidates:
            chosen = sorted(candidates, key=lambda z: (zone_mid(z) - last_price))[0]

    # If neutral or no zone, fallback: no trade
    if bias == "NEUTRAL" or not chosen:
        return {
            "name": symbol_name, "symbol": symbol,
            "bias": bias,
            "trend": (d1_score, h4_score, overall),
            "price": last_price,
            "atr": float(m30_atr),
            "supports": supports,
            "resistances": resistances,
            "ob": ob,
            "bull_fvgs": bull_fvgs,
            "bear_fvgs": bear_fvgs,
            "trade": None
        }

    zname, zlow, zhigh = chosen
    entry = (zlow + zhigh) / 2.0

    # Touch filter (optional): if price already close to zone, still ok
    # (This is just for reporting; we still send pending by default)
    touch_ok = abs(last_price - entry) <= (TOUCH_ATR_MULT * m30_atr)

    # SL/TP
    if bias == "BUY":
        sl = zlow - SL_BUFFER_ATR * m30_atr
        r = entry - sl
        tp1 = entry + 1.0 * r
        tp2 = entry + 2.0 * r
        tp3 = entry + 3.0 * r
        pending_type = "Buy Limit"
    else:
        sl = zhigh + SL_BUFFER_ATR * m30_atr
        r = sl - entry
        tp1 = entry - 1.0 * r
        tp2 = entry - 2.0 * r
        tp3 = entry - 3.0 * r
        pending_type = "Sell Limit"

    # Risk info
    risk_usd = ACCOUNT_BALANCE * (RISK_PCT / 100.0)

    return {
        "name": symbol_name, "symbol": symbol,
        "bias": bias,
        "trend": (d1_score, h4_score, overall),
        "price": last_price,
        "atr": float(m30_atr),
        "atr_pct": float(m30_atr / last_price),
        "supports": supports,
        "resistances": resistances,
        "ob": ob,
        "bull_fvgs": bull_fvgs,
        "bear_fvgs": bear_fvgs,
        "trade": {
            "pending_type": pending_type,
            "zone_name": zname,
            "zone": (float(zlow), float(zhigh)),
            "entry": float(entry),
            "sl": float(sl),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "tp3": float(tp3),
            "touch_ok": bool(touch_ok),
            "risk_pct": float(RISK_PCT),
            "risk_usd": float(risk_usd),
        }
    }


def fmt_trade(t: dict) -> str:
    name = t["name"]
    sym = t["symbol"]
    price = t.get("price")
    d1, h4, overall = t.get("trend", (0, 0, 0))
    bias = t.get("bias", "NA")

    if t.get("skip"):
        return (
            f"📌 {name} ({sym})\n"
            f"Price: {price}\n"
            f"⛔ Skip: {t.get('reason')}\n"
        )

    trade = t.get("trade")
    lines = []
    lines.append(f"📌 {name} ({sym})")
    lines.append(f"Trend D1/H4: {d1:+d} / {h4:+d}  => Overall: {overall:+d}")
    lines.append(f"TF Entry: M30")
    lines.append(f"Price: {price:.5f}" if price and price < 1000 else f"Price: {price:.2f}")

    if not trade:
        lines.append("Signal: ⚪ NO TRADE (no clear bias/zone)")
        return "\n".join(lines)

    entry = trade["entry"]
    sl = trade["sl"]
    tp1, tp2, tp3 = trade["tp1"], trade["tp2"], trade["tp3"]
    zlow, zhigh = trade["zone"]
    ptype = trade["pending_type"]
    zname = trade["zone_name"]

    def f(x):
        if abs(x) < 1000:
            return f"{x:.5f}"
        return f"{x:.2f}"

    lines.append(f"Signal: {'🔵 BUY' if bias=='BUY' else '🔴 SELL'} (Pending)")
    lines.append(f"🟢 {ptype}: {f(entry)}")
    lines.append(f"SL: {f(sl)}")
    lines.append(f"TP1: {f(tp1)}")
    lines.append(f"TP2: {f(tp2)}")
    lines.append(f"TP3: {f(tp3)}")
    lines.append(f"Zone: {zname} [{f(zlow)} - {f(zhigh)}]")
    lines.append(f"Risk: {trade['risk_pct']:.1f}% (~${trade['risk_usd']:.2f})")
    return "\n".join(lines)


def main():
    # Quick startup ping (optional)
    logging.info("Bot starting...")
    state = load_state()

    while True:
        try:
            msgs = []
            for name, sym in SYMBOLS.items():
                result = build_trade(name, sym)
                if not result:
                    continue

                # cooldown key per symbol+direction+entry
                key = f"{name}:{result.get('bias')}"

                # only send when we actually have a trade
                if result.get("trade") and cooldown_ok(state, key):
                    msgs.append(fmt_trade(result))
                    mark_sent(state, key)

            if msgs:
                final = "بوت الشبح (M30)\n\n" + "\n\n".join(msgs)
                ok = tg_send(final)
                if ok:
                    save_state(state)
                    logging.info("Signals sent: %d", len(msgs))
                else:
                    logging.warning("Failed to send signals.")
            else:
                logging.info("No new signals (cooldown/no-trade).")

        except Exception as e:
            logging.exception("Main loop error: %s", e)

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
