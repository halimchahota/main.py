import os
import io
import time
import math
import json
import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import matplotlib.pyplot as plt
import mplfinance as mpf


# =========================
# ENV / SETTINGS
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # "@channel" OR "-100xxxxxxxxxx"

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# Strategy toggles
USE_EMA_FILTER = os.getenv("USE_EMA_FILTER", "1").strip() == "1"
USE_RETEST = os.getenv("USE_RETEST", "1").strip() == "1"

# EMA
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_MID = int(os.getenv("EMA_MID", "50"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "200"))

# ATR / zones
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.5"))

# Retest
RETEST_MODE = os.getenv("RETEST_MODE", "touch").strip().lower()  # "touch" or "mid"
RETEST_ATR = float(os.getenv("RETEST_ATR", "0.35"))
INVALID_ATR = float(os.getenv("INVALID_ATR", "0.20"))

# BOS / candle confirm
BOS_LOOKBACK = int(os.getenv("BOS_LOOKBACK", "10"))

# RR targets (1R/2R/3R)
TP_R1 = float(os.getenv("TP_R1", "1"))
TP_R2 = float(os.getenv("TP_R2", "2"))
TP_R3 = float(os.getenv("TP_R3", "3"))

# Chart
CHART_BARS = int(os.getenv("CHART_BARS", "160"))

# Timeframes (Yahoo)
TF_SIGNAL = "30m"
PERIOD_SIGNAL = os.getenv("PERIOD_SIGNAL", "14d")
PERIOD_D1 = os.getenv("PERIOD_D1", "200d")
PERIOD_H1 = os.getenv("PERIOD_H1", "90d")  # build H4 from H1 resample

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
STATE_FILE = os.getenv("STATE_FILE", "/tmp/vip_bot_state.json")

SYMBOLS: Dict[str, str] = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30": os.getenv("US30_SYMBOL", "^DJI"),
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =========================
# Telegram (PHOTO first + fallback TEXT)
# =========================
def tg_send_photo(caption: str, image_bytes: bytes) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN / CHAT_ID missing (Railway Variables).")

    caption = (caption or "")[:1000]  # ✅ avoid Telegram caption limit 1024
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", image_bytes, "image/png")}
    data = {"chat_id": CHAT_ID, "caption": caption}

    r = requests.post(url, data=data, files=files, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram sendPhoto error {r.status_code}: {r.text}")


def tg_send_text(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN / CHAT_ID missing (Railway Variables).")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}

    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram sendMessage error {r.status_code}: {r.text}")


# =========================
# State / Anti-spam
# =========================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def now_ts() -> int:
    return int(time.time())


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def cooldown_ok(state: dict, key: str) -> bool:
    rec = state.get("sent", {}).get(key)
    if not rec:
        return True
    last = int(rec.get("ts", 0))
    return (now_ts() - last) >= COOLDOWN_MINUTES * 60


def already_sent_same(state: dict, key: str, sig: str) -> bool:
    rec = state.get("sent", {}).get(key)
    if not rec:
        return False
    return rec.get("sig") == sig


def mark_sent(state: dict, key: str, sig: str) -> None:
    state.setdefault("sent", {})[key] = {"ts": now_ts(), "sig": sig}


# =========================
# Market data
# =========================
def norm_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str.title).dropna()
    need = {"Open", "High", "Low", "Close"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    return df


def yf_fetch(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=False)
    return norm_df(df)


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    o = df["Open"].resample(rule).first()
    h = df["High"].resample(rule).max()
    l = df["Low"].resample(rule).min()
    c = df["Close"].resample(rule).last()
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}).dropna()


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

    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    v = tr.rolling(n).mean().iloc[-1]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return float(tr.iloc[-1])
    return float(v)


def trend_score(df: pd.DataFrame) -> int:
    # +1 bullish, -1 bearish, 0 neutral (EMA50/EMA200 + slope)
    if df.empty or len(df) < 80:
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


