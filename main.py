import os
import io
import time
import math
import json
import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

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

# Money / risk (display + generic sizing)
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

# Loop / spam control
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# Strategy toggles
USE_EMA_FILTER = os.getenv("USE_EMA_FILTER", "1").strip() == "1"
USE_RETEST = os.getenv("USE_RETEST", "1").strip() == "1"

# EMA settings
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_MID = int(os.getenv("EMA_MID", "50"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "200"))

# Zones / ATR controls
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))  # skip if ATR% too high
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.5"))

# Retest controls
RETEST_MODE = os.getenv("RETEST_MODE", "touch").strip().lower()  # "touch" or "mid"
RETEST_ATR = float(os.getenv("RETEST_ATR", "0.35"))              # touch distance
INVALID_ATR = float(os.getenv("INVALID_ATR", "0.20"))            # invalidation beyond zone

# Telegram
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

# Storage (optional; Railway ephemeral, but fine for runtime)
STATE_FILE = os.getenv("STATE_FILE", "/tmp/vip_bot_state.json")

# Assets (Yahoo Finance)
SYMBOLS: Dict[str, str] = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30": os.getenv("US30_SYMBOL", "^DJI"),
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),
}

# Timeframes
TF_SIGNAL = "30m"
PERIOD_SIGNAL = os.getenv("PERIOD_SIGNAL", "14d")

PERIOD_D1 = os.getenv("PERIOD_D1", "200d")
PERIOD_H1 = os.getenv("PERIOD_H1", "90d")  # used to build H4 via resample

# BOS lookback on M30
BOS_LOOKBACK = int(os.getenv("BOS_LOOKBACK", "10"))

# Targets (RR)
TP_R1 = float(os.getenv("TP_R1", "1"))  # 1R
TP_R2 = float(os.getenv("TP_R2", "2"))  # 2R
TP_R3 = float(os.getenv("TP_R3", "3"))  # 3R

# Chart options
CHART_BARS = int(os.getenv("CHART_BARS", "160"))  # candles shown in image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =========================
# Telegram
# =========================
def tg_send_photo(caption: str, image_bytes: bytes) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN / CHAT_ID missing (set in Railway Variables).")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", image_bytes, "image/png")}
    data = {"chat_id": CHAT_ID, "caption": caption, "disable_notification": False}
    r = requests.post(url, data=data, files=files, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


def tg_send_text(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN / CHAT_ID missing (set in Railway Variables).")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


# =========================
# State / Anti-spam
# =========================
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def _now_ts() -> int:
    return int(time.time())


def _hash_msg(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _cooldown_ok(state: dict, key: str) -> bool:
    rec = state.get("sent", {}).get(key)
    if not rec:
        return True
    last_ts = int(rec.get("ts", 0))
    return (_now_ts() - last_ts) >= COOLDOWN_MINUTES * 60


def _mark_sent(state: dict, key: str, signature: str) -> None:
    state.setdefault("sent", {})[key] = {"ts": _now_ts(), "sig": signature}


def _already_sent_same(state: dict, key: str, signature: str) -> bool:
    rec = state.get("sent", {}).get(key)
    if not rec:
        return False
    return rec.get("sig") == signature


# =========================
# Market Data
# =========================
def _norm_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str.title).dropna()
    needed = {"Open", "High", "Low", "Close"}
    if not needed.issubset(df.columns):
        return pd.DataFrame()
    return df


def yf_fetch(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=False)
    return _norm_df(df)


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    o = df["Open"].resample(rule).first()
    h = df["High"].resample(rule).max()
    l = df["Low"].resample(rule).min()
    c = df["Close"].resample(rule).last()
    out = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}).dropna()
    return out


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

    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1
    ).max(axis=1)

    v = tr.rolling(n).mean().iloc[-1]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return float(tr.iloc[-1])
    return float(v)


def trend_score(df: pd.DataFrame) -> int:
    """
    Simple stable trend:
    +1 bullish, -1 bearish, 0 neutral
    based on EMA50 vs EMA200 and slope EMA50
    """
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
    """
    BUY:  Close > EMA_SLOW and EMA_FAST > EMA_MID > EMA_SLOW
    SELL: Close < EMA_SLOW and EMA_FAST < EMA_MID < EMA_SLOW
    """
    if df.empty or len(df) < EMA_SLOW + 5:
        return False

    close = df["Close"]
    ef = ema(close, EMA_FAST).iloc[-1]
    em = ema(close, EMA_MID).iloc[-1]
    es = ema(close, EMA_SLOW).iloc[-1]
    c = float(close.iloc[-1])

    if direction == "BUY":
        return (c > es) and (ef > em > es)
    return (c < es) and (ef < em < es)


def bos_m30(m30: pd.DataFrame, direction: str, lookback: int) -> bool:
    """
    BOS:
    BUY  => last close > max previous highs
    SELL => last close < min previous lows
    """
    if m30.empty or len(m30) < lookback + 2:
        return False
    d = m30.tail(lookback + 2)
    last_close = float(d["Close"].iloc[-1])
    prev_high = float(d["High"].iloc[:-1].max())
    prev_low = float(d["Low"].iloc[:-1].min())
    return (last_close > prev_high) if direction == "BUY" else (last_close < prev_low)


def candle_confirm_m30(m30: pd.DataFrame, direction: str) -> bool:
    o = float(m30["Open"].iloc[-1])
    c = float(m30["Close"].iloc[-1])
    return (c > o) if direction == "BUY" else (c < o)


# =========================
# Zones: OB + FVG (M30)
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
    """
    Heuristic:
    BUY  => last bearish candle before strong bullish impulse
    SELL => last bullish candle before strong bearish impulse
    """
    if m30.empty or len(m30) < 60:
        return None

    w = m30.tail(160).copy()
    bodies = (w["Close"] - w["Open"]).abs()
    thr = float(bodies.quantile(0.75))

    for i in range(len(w) - 3, 15, -1):
        o = float(w["Open"].iloc[i]); c = float(w["Close"].iloc[i])
        lo = float(w["Low"].iloc[i]); hi = float(w["High"].iloc[i])

        next_body = abs(float(w["Close"].iloc[i+1]) - float(w["Open"].iloc[i+1]))
        if next_body < thr:
            continue

        if direction == "BUY":
            if c < o and float(w["Close"].iloc[i+1]) > float(w["Open"].iloc[i+1]):
                return Zone(low=min(lo, hi), high=max(lo, hi), name="OrderBlock", side="BULL")

        if direction == "SELL":
            if c > o and float(w["Close"].iloc[i+1]) < float(w["Open"].iloc[i+1]):
                return Zone(low=min(lo, hi), high=max(lo, hi), name="OrderBlock", side="BEAR")

    return None