def ema_filter_ok(df: pd.DataFrame, direction: str) -> bool:
    # BUY: Close > EMA_SLOW & EMA_FAST > EMA_MID > EMA_SLOW
    # SELL: Close < EMA_SLOW & EMA_FAST < EMA_MID < EMA_SLOW
    if df.empty or len(df) < EMA_SLOW + 5:
        return False
    close = df["Close"]
    ef = float(ema(close, EMA_FAST).iloc[-1])
    em = float(ema(close, EMA_MID).iloc[-1])
    es = float(ema(close, EMA_SLOW).iloc[-1])
    c = float(close.iloc[-1])

    if direction == "BUY":
        return (c > es) and (ef > em > es)
    return (c < es) and (ef < em < es)


def candle_confirm_m30(m30: pd.DataFrame, direction: str) -> bool:
    o = float(m30["Open"].iloc[-1])
    c = float(m30["Close"].iloc[-1])
    return (c > o) if direction == "BUY" else (c < o)


def bos_m30(m30: pd.DataFrame, direction: str, lookback: int) -> bool:
    if m30.empty or len(m30) < lookback + 2:
        return False
    d = m30.tail(lookback + 2)
    last_close = float(d["Close"].iloc[-1])
    prev_high = float(d["High"].iloc[:-1].max())
    prev_low = float(d["Low"].iloc[:-1].min())
    return (last_close > prev_high) if direction == "BUY" else (last_close < prev_low)


# =========================
# Zones: OrderBlock + FVG (M30)
# =========================
@dataclass
class Zone:
    low: float
    high: float
    name: str  # "OrderBlock" or "FVG"
    side: str  # "BULL" or "BEAR"

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


def find_order_block_m30(m30: pd.DataFrame, direction: str) -> Optional[Zone]:
    if m30.empty or len(m30) < 60:
        return None

    w = m30.tail(160).copy()
    bodies = (w["Close"] - w["Open"]).abs()
    thr = float(bodies.quantile(0.75))

    for i in range(len(w) - 3, 15, -1):
        o = float(w["Open"].iloc[i])
        c = float(w["Close"].iloc[i])
        lo = float(w["Low"].iloc[i])
        hi = float(w["High"].iloc[i])

        next_body = abs(float(w["Close"].iloc[i + 1]) - float(w["Open"].iloc[i + 1]))
        if next_body < thr:
            continue

        if direction == "BUY":
            if c < o and float(w["Close"].iloc[i + 1]) > float(w["Open"].iloc[i + 1]):
                return Zone(low=min(lo, hi), high=max(lo, hi), name="OrderBlock", side="BULL")

        if direction == "SELL":
            if c > o and float(w["Close"].iloc[i + 1]) < float(w["Open"].iloc[i + 1]):
                return Zone(low=min(lo, hi), high=max(lo, hi), name="OrderBlock", side="BEAR")

    return None


def find_fvg_m30(m30: pd.DataFrame, direction: str) -> Optional[Zone]:
    if m30.empty or len(m30) < 30:
        return None

    d = m30.tail(400)
    h = d["High"].values
    l = d["Low"].values

    if direction == "BUY":
        for i in range(len(d) - 1, 2, -1):
            if l[i] > h[i - 2]:
                return Zone(low=float(h[i - 2]), high=float(l[i]), name="FVG", side="BULL")
    else:
        for i in range(len(d) - 1, 2, -1):
            if h[i] < l[i - 2]:
                return Zone(low=float(h[i]), high=float(l[i - 2]), name="FVG", side="BEAR")

    return None


# =========================
# RETEST VIP+
# =========================
def retest_decision(direction: str, price: float, zone: Zone, a30: float) -> Tuple[str, float]:
    """
    Returns (MODE, entry)
    MODE:
      - "MARKET": touched zone now => entry=current price
      - "PENDING": wait => entry=zone.mid
      - "CANCEL": invalidation => no trade
    """
    zlow, zhigh = zone.low, zone.high
    mid = zone.mid

    # invalidation
    if direction == "BUY":
        if price < (zlow - INVALID_ATR * a30):
            return "CANCEL", mid
    else:
        if price > (zhigh + INVALID_ATR * a30):
            return "CANCEL", mid

    # touch logic
    if RETEST_MODE == "mid":
        touched = abs(price - mid) <= (RETEST_ATR * a30)
    else:
        touched = (zlow <= price <= zhigh) or (abs(price - mid) <= (RETEST_ATR * a30))

    if touched:
        return "MARKET", price
    return "PENDING", mid