def find_fvg_m30(m30: pd.DataFrame, direction: str) -> Optional[Zone]:
    """
    3-candle FVG:
    Bull: low(i) > high(i-2) => [high(i-2), low(i)]
    Bear: high(i) < low(i-2) => [high(i), low(i-2)]
    return latest match
    """
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
      - "MARKET": touched zone now => entry = current price
      - "PENDING": wait => entry = zone.mid
      - "CANCEL": invalidation => no trade
    """
    zlow, zhigh = zone.low, zone.high
    mid = zone.mid

    # Invalidation
    if direction == "BUY":
        if price < (zlow - INVALID_ATR * a30):
            return "CANCEL", mid
    else:
        if price > (zhigh + INVALID_ATR * a30):
            return "CANCEL", mid

    # Touch
    if RETEST_MODE == "mid":
        touched = abs(price - mid) <= (RETEST_ATR * a30)
    else:
        touched = (zlow <= price <= zhigh) or (abs(price - mid) <= (RETEST_ATR * a30))

    if touched:
        return "MARKET", price
    return "PENDING", mid


# =========================
# SL / TP (RR correct)
# =========================
def calc_sl_tp(direction: str, entry: float, zone: Zone, a30: float) -> Tuple[float, float, float, float]:
    """
    SL beyond zone by SL_BUFFER_ATR*ATR
    TP based on 1R/2R/3R
    """
    if direction == "BUY":
        sl = zone.low - (SL_BUFFER_ATR * a30)
        R = abs(entry - sl)
        tp1 = entry + TP_R1 * R
        tp2 = entry + TP_R2 * R
        tp3 = entry + TP_R3 * R
    else:
        sl = zone.high + (SL_BUFFER_ATR * a30)
        R = abs(entry - sl)
        tp1 = entry - TP_R1 * R
        tp2 = entry - TP_R2 * R
        tp3 = entry - TP_R3 * R
    return sl, tp1, tp2, tp3


# =========================
# Chart rendering: candles + EMAs + zone + levels
# =========================
def render_chart(m30: pd.DataFrame, sig: dict) -> bytes:
    plot_df = m30.copy().tail(CHART_BARS)
    plot_df.index.name = "Date"

    # EMAs
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
        hlines=dict(
            hlines=hlines,
            colors=colors,
            linewidths=[1.2, 1.2, 1.0, 1.0, 1.0],
            alpha=0.9,
        ),
        axtitle=f"{sig['name']} ({sig['ticker']}) - M30 | EMA20/50/200",
    )

    # Zone rectangle
    ax.axhspan(zone.low, zone.high, xmin=0.0, xmax=1.0, alpha=0.12)

    # Right labels
    ax.text(1.01, entry, f"Entry {entry:.2f}", transform=ax.get_yaxis_transform(), va="center")
    ax.text(1.01, sl, f"SL {sl:.2f}", transform=ax.get_yaxis_transform(), va="center")
    ax.text(1.01, tp1, f"TP1 {tp1:.2f}", transform=ax.get_yaxis_transform(), va="center")
    ax.text(1.01, tp2, f"TP2 {tp2:.2f}", transform=ax.get_yaxis_transform(), va="center")
    ax.text(1.01, tp3, f"TP3 {tp3:.2f}", transform=ax.get_yaxis_transform(), va="center")

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================
# Signal builder per asset
# =========================
def build_signal(symbol_name: str, ticker: str) -> Optional[dict]:
    # Fetch M30
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

    # Trend
    td = trend_score(d1)
    th = trend_score(h4)
    overall = td + th

    # Strict trend: only strong alignment
    if overall not in (-2, 2):
        return None

    direction = "BUY" if overall == 2 else "SELL"

    # ATR filter
    price = float(m30["Close"].iloc[-1])
    a30 = atr(m30, 14)
    if not np.isfinite(a30) or a30 <= 0:
        return None
    if (a30 / price) > MAX_ATR_PCT:
        return None

    # M30 confirmations: candle + BOS
    if not candle_confirm_m30(m30, direction):
        return None
    if not bos_m30(m30, direction, BOS_LOOKBACK):
        return None

    # EMA filter (optional but recommended)
    if USE_EMA_FILTER and not ema_filter_ok(m30, direction):
        return None

    # Zones: prefer OrderBlock then FVG
    ob = find_order_block_m30(m30, direction)
    fvg = find_fvg_m30(m30, direction)
    zone = ob if ob else fvg
    if not zone:
        return None

    # Pending distance limit
    if abs(zone.mid - price) > (MAX_PENDING_DISTANCE_ATR * a30):
        return None

    # RETEST logic
    if USE_RETEST:
        mode, entry = retest_decision(direction, price, zone, a30)
        if mode == "CANCEL":
            return None
    else:
        # no retest: default pending at zone.mid
        mode, entry = "PENDING", zone.mid

    sl, tp1, tp2, tp3 = calc_sl_tp(direction, entry, zone, a30)

    # Basic confidence (simple score)
    conf = 0
    conf += 4  # overall strong trend
    conf += 2  # BOS
    conf += 2  # EMA filter (if enabled)
    conf += 2  # OB > FVG preference
    if zone.name == "OrderBlock":
        conf += 1
    conf = min(10, conf)

    risk_usd = ACCOUNT_BALANCE * (RISK_PCT / 100.0)

    return {
        "name": symbol_name,
        "ticker": ticker,
        "direction": direction,
        "mode": mode,  # MARKET or PENDING
        "price": price,
        "trend_d1": td,
        "trend_h4": th,
        "overall": overall,
        "zone": zone,
        "entry": float(entry),
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "tp3": float(tp3),
        "risk_usd": float(risk_usd),
        "confidence": int(conf),
    }


def format_caption(sig: dict) -> str:
    zone: Zone = sig["zone"]
    direction = sig["direction"]
    mode = sig["mode"]

    icon = "🔵" if direction == "BUY" else "🔴"
    order_name = f"{direction} ({mode})"
    if mode == "PENDING":
        order_name = ("Buy Limit" if direction == "BUY" else "Sell Limit") + " (RETEST)"

    ema_txt = "ON" if USE_EMA_FILTER else "OFF"
    ret_txt = f"ON ({RETEST_MODE})" if USE_RETEST else "OFF"

    return (
        f"🔥 VIP M30+\n"
        f"📌 {sig['name']} ({sig['ticker']})\n"
        f"Trend D1/H4: {sig['trend_d1']:+d} / {sig['trend_h4']:+d} => Overall: {sig['overall']:+d}\n\n"
        f"{icon} {order_name}: {sig['entry']:.2f}\n"
        f"SL: {sig['sl']:.2f}\n"
        f"TP1: {sig['tp1']:.2f}\n"
        f"TP2: {sig['tp2']:.2f}\n"
        f"TP3: {sig['tp3']:.2f}\n\n"
        f"Zone: {zone.name} [{zone.low:.2f} - {zone.high:.2f}] ({zone.side})\n"
        f"Risk: {RISK_PCT:.1f}% (~${sig['risk_usd']:.2f})\n"
        f"EMA Filter: {ema_txt} | Retest: {ret_txt}\n"
        f"Confidence: {sig['confidence']}/10"
    )


# =========================
# Main Loop
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing BOT_TOKEN/CHAT_ID. Set in Railway Variables and redeploy.")
        return

    state = _load_state()
    logging.info("VIP bot started.")

    # Optional startup ping (comment if you don't want)
    try:
        tg_send_text("✅ VIP M30+ bot started (Chart + EMAs + EMA Filter + Retest).")
    except Exception as e:
        logging.error("Startup Telegram send failed: %s", e)

    while True:
        try:
            for name, ticker in SYMBOLS.items():
                sig = build_signal(name, ticker)
                if not sig:
                    continue

                # signature to avoid duplicates
                signature = _hash_msg(
                    f"{ticker}|{sig['direction']}|{sig['mode']}|{round(sig['entry'],2)}|{round(sig['sl'],2)}|{sig['zone'].name}"
                )
                key = f"{ticker}:{sig['direction']}"

                # spam control
                if not _cooldown_ok(state, key):
                    continue
                if _already_sent_same(state, key, signature):
                    continue

                # build chart + send
                m30 = yf_fetch(ticker, TF_SIGNAL, PERIOD_SIGNAL)
                img = render_chart(m30, sig)
                caption = format_caption(sig)

                tg_send_photo(caption, img)
                _mark_sent(state, key, signature)
                _save_state(state)

                logging.info("Sent %s %s", name, sig["mode"])
                time.sleep(1)

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            logging.exception("Main loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