# =========================
# SL / TP (RR correct & monotonic)
# =========================
def calc_sl_tp(direction: str, entry: float, zone: Zone, a30: float) -> Tuple[float, float, float, float]:
    """
    SL beyond zone by SL_BUFFER_ATR*ATR
    TP based on true R = abs(entry - SL)
    """
    if direction == "BUY":
        sl = zone.low - (SL_BUFFER_ATR * a30)
        R = abs(entry - sl)
        tp1 = entry + TP_R1 * R
        tp2 = entry + TP_R2 * R
        tp3 = entry + TP_R3 * R
        # ensure monotonic
        tp1, tp2, tp3 = sorted([tp1, tp2, tp3])
    else:
        sl = zone.high + (SL_BUFFER_ATR * a30)
        R = abs(sl - entry)
        tp1 = entry - TP_R1 * R
        tp2 = entry - TP_R2 * R
        tp3 = entry - TP_R3 * R
        tp1, tp2, tp3 = sorted([tp1, tp2, tp3], reverse=True)

    # round ONLY at end
    return round(sl, 2), round(tp1, 2), round(tp2, 2), round(tp3, 2)


# =========================
# Chart rendering (Candles + EMAs + Zone + Levels)
# =========================
def render_chart(m30: pd.DataFrame, sig: dict) -> bytes:
    plot_df = m30.copy().tail(CHART_BARS)
    plot_df.index.name = "Date"

    plot_df["EMA20"] = plot_df["Close"].ewm(span=20, adjust=False).mean()
    plot_df["EMA50"] = plot_df["Close"].ewm(span=50, adjust=False).mean()
    plot_df["EMA200"] = plot_df["Close"].ewm(span=200, adjust=False).mean()

    zone: Zone = sig["zone"]
    entry = sig["entry"]
    sl = sig["sl"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]

    apds = [
        mpf.make_addplot(plot_df["EMA20"], width=1.0),
        mpf.make_addplot(plot_df["EMA50"], width=1.0),
        mpf.make_addplot(plot_df["EMA200"], width=1.2),
    ]

    hlines = [entry, sl, tp1, tp2, tp3]
    colors = ["g", "r", "k", "k", "k"]

    fig = mpf.figure(figsize=(10, 6), dpi=150)
    ax = fig.add_subplot(1, 1, 1)

    mpf.plot(
        plot_df,
        type="candle",
        ax=ax,
        volume=False,
        style="yahoo",
        addplot=apds,
        hlines=dict(hlines=hlines, colors=colors, linewidths=[1.2, 1.2, 1.0, 1.0, 1.0], alpha=0.9),
        axtitle=f"{sig['name']} ({sig['ticker']}) - M30 | EMA20/50/200",
    )

    # zone highlight
    ax.axhspan(zone.low, zone.high, xmin=0.0, xmax=1.0, alpha=0.12)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================
# Build signal
# =========================
def build_signal(symbol_name: str, ticker: str) -> Optional[dict]:
    # M30 data
    m30 = yf_fetch(ticker, TF_SIGNAL, PERIOD_SIGNAL)
    if m30.empty or len(m30) < 120:
        return None

    # Trend frames
    d1 = yf_fetch(ticker, "1d", PERIOD_D1)
    h1 = yf_fetch(ticker, "60m", PERIOD_H1)
    if d1.empty or h1.empty:
        return None
    h4 = resample_ohlc(h1, "4H")
    if h4.empty:
        return None

    td = trend_score(d1)
    th = trend_score(h4)
    overall = td + th

    # strict trend
    if overall not in (-2, 2):
        return None

    direction = "BUY" if overall == 2 else "SELL"
    price = float(m30["Close"].iloc[-1])

    # ATR filter
    a30 = atr(m30, 14)
    if not np.isfinite(a30) or a30 <= 0:
        return None
    if (a30 / price) > MAX_ATR_PCT:
        return None

    # M30 confirm: candle + BOS
    if not candle_confirm_m30(m30, direction):
        return None
    if not bos_m30(m30, direction, BOS_LOOKBACK):
        return None

    # EMA filter
    if USE_EMA_FILTER and not ema_filter_ok(m30, direction):
        return None

    # Zones
    ob = find_order_block_m30(m30, direction)
    fvg = find_fvg_m30(m30, direction)
    zone = ob if ob else fvg
    if not zone:
        return None

    # distance filter
    if abs(zone.mid - price) > (MAX_PENDING_DISTANCE_ATR * a30):
        return None

    # RETEST
    if USE_RETEST:
        mode, entry = retest_decision(direction, price, zone, a30)
        if mode == "CANCEL":
            return None
    else:
        mode, entry = "PENDING", zone.mid

    sl, tp1, tp2, tp3 = calc_sl_tp(direction, float(entry), zone, a30)

    # simple confidence
    conf = 0
    conf += 4  # strong trend
    conf += 2  # BOS
    conf += 2 if USE_EMA_FILTER else 0
    conf += 2  # zone found
    conf += 1 if zone.name == "OrderBlock" else 0
    conf = min(10, conf)

    return {
        "name": symbol_name,
        "ticker": ticker,
        "trend_d1": td,
        "trend_h4": th,
        "overall": overall,
        "direction": direction,
        "mode": mode,  # MARKET / PENDING
        "price": round(price, 2),
        "zone": zone,
        "entry": round(float(entry), 2),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_usd": round(ACCOUNT_BALANCE * (RISK_PCT / 100.0), 2),
        "confidence": conf,
    }


def format_caption(sig: dict) -> str:
    zone: Zone = sig["zone"]
    direction = sig["direction"]
    mode = sig["mode"]

    icon = "🔵" if direction == "BUY" else "🔴"
    if mode == "PENDING":
        order = ("Buy Limit" if direction == "BUY" else "Sell Limit") + " (RETEST)"
    else:
        order = f"{direction} (RETEST MARKET)"

    return (
        f"🔥 VIP M30+\n"
        f"📌 {sig['name']} ({sig['ticker']})\n"
        f"Trend D1/H4: {sig['trend_d1']:+d} / {sig['trend_h4']:+d} => Overall: {sig['overall']:+d}\n\n"
        f"{icon} {order}: {sig['entry']:.2f}\n"
        f"SL: {sig['sl']:.2f}\n"
        f"TP1: {sig['tp1']:.2f}\n"
        f"TP2: {sig['tp2']:.2f}\n"
        f"TP3: {sig['tp3']:.2f}\n\n"
        f"Zone: {zone.name} [{zone.low:.2f} - {zone.high:.2f}] ({zone.side})\n"
        f"Risk: {RISK_PCT:.1f}% (~${sig['risk_usd']:.2f})\n"
        f"EMA Filter: {'ON' if USE_EMA_FILTER else 'OFF'} | Retest: {'ON' if USE_RETEST else 'OFF'}\n"
        f"Confidence: {sig['confidence']}/10"
    )


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing BOT_TOKEN/CHAT_ID. Set them in Railway Variables.")
        return

    state = load_state()
    logging.info("VIP bot started.")

    # Startup ping (optional)
    try:
        tg_send_text("✅ VIP M30+ started (Chart + EMAs + EMA Filter + Retest).")
    except Exception as e:
        logging.error("Startup message failed: %s", e)

    while True:
        try:
            for name, ticker in SYMBOLS.items():
                sig = build_signal(name, ticker)
                if not sig:
                    continue

                # signature (avoid duplicates)
                zone: Zone = sig["zone"]
                signature = sha(
                    f"{ticker}|{sig['direction']}|{sig['mode']}|{sig['entry']}|{sig['sl']}|{zone.name}|{round(zone.low,2)}|{round(zone.high,2)}"
                )
                key = f"{ticker}:{sig['direction']}"

                if not cooldown_ok(state, key):
                    continue
                if already_sent_same(state, key, signature):
                    continue

                # build chart + send photo (fallback text)
                try:
                    m30 = yf_fetch(ticker, TF_SIGNAL, PERIOD_SIGNAL)
                    img = render_chart(m30, sig)
                    cap = format_caption(sig)
                    tg_send_photo(cap, img)
                except Exception as e:
                    logging.error("Send photo failed, fallback to text. Reason: %s", e)
                    try:
                        tg_send_text(format_caption(sig))
                    except Exception as e2:
                        logging.error("Fallback text failed too: %s", e2)
                        raise

                mark_sent(state, key, signature)
                save_state(state)
                logging.info("Sent %s (%s)", name, sig["mode"])
                time.sleep(1)

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            logging.exception("Main loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
