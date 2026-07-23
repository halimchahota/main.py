
# -*- coding: utf-8 -*-
import os
import io
import time
import json
import math
import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import requests
import pandas as pd
import yfinance as yf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import json
from pathlib import Path
from datetime import datetime

TRADES_FILE = Path("trades.json")


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("institutional_adaptive_bot")


# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

M5_CONFIRMATION = os.getenv("M5_CONFIRMATION", "1") == "1"
USE_LIQUIDITY_SWEEP = os.getenv("USE_LIQUIDITY_SWEEP", "1") == "1"
LIQUIDITY_LOOKBACK = int(os.getenv("LIQUIDITY_LOOKBACK", "30"))
USE_SUPPLY_DEMAND = os.getenv("USE_SUPPLY_DEMAND", "1") == "1"
USE_VOLUME_CONFIRMATION = os.getenv("USE_VOLUME_CONFIRMATION", "1") == "1"
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "45"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "25"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
UPDATES_TIMEOUT = 30

LOOKBACK_D1 = os.getenv("LOOKBACK_D1", "120d")
LOOKBACK_H4 = os.getenv("LOOKBACK_H4", "90d")
LOOKBACK_M30 = os.getenv("LOOKBACK_M30", "20d")

EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))

STATE_PATH = os.getenv("STATE_PATH", "/tmp/state.json")

MIN_SCORE_TO_SIGNAL = float(os.getenv("MIN_SCORE_TO_SIGNAL", "7.8"))
MIN_SCORE_GAP = float(os.getenv("MIN_SCORE_GAP", "1.8"))

MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.010"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.0005"))

SR_LOOKBACK = int(os.getenv("SR_LOOKBACK", "60"))
FIB_LOOKBACK = int(os.getenv("FIB_LOOKBACK", "50"))
MOMENTUM_BARS = int(os.getenv("MOMENTUM_BARS", "6"))
VOLUME_LOOKBACK = int(os.getenv("VOLUME_LOOKBACK", "20"))
WYCKOFF_LOOKBACK = int(os.getenv("WYCKOFF_LOOKBACK", "50"))

DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "23"))
DAILY_REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "59"))

USE_SESSION_FILTER = os.getenv("USE_SESSION_FILTER", "0").strip() == "1"
SESSION_START_UTC = int(os.getenv("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.getenv("SESSION_END_UTC", "21"))

SCAN_TOP_N = int(os.getenv("SCAN_TOP_N", "3"))
MIN_CONF_SCAN = float(os.getenv("MIN_CONF_SCAN", "7.8"))

LEARN_RATE_WIN = float(os.getenv("LEARN_RATE_WIN", "0.10"))
LEARN_RATE_LOSS = float(os.getenv("LEARN_RATE_LOSS", "0.08"))
LEARN_RATE_BE = float(os.getenv("LEARN_RATE_BE", "0.02"))
WEIGHT_MIN = float(os.getenv("WEIGHT_MIN", "0.40"))
WEIGHT_MAX = float(os.getenv("WEIGHT_MAX", "2.40"))
# =========================
# TRADE STORAGE
# =========================

open_trades = []
closed_trades = []

stats = {
    "signals": 0,
    "closed": 0,
    "wins": 0,
    "losses": 0,
    "tp1": 0,
    "tp2": 0,
    "tp3": 0
}


def save_trades():
    data = {
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "stats": stats
    }

    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def load_trades():
    global open_trades, closed_trades, stats

    if not TRADES_FILE.exists():
        save_trades()
        return

    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    open_trades = data.get("open_trades", [])
    closed_trades = data.get("closed_trades", [])
    stats = data.get("stats", stats)


def add_trade(symbol, side, entry, sl, tp1, tp2, tp3, score):

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "score": score,
        "status": "OPEN",
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "opened_at": datetime.utcnow().isoformat()
    }

    open_trades.append(trade)
    stats["signals"] += 1

    save_trades()
SYMBOLS: Dict[str, str] = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),
    "XAG": os.getenv("XAG_SYMBOL", "SI=F"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30": os.getenv("US30_SYMBOL", "^DJI"),
    "GER40": os.getenv("GER40_SYMBOL", "^GDAXI"),
    "OILCASH": os.getenv("OILCASH_SYMBOL", "CL=F"),
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "EURUSD": os.getenv("EURUSD_SYMBOL", "EURUSD=X"),
    "USDJPY": os.getenv("USDJPY_SYMBOL", "USDJPY=X"),
}

CRYPTO_LABELS = {"BTC"}
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


# =========================
# HELPERS
# =========================
def is_crypto_label(label: str) -> bool:
    return label.upper() in CRYPTO_LABELS


def is_weekend_utc() -> bool:
    return time.gmtime().tm_wday in (5, 6)


def session_allowed_for_symbol(label: str) -> bool:
    if not USE_SESSION_FILTER:
        return True
    if is_crypto_label(label):
        return True
    hour = time.gmtime().tm_hour
    return SESSION_START_UTC <= hour < SESSION_END_UTC


def market_is_open_for_symbol(label: str) -> bool:
    if is_crypto_label(label):
        return True
    return not is_weekend_utc()


def _utc_date_str(ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_float(x, default=0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def price_decimals(label: str, px: float) -> int:
    label = (label or "").upper()
    if "JPY" in label:
        return 3
    if label in {"EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD"}:
        return 5
    if label in {"BTC"}:
        return 2
    if label in {"XAU", "XAG"}:
        return 2
    return 2 if abs(px) >= 100 else 5


def fmt_price(label: str, x: float, px_hint: float) -> str:
    return f"{x:.{price_decimals(label, px_hint)}f}"


# =========================
# STATE
# =========================
DEFAULT_FEATURE_WEIGHTS = {
    "rsi": 0.90,
    "macd": 0.90,
    "volume": 0.80,
    "momentum": 0.90,
    "volatility": 0.70,
    "fibonacci": 1.00,
    "sr": 1.00,
    "wyckoff": 1.25,
    "trend": 1.40,
    "liquidity": 1.35,
    "bos": 1.30,
    "choch": 1.20,
    "displacement": 1.15,
}


def load_state() -> dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
                st.setdefault("last_sent", {})
                st.setdefault("tg_offset", 0)
                st.setdefault("paused", False)
                st.setdefault("open_trades", {})
                st.setdefault("daily", {})
                st.setdefault("last_report_date", "")
                st.setdefault("weights_global", DEFAULT_FEATURE_WEIGHTS.copy())
                st.setdefault("weights_symbol", {})
                return st
    except Exception as e:
        logger.warning("Failed to load state: %s", e)

    return {
        "last_sent": {},
        "tg_offset": 0,
        "paused": False,
        "open_trades": {},
        "daily": {},
        "last_report_date": "",
        "weights_global": DEFAULT_FEATURE_WEIGHTS.copy(),
        "weights_symbol": {},
    }


def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)


def get_symbol_weights(state: dict, label: str) -> dict:
    state.setdefault("weights_global", DEFAULT_FEATURE_WEIGHTS.copy())
    state.setdefault("weights_symbol", {})
    if label not in state["weights_symbol"]:
        state["weights_symbol"][label] = DEFAULT_FEATURE_WEIGHTS.copy()

    out = {}
    for k in DEFAULT_FEATURE_WEIGHTS.keys():
        g = safe_float(state["weights_global"].get(k, DEFAULT_FEATURE_WEIGHTS[k]), DEFAULT_FEATURE_WEIGHTS[k])
        s = safe_float(state["weights_symbol"][label].get(k, DEFAULT_FEATURE_WEIGHTS[k]), DEFAULT_FEATURE_WEIGHTS[k])
        out[k] = (g + s) / 2.0
    return out


def update_weights_after_trade(state: dict, label: str, features_used: dict, outcome: str) -> None:
    state.setdefault("weights_global", DEFAULT_FEATURE_WEIGHTS.copy())
    state.setdefault("weights_symbol", {})
    state["weights_symbol"].setdefault(label, DEFAULT_FEATURE_WEIGHTS.copy())

    if outcome == "WIN":
        lr = LEARN_RATE_WIN
        direction = 1.0
    elif outcome == "LOSS":
        lr = LEARN_RATE_LOSS
        direction = -1.0
    else:
        lr = LEARN_RATE_BE
        direction = 0.5

    for k, raw_score in features_used.items():
        strength = abs(safe_float(raw_score))
        if strength <= 0:
            continue

        delta = lr * strength * direction
        g = safe_float(state["weights_global"].get(k, DEFAULT_FEATURE_WEIGHTS[k]), DEFAULT_FEATURE_WEIGHTS[k])
        s = safe_float(state["weights_symbol"][label].get(k, DEFAULT_FEATURE_WEIGHTS[k]), DEFAULT_FEATURE_WEIGHTS[k])

        state["weights_global"][k] = clamp(g + (delta * 0.35), WEIGHT_MIN, WEIGHT_MAX)
        state["weights_symbol"][label][k] = clamp(s + (delta * 0.65), WEIGHT_MIN, WEIGHT_MAX)
# =========================
# TRADE MONITOR
# =========================

def close_trade(trade, result):
    trade["status"] = "CLOSED"
    trade["closed_at"] = datetime.utcnow().isoformat()
    trade["result"] = result

    closed_trades.append(trade)
    open_trades.remove(trade)

    stats["closed"] += 1

    if result == "WIN":
        stats["wins"] += 1
    elif result == "LOSS":
        stats["losses"] += 1

    save_trades()


def monitor_trades():

    for trade in open_trades.copy():

        symbol = trade["symbol"]

        try:
            df_price = yf_download_safe(
                SYMBOLS[symbol],
                "5d",
                "5m"
            )

            if df_price is None or df_price.empty:
                continue

            price = float(df_price["Close"].iloc[-1])

        except Exception as e:
            logger.error(f"Monitor error {symbol}: {e}")
            continue


        if trade["side"] == "BUY":

            if price >= trade["tp1"] and not trade["tp1_hit"]:
                trade["tp1_hit"] = True
                stats["tp1"] += 1

            if price >= trade["tp2"] and not trade["tp2_hit"]:
                trade["tp2_hit"] = True
                stats["tp2"] += 1

            if price >= trade["tp3"]:
                stats["tp3"] += 1
                close_trade(trade, "WIN")
                continue

            if price <= trade["sl"]:
                close_trade(trade, "LOSS")
                continue


        elif trade["side"] == "SELL":

            if price <= trade["tp1"] and not trade["tp1_hit"]:
                trade["tp1_hit"] = True
                stats["tp1"] += 1

            if price <= trade["tp2"] and not trade["tp2_hit"]:
                trade["tp2_hit"] = True
                stats["tp2"] += 1

            if price <= trade["tp3"]:
                stats["tp3"] += 1
                close_trade(trade, "WIN")
                continue

            if price >= trade["sl"]:
                close_trade(trade, "LOSS")
                continue

# =========================
# TELEGRAM
# =========================
def tg_url(method: str) -> str:
    return TELEGRAM_BASE.format(token=BOT_TOKEN, method=method)


def tg_delete_webhook() -> None:
    try:
        r = requests.get(
            tg_url("deleteWebhook"),
            params={"drop_pending_updates": False},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            logger.info("deleteWebhook OK")
        else:
            logger.warning("deleteWebhook failed: %s | %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("deleteWebhook error: %s", e)


def tg_get_me() -> Optional[dict]:
    try:
        r = requests.get(tg_url("getMe"), timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return None
        return r.json().get("result")
    except Exception:
        return None


def tg_send_message(chat_id: str, text: str) -> bool:
    try:
        payload = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
        r = requests.post(tg_url("sendMessage"), json=payload, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            logger.error("sendMessage failed: %s | %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        logger.error("sendMessage error: %s", e)
        return False


def tg_send_photo(chat_id: str, caption: str, image_bytes: bytes) -> bool:
    try:
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": (caption or "")[:1000]}
        r = requests.post(tg_url("sendPhoto"), data=data, files=files, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            logger.error("sendPhoto failed: %s | %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        logger.error("sendPhoto error: %s", e)
        return False


def tg_get_updates(offset: int) -> dict:
    try:
        params = {
            "timeout": UPDATES_TIMEOUT,
            "offset": offset,
            "allowed_updates": json.dumps(["message", "channel_post"]),
        }
        r = requests.get(tg_url("getUpdates"), params=params, timeout=UPDATES_TIMEOUT + 10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("getUpdates error: %s", e)
        return {"ok": False, "result": []}


# =========================
# DATA + INDICATORS
# =========================
def yf_download_safe(symbol: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        needed = {"Open", "High", "Low", "Close"}
        if not needed.issubset(df.columns):
            return None
        if "Volume" not in df.columns:
            df["Volume"] = 0.0
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df.empty:
            return None
        return df
    except Exception as e:
        logger.error("yfinance failed %s %s %s: %s", symbol, period, interval, e)
        return None


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h = df["High"]
    l = df["Low"]
    c = df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def find_support_resistance(df: pd.DataFrame, lookback: int = 60) -> Tuple[float, float]:
    d = df.tail(lookback)
    support = float(d["Low"].min())
    resistance = float(d["High"].max())
    return support, resistance


def fib_zone(df: pd.DataFrame, side: str, lookback: int = 50) -> Optional[Tuple[float, float, float, float]]:
    if df is None or len(df) < lookback + 5:
        return None
    d = df.tail(lookback)
    swing_high = float(d["High"].max())
    swing_low = float(d["Low"].min())
    rng = swing_high - swing_low
    if rng <= 0:
        return None
    if side == "BUY":
        fib62 = swing_high - rng * 0.618
        fib79 = swing_high - rng * 0.786
    else:
        fib62 = swing_low + rng * 0.618
        fib79 = swing_low + rng * 0.786
    return float(fib62), float(fib79), swing_high, swing_low


# =========================
# WYCKOFF ENGINE
# =========================
@dataclass
class WyckoffSignal:
    phase: str
    spring: bool
    upthrust: bool
    buy_score: float
    sell_score: float
    range_high: float
    range_low: float
    volume_ratio: float


def detect_wyckoff(df: pd.DataFrame, lookback: int = 50) -> WyckoffSignal:
    if df is None or len(df) < lookback + 5:
        return WyckoffSignal(
            phase="neutral",
            spring=False,
            upthrust=False,
            buy_score=0.0,
            sell_score=0.0,
            range_high=0.0,
            range_low=0.0,
            volume_ratio=1.0,
        )

    d = df.tail(lookback).copy()

    range_high = float(d["High"].iloc[:-1].max())
    range_low = float(d["Low"].iloc[:-1].min())

    last_open = float(d["Open"].iloc[-1])
    last_high = float(d["High"].iloc[-1])
    last_low = float(d["Low"].iloc[-1])
    last_close = float(d["Close"].iloc[-1])

    vol = d["Volume"].fillna(0)
    vol_ma = float(vol.iloc[:-1].tail(20).mean()) if len(vol) >= 21 else float(vol.mean())
    last_vol = float(vol.iloc[-1]) if len(vol) else 0.0
    volume_ratio = (last_vol / (vol_ma + 1e-9)) if vol_ma > 0 else 1.0

    full_range = max(range_high - range_low, 1e-9)
    body = abs(last_close - last_open)

    spring = (last_low < range_low) and (last_close > range_low)
    upthrust = (last_high > range_high) and (last_close < range_high)

    buy_score = 0.0
    sell_score = 0.0
    phase = "neutral"

    recent_span = float(d["High"].max() - d["Low"].min())
    compression = full_range / (recent_span + 1e-9)

    if spring:
        buy_score += 2.5
        if volume_ratio >= 1.2:
            buy_score += 1.2
        if body > full_range * 0.10:
            buy_score += 0.8
        phase = "accumulation"

    if upthrust:
        sell_score += 2.5
        if volume_ratio >= 1.2:
            sell_score += 1.2
        if body > full_range * 0.10:
            sell_score += 0.8
        phase = "distribution"

    if phase == "neutral" and compression > 0.75:
        buy_score += 0.3
        sell_score += 0.3

    return WyckoffSignal(
        phase=phase,
        spring=bool(spring),
        upthrust=bool(upthrust),
        buy_score=float(buy_score),
        sell_score=float(sell_score),
        range_high=float(range_high),
        range_low=float(range_low),
        volume_ratio=float(volume_ratio),
    )


# =========================
# SMART MONEY / PRICE ACTION
# =========================
@dataclass
class StructureSignal:
    trend: str
    bos_buy: bool
    bos_sell: bool
    choch_buy: bool
    choch_sell: bool
    sweep_buy: bool
    sweep_sell: bool
    displacement_buy: bool
    displacement_sell: bool
    strength: float


def detect_trend_strength(df: pd.DataFrame, ema_fast: int = 20, ema_slow: int = 50) -> Tuple[str, float]:
    if df is None or len(df) < max(ema_fast, ema_slow) + 10:
        return "neutral", 0.0

    c = df["Close"]
    f = ema(c, ema_fast)
    s = ema(c, ema_slow)

    last_close = float(c.iloc[-1])
    last_f = float(f.iloc[-1])
    last_s = float(s.iloc[-1])

    slope_f = (float(f.iloc[-1]) - float(f.iloc[-5])) / (abs(float(f.iloc[-5])) + 1e-9)
    slope_s = (float(s.iloc[-1]) - float(s.iloc[-5])) / (abs(float(s.iloc[-5])) + 1e-9)

    if last_close > last_f > last_s and slope_f > 0 and slope_s > 0:
        return "bullish", 8.5
    if last_close < last_f < last_s and slope_f < 0 and slope_s < 0:
        return "bearish", 8.5
    if last_close > last_s:
        return "bullish", 5.5
    if last_close < last_s:
        return "bearish", 5.5
    return "neutral", 3.0


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20, atr_mult: float = 0.08) -> Tuple[bool, bool]:
    if df is None or len(df) < lookback + 5:
        return False, False

    d = df.tail(lookback + 2).copy()
    a = atr(d, 14).iloc[-1]
    a = float(a) if pd.notna(a) else 0.0
    if a <= 0:
        return False, False

    prev_high = float(d["High"].iloc[:-1].max())
    prev_low = float(d["Low"].iloc[:-1].min())

    last_high = float(d["High"].iloc[-1])
    last_low = float(d["Low"].iloc[-1])
    last_close = float(d["Close"].iloc[-1])

    sweep_buy = (last_low < (prev_low - atr_mult * a)) and (last_close > prev_low)
    sweep_sell = (last_high > (prev_high + atr_mult * a)) and (last_close < prev_high)
    return sweep_buy, sweep_sell


def detect_bos_choch(df: pd.DataFrame, swing_lookback: int = 20) -> Tuple[bool, bool, bool, bool]:
    if df is None or len(df) < swing_lookback + 10:
        return False, False, False, False

    d = df.tail(swing_lookback + 6).copy()
    last_close = float(d["Close"].iloc[-1])

    prev_high = float(d["High"].iloc[-(swing_lookback+1):-1].max())
    prev_low = float(d["Low"].iloc[-(swing_lookback+1):-1].min())

    bos_buy = last_close > prev_high
    bos_sell = last_close < prev_low

    recent = d.tail(6)
    recent_up = float(recent["Close"].iloc[-2]) > float(recent["Close"].iloc[-6])
    recent_down = float(recent["Close"].iloc[-2]) < float(recent["Close"].iloc[-6])

    choch_buy = recent_down and bos_buy
    choch_sell = recent_up and bos_sell

    return bos_buy, bos_sell, choch_buy, choch_sell


def detect_displacement(df: pd.DataFrame, body_mult: float = 1.5) -> Tuple[bool, bool]:
    if df is None or len(df) < 20:
        return False, False

    d = df.tail(20).copy()
    bodies = (d["Close"] - d["Open"]).abs()
    avg_body = float(bodies.iloc[:-1].mean())
    last_open = float(d["Open"].iloc[-1])
    last_close = float(d["Close"].iloc[-1])
    last_body = abs(last_close - last_open)

    if avg_body <= 0:
        return False, False

    bullish = (last_close > last_open) and (last_body >= body_mult * avg_body)
    bearish = (last_close < last_open) and (last_body >= body_mult * avg_body)
    return bullish, bearish


def analyze_structure(df: pd.DataFrame) -> StructureSignal:
    trend, strength = detect_trend_strength(df, EMA_FAST, EMA_SLOW)
    sweep_buy, sweep_sell = detect_liquidity_sweep(df, 20, 0.08)
    bos_buy, bos_sell, choch_buy, choch_sell = detect_bos_choch(df, 20)
    displacement_buy, displacement_sell = detect_displacement(df, 1.5)

    return StructureSignal(
        trend=trend,
        bos_buy=bos_buy,
        bos_sell=bos_sell,
        choch_buy=choch_buy,
        choch_sell=choch_sell,
        sweep_buy=sweep_buy,
        sweep_sell=sweep_sell,
        displacement_buy=displacement_buy,
        displacement_sell=displacement_sell,
        strength=float(strength),
    )


# =========================
# SCORING ENGINE
# =========================
def trend_bias_from_higher_tf(d1: pd.DataFrame, h4: pd.DataFrame) -> int:
    d1_fast = ema(d1["Close"], EMA_FAST)
    d1_slow = ema(d1["Close"], EMA_SLOW)
    h4_fast = ema(h4["Close"], EMA_FAST)
    h4_slow = ema(h4["Close"], EMA_SLOW)

    score = 0
    if d1_fast.iloc[-1] > d1_slow.iloc[-1]:
        score += 1
    elif d1_fast.iloc[-1] < d1_slow.iloc[-1]:
        score -= 1

    if h4_fast.iloc[-1] > h4_slow.iloc[-1]:
        score += 1
    elif h4_fast.iloc[-1] < h4_slow.iloc[-1]:
        score -= 1
    return score


def feature_scores(m30: pd.DataFrame, d1: pd.DataFrame, h4: pd.DataFrame, label: str) -> dict:
    close = m30["Close"]
    volume = m30["Volume"].fillna(0)

    last_close = safe_float(close.iloc[-1])
    last_atr = safe_float(atr(m30, ATR_PERIOD).iloc[-1])
    atr_pct = last_atr / (abs(last_close) + 1e-9)

    rsi_series = rsi(close, RSI_PERIOD)
    last_rsi = safe_float(rsi_series.iloc[-1], 50.0)

    macd_line, signal_line, hist = macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    last_macd = safe_float(macd_line.iloc[-1])
    last_signal = safe_float(signal_line.iloc[-1])
    prev_macd = safe_float(macd_line.iloc[-2]) if len(macd_line) >= 2 else last_macd
    prev_signal = safe_float(signal_line.iloc[-2]) if len(signal_line) >= 2 else last_signal
    last_hist = safe_float(hist.iloc[-1])

    vol_ma = safe_float(volume.tail(VOLUME_LOOKBACK).mean(), 0.0)
    last_vol = safe_float(volume.iloc[-1], 0.0)
    vol_ratio = (last_vol / (vol_ma + 1e-9)) if vol_ma > 0 else 1.0

    momentum_val = safe_float(last_close - safe_float(close.iloc[-1 - min(MOMENTUM_BARS, len(close)-1)]), 0.0)

    support, resistance = find_support_resistance(m30, SR_LOOKBACK)
    dist_to_support = abs(last_close - support) / (last_atr + 1e-9)
    dist_to_resistance = abs(last_close - resistance) / (last_atr + 1e-9)

    trend_bias = trend_bias_from_higher_tf(d1, h4)
    wy = detect_wyckoff(m30, lookback=WYCKOFF_LOOKBACK)
    st = analyze_structure(m30)

    buy_rsi = 0.0
    sell_rsi = 0.0
    if last_rsi <= 30:
        buy_rsi = 1.8
    elif last_rsi <= 35:
        buy_rsi = 1.2
    elif last_rsi >= 70:
        sell_rsi = 1.8
    elif last_rsi >= 65:
        sell_rsi = 1.2

    buy_macd = 0.0
    sell_macd = 0.0
    if prev_macd <= prev_signal and last_macd > last_signal:
        buy_macd = 1.6
    elif prev_macd >= prev_signal and last_macd < last_signal:
        sell_macd = 1.6
    else:
        if last_hist > 0:
            buy_macd = 0.7
        elif last_hist < 0:
            sell_macd = 0.7

    buy_volume = 0.0
    sell_volume = 0.0
    if vol_ratio >= 1.4:
        if momentum_val > 0:
            buy_volume = 1.2
        elif momentum_val < 0:
            sell_volume = 1.2

    buy_momentum = 0.0
    sell_momentum = 0.0
    if momentum_val > 0:
        buy_momentum = 1.1
    elif momentum_val < 0:
        sell_momentum = 1.1

    buy_volatility = 0.0
    sell_volatility = 0.0
    if MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT:
        buy_volatility = 0.8
        sell_volatility = 0.8
    else:
        buy_volatility = -0.8
        sell_volatility = -0.8

    buy_fib = 0.0
    sell_fib = 0.0
    buy_fib62 = buy_fib79 = None
    sell_fib62 = sell_fib79 = None

    fib_buy = fib_zone(m30, "BUY", FIB_LOOKBACK)
    fib_sell = fib_zone(m30, "SELL", FIB_LOOKBACK)

    if fib_buy:
        buy_fib62, buy_fib79, _, _ = fib_buy
        lo = min(buy_fib62, buy_fib79)
        hi = max(buy_fib62, buy_fib79)
        if lo <= last_close <= hi:
            buy_fib = 1.8

    if fib_sell:
        sell_fib62, sell_fib79, _, _ = fib_sell
        lo = min(sell_fib62, sell_fib79)
        hi = max(sell_fib62, sell_fib79)
        if lo <= last_close <= hi:
            sell_fib = 1.8

    buy_sr = 0.0
    sell_sr = 0.0
    if dist_to_support <= 1.0:
        buy_sr = 1.5
    if dist_to_resistance <= 1.0:
        sell_sr = 1.5

    buy_bias = 0.0
    sell_bias = 0.0
    if trend_bias >= 2:
        buy_bias = 1.2
    elif trend_bias <= -2:
        sell_bias = 1.2
    elif trend_bias == 1:
        buy_bias = 0.6
    elif trend_bias == -1:
        sell_bias = 0.6

    buy_trend = 0.0
    sell_trend = 0.0
    if st.trend == "bullish":
        buy_trend = 1.8 if st.strength >= 8 else 1.0
        if st.strength >= 8:
            sell_momentum -= 1.0
            sell_macd -= 0.8
            sell_sr -= 0.5
    elif st.trend == "bearish":
        sell_trend = 1.8 if st.strength >= 8 else 1.0
        if st.strength >= 8:
            buy_momentum -= 1.0
            buy_macd -= 0.8
            buy_sr -= 0.5

    buy_liquidity = 1.7 if st.sweep_buy else 0.0
    sell_liquidity = 1.7 if st.sweep_sell else 0.0

    buy_bos = 1.5 if st.bos_buy else 0.0
    sell_bos = 1.5 if st.bos_sell else 0.0

    buy_choch = 1.2 if st.choch_buy else 0.0
    sell_choch = 1.2 if st.choch_sell else 0.0

    buy_displacement = 1.1 if st.displacement_buy else 0.0
    sell_displacement = 1.1 if st.displacement_sell else 0.0

    return {
        "meta": {
            "last_close": last_close,
            "atr": last_atr,
            "atr_pct": atr_pct,
            "support": support,
            "resistance": resistance,
            "rsi": last_rsi,
            "vol_ratio": vol_ratio,
            "trend_bias": trend_bias,
            "buy_fib62": buy_fib62,
            "buy_fib79": buy_fib79,
            "sell_fib62": sell_fib62,
            "sell_fib79": sell_fib79,
            "wy_phase": wy.phase,
            "wy_spring": wy.spring,
            "wy_upthrust": wy.upthrust,
            "wy_range_high": wy.range_high,
            "wy_range_low": wy.range_low,
            "wy_volume_ratio": wy.volume_ratio,
            "smc_trend": st.trend,
            "smc_strength": st.strength,
            "smc_sweep_buy": st.sweep_buy,
            "smc_sweep_sell": st.sweep_sell,
            "smc_bos_buy": st.bos_buy,
            "smc_bos_sell": st.bos_sell,
            "smc_choch_buy": st.choch_buy,
            "smc_choch_sell": st.choch_sell,
            "smc_disp_buy": st.displacement_buy,
            "smc_disp_sell": st.displacement_sell,
        },
        "buy": {
            "rsi": buy_rsi,
            "macd": buy_macd,
            "volume": buy_volume,
            "momentum": buy_momentum,
            "volatility": buy_volatility,
            "fibonacci": buy_fib,
            "sr": buy_sr,
            "wyckoff": wy.buy_score,
            "trend": buy_trend,
            "liquidity": buy_liquidity,
            "bos": buy_bos,
            "choch": buy_choch,
            "displacement": buy_displacement,
            "bias": buy_bias,
        },
        "sell": {
            "rsi": sell_rsi,
            "macd": sell_macd,
            "volume": sell_volume,
            "momentum": sell_momentum,
            "volatility": sell_volatility,
            "fibonacci": sell_fib,
            "sr": sell_sr,
            "wyckoff": wy.sell_score,
            "trend": sell_trend,
            "liquidity": sell_liquidity,
            "bos": sell_bos,
            "choch": sell_choch,
            "displacement": sell_displacement,
            "bias": sell_bias,
        },
    }


# =========================
# PLAN
# =========================
@dataclass
class Plan:
    label: str
    symbol: str
    side: str
    timeframe: str
    entry_type: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    confidence: float
    risk_usd: float
    buy_score: float
    sell_score: float
    support: float
    resistance: float
    rsi_value: float
    atr_value: float
    fib_low: Optional[float]
    fib_high: Optional[float]
    features_used: Dict[str, float]
    weighted_contributions: Dict[str, float]
    wy_phase: str
    wy_spring: bool
    wy_upthrust: bool
    wy_range_high: float
    wy_range_low: float
    smc_trend: str
    smc_strength: float
    smc_sweep_buy: bool
    smc_sweep_sell: bool
    smc_bos_buy: bool
    smc_bos_sell: bool
    smc_choch_buy: bool
    smc_choch_sell: bool
    smc_disp_buy: bool
    smc_disp_sell: bool


def calc_rr_targets(entry: float, sl: float, side: str) -> Tuple[float, float, float]:
    r = abs(entry - sl)
    r = max(r, 1e-9)
    if side == "BUY":
        return entry + (2 * r), entry + (3 * r), entry + (4 * r)
    return entry - (2 * r), entry - (3 * r), entry - (4 * r)


def build_plan(state: dict, label: str, symbol: str) -> Optional[Plan]:
    d1 = yf_download_safe(symbol, LOOKBACK_D1, "1d")
    h4 = yf_download_safe(symbol, LOOKBACK_H4, "4h")
    m30 = yf_download_safe(symbol, LOOKBACK_M30, "30m")
    if d1 is None or h4 is None or m30 is None:
        return None

    feats = feature_scores(m30, d1, h4, label)
    meta = feats["meta"]
    buy_raw = feats["buy"]
    sell_raw = feats["sell"]

    weights = get_symbol_weights(state, label)

    buy_score = 0.0
    sell_score = 0.0
    buy_contrib = {}
    sell_contrib = {}

    for k in DEFAULT_FEATURE_WEIGHTS.keys():
        w = safe_float(weights.get(k, 1.0), 1.0)
        bv = safe_float(buy_raw.get(k, 0.0), 0.0)
        sv = safe_float(sell_raw.get(k, 0.0), 0.0)
        buy_contrib[k] = bv * w
        sell_contrib[k] = sv * w
        buy_score += buy_contrib[k]
        sell_score += sell_contrib[k]

    buy_score += safe_float(buy_raw.get("bias", 0.0))
    sell_score += safe_float(sell_raw.get("bias", 0.0))

    side = None
    if buy_score >= MIN_SCORE_TO_SIGNAL and (buy_score - sell_score) >= MIN_SCORE_GAP:
        side = "BUY"
    elif sell_score >= MIN_SCORE_TO_SIGNAL and (sell_score - buy_score) >= MIN_SCORE_GAP:
        side = "SELL"
    else:
        return None

    px = safe_float(meta["last_close"])
    a = safe_float(meta["atr"])
    atr_pct = safe_float(meta["atr_pct"])
    if a <= 0:
        return None
    if atr_pct > MAX_ATR_PCT or atr_pct < MIN_ATR_PCT:
        return None

    support = safe_float(meta["support"])
    resistance = safe_float(meta["resistance"])

    smc_trend = str(meta.get("smc_trend", "neutral"))
    smc_strength = float(meta.get("smc_strength", 0.0))

    if side == "SELL" and smc_trend == "bullish" and smc_strength >= 8.0:
        if not (
            bool(meta.get("smc_sweep_sell", False))
            and (
                bool(meta.get("smc_bos_sell", False))
                or bool(meta.get("smc_choch_sell", False))
            )
        ):
            return None

    if side == "BUY" and smc_trend == "bearish" and smc_strength >= 8.0:
        if not (
            bool(meta.get("smc_sweep_buy", False))
            and (
                bool(meta.get("smc_bos_buy", False))
                or bool(meta.get("smc_choch_buy", False))
            )
        ):
            return None

    fib_low = fib_high = None
    if side == "BUY" and meta["buy_fib62"] is not None and meta["buy_fib79"] is not None:
        fib_low = min(meta["buy_fib62"], meta["buy_fib79"])
        fib_high = max(meta["buy_fib62"], meta["buy_fib79"])
    elif side == "SELL" and meta["sell_fib62"] is not None and meta["sell_fib79"] is not None:
        fib_low = min(meta["sell_fib62"], meta["sell_fib79"])
        fib_high = max(meta["sell_fib62"], meta["sell_fib79"])

    entry_type = "MARKET"
    entry = px

    zone_low = fib_low
    zone_high = fib_high

    if zone_low is None or zone_high is None:
        if side == "BUY" and support > 0:
            zone_low = support - (0.20 * a)
            zone_high = support + (0.20 * a)
        elif side == "SELL" and resistance > 0:
            zone_low = resistance - (0.20 * a)
            zone_high = resistance + (0.20 * a)

    if zone_low is None or zone_high is None:
        return None

    zone_low = float(min(zone_low, zone_high))
    zone_high = float(max(zone_low, zone_high))
    zone_mid = (zone_low + zone_high) / 2.0

    if side == "SELL":
        if zone_low <= px <= zone_high:
            entry_type = "MARKET"
            entry = px
        elif px > zone_high:
            dist = (px - zone_high) / (a + 1e-9)
            if dist <= 1.0:
                entry_type = "LIMIT"
                entry = zone_mid
            else:
                return None
        else:
            return None
    else:
        if zone_low <= px <= zone_high:
            entry_type = "MARKET"
            entry = px
        elif px < zone_low:
            dist = (zone_low - px) / (a + 1e-9)
            if dist <= 1.0:
                entry_type = "LIMIT"
                entry = zone_mid
            else:
                return None
        else:
            return None

    if side == "BUY":
        base_sl = min(support, entry - 1.5 * a) if support > 0 else (entry - 1.5 * a)
        sl = min(base_sl, entry - 0.8 * a)
    else:
        base_sl = max(resistance, entry + 1.5 * a) if resistance > 0 else (entry + 1.5 * a)
        sl = max(base_sl, entry + 0.8 * a)

    if abs(entry - sl) < 0.25 * a:
        sl = (entry - 1.2 * a) if side == "BUY" else (entry + 1.2 * a)

    tp1, tp2, tp3 = calc_rr_targets(entry, sl, side)

    if side == "SELL" and px <= tp1:
        return None
    if side == "BUY" and px >= tp1:
        return None

    entry_distance_atr = abs(entry - px) / (a + 1e-9)
    if entry_type == "LIMIT" and entry_distance_atr > 1.2:
        return None

    confidence = clamp(max(buy_score, sell_score), 1.0, 10.0)

    features_used = buy_raw if side == "BUY" else sell_raw
    weighted_contributions = buy_contrib if side == "BUY" else sell_contrib

    return Plan(
        label=label,
        symbol=symbol,
        side=side,
        timeframe="M30",
        entry_type=entry_type,
        entry=float(entry),
        sl=float(sl),
        tp1=float(tp1),
        tp2=float(tp2),
        tp3=float(tp3),
        confidence=float(confidence),
        risk_usd=float(ACCOUNT_BALANCE * (RISK_PCT / 100.0)),
        buy_score=float(buy_score),
        sell_score=float(sell_score),
        support=float(support),
        resistance=float(resistance),
        rsi_value=float(meta["rsi"]),
        atr_value=float(a),
        fib_low=float(fib_low) if fib_low is not None else None,
        fib_high=float(fib_high) if fib_high is not None else None,
        features_used={k: float(v) for k, v in features_used.items() if k in DEFAULT_FEATURE_WEIGHTS},
        weighted_contributions={k: float(v) for k, v in weighted_contributions.items()},
        wy_phase=str(meta.get("wy_phase", "neutral")),
        wy_spring=bool(meta.get("wy_spring", False)),
        wy_upthrust=bool(meta.get("wy_upthrust", False)),
        wy_range_high=float(meta.get("wy_range_high", 0.0)),
        wy_range_low=float(meta.get("wy_range_low", 0.0)),
        smc_trend=str(meta.get("smc_trend", "neutral")),
        smc_strength=float(meta.get("smc_strength", 0.0)),
        smc_sweep_buy=bool(meta.get("smc_sweep_buy", False)),
        smc_sweep_sell=bool(meta.get("smc_sweep_sell", False)),
        smc_bos_buy=bool(meta.get("smc_bos_buy", False)),
        smc_bos_sell=bool(meta.get("smc_bos_sell", False)),
        smc_choch_buy=bool(meta.get("smc_choch_buy", False)),
        smc_choch_sell=bool(meta.get("smc_choch_sell", False)),
        smc_disp_buy=bool(meta.get("smc_disp_buy", False)),
        smc_disp_sell=bool(meta.get("smc_disp_sell", False)),
    )


# =========================
# CHART
# =========================
def render_chart(df: pd.DataFrame, plan: Plan) -> bytes:
    d = df.tail(120).copy()
    d["EMA_FAST"] = ema(d["Close"], EMA_FAST)
    d["EMA_SLOW"] = ema(d["Close"], EMA_SLOW)

    x = list(range(len(d)))
    o = d["Open"].values
    h = d["High"].values
    l = d["Low"].values
    c = d["Close"].values

    fig = plt.figure(figsize=(10, 6), dpi=120)
    ax = plt.gca()

    for i in range(len(d)):
        ax.plot([x[i], x[i]], [l[i], h[i]], linewidth=1)
        y0 = min(o[i], c[i])
        y1 = max(o[i], c[i])
        rect = plt.Rectangle((x[i] - 0.32, y0), 0.64, max(y1 - y0, 1e-9), fill=False, linewidth=1)
        ax.add_patch(rect)

    ax.plot(x, d["EMA_FAST"].values, linewidth=1.2, label=f"EMA{EMA_FAST}")
    ax.plot(x, d["EMA_SLOW"].values, linewidth=1.2, label=f"EMA{EMA_SLOW}")

    if plan.fib_low is not None and plan.fib_high is not None:
        ax.axhspan(plan.fib_low, plan.fib_high, alpha=0.08)

    if plan.wy_range_low > 0 and plan.wy_range_high > 0:
        ax.axhspan(plan.wy_range_low, plan.wy_range_high, alpha=0.05)

    ax.axhline(plan.support, linewidth=1.0, linestyle="--")
    ax.axhline(plan.resistance, linewidth=1.0, linestyle="--")
    ax.axhline(plan.entry, linewidth=1.2, linestyle="--")
    ax.axhline(plan.sl, linewidth=1.2, linestyle="--")
    ax.axhline(plan.tp1, linewidth=1.0, linestyle=":")
    ax.axhline(plan.tp2, linewidth=1.0, linestyle=":")
    ax.axhline(plan.tp3, linewidth=1.0, linestyle=":")

    ax.set_title(f"{plan.label} ({plan.symbol}) | {plan.timeframe} | {plan.side} {plan.entry_type}")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=9)

    idx = d.index
    step = max(1, len(d) // 6)
    ticks = list(range(0, len(d), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(idx[i])[:16] for i in ticks], rotation=15, ha="right", fontsize=8)

    fib_txt = "OFF"
    if plan.fib_low is not None and plan.fib_high is not None:
        fib_txt = f"{fmt_price(plan.label, plan.fib_low, plan.entry)} - {fmt_price(plan.label, plan.fib_high, plan.entry)}"

    wy_txt = plan.wy_phase
    if plan.wy_spring:
        wy_txt += " | Spring"
    if plan.wy_upthrust:
        wy_txt += " | Upthrust"

    top_feats = sorted(plan.weighted_contributions.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
    feat_txt = " | ".join([f"{k}:{v:+.2f}" for k, v in top_feats])

    analysis_text = (
        f"Institutional Adaptive\n"
        f"BUY:{plan.buy_score:.2f} | SELL:{plan.sell_score:.2f}\n"
        f"RSI: {plan.rsi_value:.1f}\n"
        f"Wyckoff: {wy_txt}\n"
        f"Trend: {plan.smc_trend} ({plan.smc_strength:.1f})\n"
        f"Sweep: {'BUY' if plan.smc_sweep_buy else 'SELL' if plan.smc_sweep_sell else '—'} | "
        f"BOS: {'BUY' if plan.smc_bos_buy else 'SELL' if plan.smc_bos_sell else '—'}\n"
        f"CHOCH: {'BUY' if plan.smc_choch_buy else 'SELL' if plan.smc_choch_sell else '—'} | "
        f"Disp: {'BUY' if plan.smc_disp_buy else 'SELL' if plan.smc_disp_sell else '—'}\n"
        f"Fib: {fib_txt}\n"
        f"Entry: {fmt_price(plan.label, plan.entry, plan.entry)}\n"
        f"SL: {fmt_price(plan.label, plan.sl, plan.entry)}\n"
        f"Top: {feat_txt}"
    )

    ax.text(
        0.01, 0.98, analysis_text,
        transform=ax.transAxes,
        fontsize=8.5,
        verticalalignment="top",
        bbox=dict(boxstyle="round", alpha=0.15)
    )

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================
# DAILY / TRADE MGMT
# =========================
def register_trade_for_daily(state: dict, plan: Plan) -> str:
    tid = hashlib.sha1(
        f"{plan.symbol}|{plan.side}|{plan.entry}|{time.time()}".encode("utf-8")
    ).hexdigest()[:18]

    now = time.time()
    state.setdefault("open_trades", {})[tid] = {
        "id": tid,
        "date": _utc_date_str(now),
        "ts": now,
        "symbol": plan.symbol,
        "label": plan.label,
        "side": plan.side,
        "entry": float(plan.entry),
        "sl": float(plan.sl),
        "initial_sl": float(plan.sl),
        "tp1": float(plan.tp1),
        "tp2": float(plan.tp2),
        "tp3": float(plan.tp3),
        "status": "OPEN",
        "tp1_done": False,
        "tp2_done": False,
        "tp3_done": False,
        "be_moved": False,
        "features_used": dict(plan.features_used),
    }

    day = _utc_date_str(now)
    daily = state.setdefault("daily", {}).setdefault(day, {
        "signals": 0,
        "avg_conf_sum": 0,
        "avg_conf_n": 0,
        "wins": 0,
        "losses": 0,
        "tp1": 0,
        "tp2": 0,
        "tp3": 0,
        "open": 0,
    })
    daily["signals"] += 1
    daily["avg_conf_sum"] += float(plan.confidence)
    daily["avg_conf_n"] += 1
    daily["open"] += 1
    return tid


def update_trade_outcomes(state: dict) -> None:
    open_trades = state.get("open_trades", {})
    if not open_trades:
        return

    for tid, tr in list(open_trades.items()):
        if tr.get("status") == "CLOSED":
            continue

        symbol = tr.get("symbol")
        label = tr.get("label", symbol)
        side = tr.get("side", "BUY")
        features_used = tr.get("features_used", {})

        df = yf_download_safe(symbol, "5d", "30m")
        if df is None or df.empty:
            continue

        ts = float(tr.get("ts", 0))
        if isinstance(df.index, pd.DatetimeIndex):
            epoch = (df.index.astype("int64") // 10**9).astype("int64")
            df2 = df.copy()
            df2["_epoch"] = epoch
            df2 = df2[df2["_epoch"] >= int(ts)]
        else:
            df2 = df

        if df2.empty:
            continue

        entry = float(tr["entry"])
        sl = float(tr["sl"])
        tp1 = float(tr["tp1"])
        tp2 = float(tr["tp2"])
        tp3 = float(tr["tp3"])

        tp1_done = bool(tr.get("tp1_done", False))
        tp2_done = bool(tr.get("tp2_done", False))
        be_moved = bool(tr.get("be_moved", False))

        for _, row in df2.iterrows():
            hi = float(row["High"])
            lo = float(row["Low"])

            if side == "BUY":
                if (not tp1_done) and hi >= tp1:
                    tr["tp1_done"] = True
                    tp1_done = True
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["tp1"] += 1
                    tg_send_message(CHAT_ID, f"🎯 TP1 HIT\n{label}\nTP1: {tp1}")

                    if not be_moved:
                        tr["sl"] = entry
                        sl = entry
                        tr["be_moved"] = True
                        be_moved = True
                        tg_send_message(CHAT_ID, f"🛡 BreakEven Activated\n{label}\nSL moved to Entry: {entry}")

                if tp1_done and (not tp2_done) and hi >= tp2:
                    tr["tp2_done"] = True
                    tp2_done = True
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["tp2"] += 1
                    tg_send_message(CHAT_ID, f"🚀 TP2 HIT\n{label}\nTP2: {tp2}")

                if tp2_done and hi >= tp3:
                    tr["tp3_done"] = True
                    tr["status"] = "CLOSED"
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["tp3"] += 1
                    daily["wins"] += 1
                    daily["open"] = max(0, int(daily["open"]) - 1)
                    update_weights_after_trade(state, label, features_used, "WIN")
                    tg_send_message(CHAT_ID, f"🏁 TP3 HIT - TRADE CLOSED\n{label}\nTP3: {tp3}")
                    break

                if lo <= sl:
                    tr["status"] = "CLOSED"
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["open"] = max(0, int(daily["open"]) - 1)

                    if be_moved:
                        update_weights_after_trade(state, label, features_used, "BE")
                        tg_send_message(CHAT_ID, f"⚖️ BreakEven Exit\n{label}\nExit at Entry: {entry}")
                    else:
                        daily["losses"] += 1
                        update_weights_after_trade(state, label, features_used, "LOSS")
                        tg_send_message(CHAT_ID, f"❌ SL HIT\n{label}\nSL: {sl}")
                    break

            else:
                if (not tp1_done) and lo <= tp1:
                    tr["tp1_done"] = True
                    tp1_done = True
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["tp1"] += 1
                    tg_send_message(CHAT_ID, f"🎯 TP1 HIT\n{label}\nTP1: {tp1}")

                    if not be_moved:
                        tr["sl"] = entry
                        sl = entry
                        tr["be_moved"] = True
                        be_moved = True
                        tg_send_message(CHAT_ID, f"🛡 BreakEven Activated\n{label}\nSL moved to Entry: {entry}")

                if tp1_done and (not tp2_done) and lo <= tp2:
                    tr["tp2_done"] = True
                    tp2_done = True
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["tp2"] += 1
                    tg_send_message(CHAT_ID, f"🚀 TP2 HIT\n{label}\nTP2: {tp2}")

                if tp2_done and lo <= tp3:
                    tr["tp3_done"] = True
                    tr["status"] = "CLOSED"
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["tp3"] += 1
                    daily["wins"] += 1
                    daily["open"] = max(0, int(daily["open"]) - 1)
                    update_weights_after_trade(state, label, features_used, "WIN")
                    tg_send_message(CHAT_ID, f"🏁 TP3 HIT - TRADE CLOSED\n{label}\nTP3: {tp3}")
                    break

                if hi >= sl:
                    tr["status"] = "CLOSED"
                    day = tr.get("date") or _utc_date_str(ts)
                    daily = state.setdefault("daily", {}).setdefault(day, {
                        "signals": 0, "avg_conf_sum": 0, "avg_conf_n": 0,
                        "wins": 0, "losses": 0, "tp1": 0, "tp2": 0, "tp3": 0, "open": 0,
                    })
                    daily["open"] = max(0, int(daily["open"]) - 1)

                    if be_moved:
                        update_weights_after_trade(state, label, features_used, "BE")
                        tg_send_message(CHAT_ID, f"⚖️ BreakEven Exit\n{label}\nExit at Entry: {entry}")
                    else:
                        daily["losses"] += 1
                        update_weights_after_trade(state, label, features_used, "LOSS")
                        tg_send_message(CHAT_ID, f"❌ SL HIT\n{label}\nSL: {sl}")
                    break


def format_daily_report(state: dict, day: Optional[str] = None) -> str:
    day = day or _utc_date_str()
    d = state.get("daily", {}).get(day)
    if not d:
        return f"📊 الحصيلة اليومية {day} (UTC)\nلا توجد إشارات مسجلة اليوم."

    avg_conf = (float(d.get("avg_conf_sum", 0)) / float(d.get("avg_conf_n", 1))) if d.get("avg_conf_n", 0) else 0.0
    wins = int(d.get("wins", 0))
    losses = int(d.get("losses", 0))
    open_ = int(d.get("open", 0))
    total = int(d.get("signals", 0))
    closed = wins + losses
    winrate = (wins / max(1, closed)) * 100.0

    return (
        f"📊 الحصيلة اليومية {day} (UTC)\n"
        f"— إشارات: {total}\n"
        f"— متوسط الثقة: {avg_conf:.1f}/10\n"
        f"— صفقات مُغلقة: {closed}\n"
        f"✅ Wins: {wins} | ❌ Losses: {losses} | WinRate: {winrate:.1f}%\n"
        f"🎯 TP1: {int(d.get('tp1', 0))} | TP2: {int(d.get('tp2', 0))} | TP3: {int(d.get('tp3', 0))}\n"
        f"⏳ Open: {open_}\n"
    )


def maybe_send_daily_report(state: dict) -> None:
    now = time.gmtime()
    day = _utc_date_str()
    last = str(state.get("last_report_date", ""))

    if now.tm_hour == DAILY_REPORT_HOUR and now.tm_min >= DAILY_REPORT_MINUTE and last != day:
        tg_send_message(CHAT_ID, format_daily_report(state, day))
        state["last_report_date"] = day
        save_state(state)


# =========================
# FORMAT / DEDUP
# =========================
def format_plan(plan: Plan) -> str:
    px_hint = plan.entry
    side_emoji = "🟢" if plan.side == "BUY" else "🔴"
    order = (
        "Buy Limit" if plan.side == "BUY" and plan.entry_type == "LIMIT"
        else "Sell Limit" if plan.side == "SELL" and plan.entry_type == "LIMIT"
        else "BUY Market" if plan.side == "BUY"
        else "SELL Market"
    )

    fib_txt = "OFF"
    if plan.fib_low is not None and plan.fib_high is not None:
        fib_txt = f"{fmt_price(plan.label, plan.fib_low, px_hint)} - {fmt_price(plan.label, plan.fib_high, px_hint)}"

    wy_txt = plan.wy_phase
    if plan.wy_spring:
        wy_txt += " | Spring ✅"
    if plan.wy_upthrust:
        wy_txt += " | Upthrust ✅"

    smc_txt = (
        f"SMC Trend: {plan.smc_trend} ({plan.smc_strength:.1f}/10)\n"
        f"Sweep: {'BUY' if plan.smc_sweep_buy else 'SELL' if plan.smc_sweep_sell else '—'} | "
        f"BOS: {'BUY' if plan.smc_bos_buy else 'SELL' if plan.smc_bos_sell else '—'} | "
        f"CHOCH: {'BUY' if plan.smc_choch_buy else 'SELL' if plan.smc_choch_sell else '—'} | "
        f"Disp: {'BUY' if plan.smc_disp_buy else 'SELL' if plan.smc_disp_sell else '—'}"
    )

    top_feats = sorted(plan.weighted_contributions.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
    top_txt = " | ".join([f"{k}:{v:+.2f}" for k, v in top_feats])

    return (
        f"🔥 VIP AI SIGNAL\n"
        f"📌 {plan.label} ({plan.symbol})\n"
        f"BUY SCORE: {plan.buy_score:.2f}\n"
        f"SELL SCORE: {plan.sell_score:.2f}\n"
        f"RSI({RSI_PERIOD}): {plan.rsi_value:.1f}\n"
        f"Wyckoff: {wy_txt}\n"
        f"{smc_txt}\n"
        f"Fib Zone: {fib_txt}\n\n"
        f"{side_emoji} {order}: {fmt_price(plan.label, plan.entry, px_hint)}\n"
        f"SL: {fmt_price(plan.label, plan.sl, px_hint)}\n"
        f"TP1: {fmt_price(plan.label, plan.tp1, px_hint)}\n"
        f"TP2: {fmt_price(plan.label, plan.tp2, px_hint)}\n"
        f"TP3: {fmt_price(plan.label, plan.tp3, px_hint)}\n\n"
        f"Support: {fmt_price(plan.label, plan.support, px_hint)}\n"
        f"Resistance: {fmt_price(plan.label, plan.resistance, px_hint)}\n"
        f"Risk: {RISK_PCT:.1f}% (~${plan.risk_usd:.2f})\n"
        f"Confidence: {plan.confidence:.1f}/10\n"
        f"Top Factors: {top_txt}\n"
        f"Model: Institutional Adaptive + Wyckoff + SMC\n"
    )


def signal_hash(plan: Plan) -> str:
    key = (
        f"{plan.symbol}|{plan.side}|{plan.entry_type}|{plan.entry}|{plan.sl}|"
        f"{plan.tp1}|{plan.tp2}|{plan.tp3}|buy={plan.buy_score:.3f}|sell={plan.sell_score:.3f}"
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def should_send(state: dict, plan: Plan) -> bool:
    now = time.time()
    rec = state.get("last_sent", {}).get(plan.symbol, {})
    last_ts = float(rec.get("ts", 0))
    last_hash = rec.get("hash", "")
    h = signal_hash(plan)
    cooldown = COOLDOWN_MINUTES * 60

    if (now - last_ts) < cooldown:
        return False
    if h == last_hash and (now - last_ts) < cooldown:
        return False
    return True


def mark_sent(state: dict, plan: Plan) -> None:
    state.setdefault("last_sent", {})[plan.symbol] = {
        "ts": time.time(),
        "hash": signal_hash(plan),
    }


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "✅ أوامر البوت:\n"
    "/help\n"
    "/status\n"
    "/symbols\n"
    "/analyze XAU\n"
    "/scan\n"
    "/daily\n"
    "/pause | /resume\n"
)

ALIASES = {
    "/xau": "XAU",
    "/xag": "XAG",
    "/us100": "US100",
    "/us30": "US30",
    "/ger40": "GER40",
    "/oil": "OILCASH",
    "/btc": "BTC",
    "/eurusd": "EURUSD",
    "/usdjpy": "USDJPY",
}


def _strip_botname(cmd: str) -> str:
    return cmd.split("@", 1)[0] if "@" in cmd else cmd


def handle_command(state: dict, update: dict, bot_username: Optional[str]) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", "")) if chat.get("id") is not None else ""
    if not chat_id:
        return

    parts = text.split()
    cmd_raw = parts[0].lower()
    cmd = _strip_botname(cmd_raw)

    if cmd in ALIASES:
        parts = ["/analyze", ALIASES[cmd]]
        cmd = "/analyze"

    if cmd in ("/help", "/halp"):
        tg_send_message(chat_id, HELP_TEXT)
        return

    if cmd == "/status":
        tg_send_message(
            chat_id,
            f"CHECK_INTERVAL_SEC={CHECK_INTERVAL_SEC}\n"
            f"COOLDOWN_MINUTES={COOLDOWN_MINUTES}\n"
            f"MIN_SCORE_TO_SIGNAL={MIN_SCORE_TO_SIGNAL}\n"
            f"MIN_SCORE_GAP={MIN_SCORE_GAP}\n"
            f"RSI_PERIOD={RSI_PERIOD}\n"
            f"FIB_LOOKBACK={FIB_LOOKBACK}\n"
            f"WYCKOFF_LOOKBACK={WYCKOFF_LOOKBACK}\n"
            f"USE_SESSION_FILTER={int(USE_SESSION_FILTER)}\n"
            f"USE_LEARNING=1\n"
            f"PAUSED={state.get('paused', False)}"
        )
        return

    if cmd == "/symbols":
        items = sorted(SYMBOLS.items(), key=lambda x: x[0])
        tg_send_message(chat_id, "Symbols: " + ", ".join([f"{k}={v}" for k, v in items]))
        return

    if cmd == "/pause":
        state["paused"] = True
        save_state(state)
        tg_send_message(chat_id, "⏸️ تم إيقاف الإشارات التلقائية.")
        return

    if cmd == "/resume":
        state["paused"] = False
        save_state(state)
        tg_send_message(chat_id, "▶️ تم تشغيل الإشارات التلقائية.")
        return

    if cmd == "/daily":
        update_trade_outcomes(state)
        tg_send_message(chat_id, format_daily_report(state))
        save_state(state)
        return

    if cmd == "/analyze" and len(parts) >= 2:
        sym_key = parts[1].upper().strip()
        if sym_key not in SYMBOLS:
            tg_send_message(chat_id, "❌ الرمز غير معروف. جرّب /symbols")
            return

        if not market_is_open_for_symbol(sym_key):
            tg_send_message(chat_id, f"🚫 السوق مغلق الآن لـ {sym_key}.")
            return

        if not session_allowed_for_symbol(sym_key):
            tg_send_message(chat_id, f"⏰ {sym_key} خارج الجلسة المسموح بها.")
            return

        plan = build_plan(state, sym_key, SYMBOLS[sym_key])
        if plan is None:
            tg_send_message(chat_id, f"⚠️ لا توجد إشارة حالياً لـ {sym_key}.")
            return

        m30 = yf_download_safe(plan.symbol, LOOKBACK_M30, "30m")
        if m30 is None or m30.empty:
            tg_send_message(chat_id, "⚠️ تعذر جلب بيانات الشارت.")
            return

        tg_send_message(chat_id, format_plan(plan))
        try:
            img = render_chart(m30, plan)
            ok = tg_send_photo(chat_id, f"📉 Technical Chart - {plan.label}", img)
            if not ok:
                tg_send_message(chat_id, f"⚠️ تعذر إرسال الشارت لـ {plan.label}")
        except Exception as e:
            tg_send_message(chat_id, f"⚠️ Chart error for {plan.label}: {e}")

        register_trade_for_daily(state, plan)
        save_state(state)
        return

    if cmd == "/scan":
        plans: List[Tuple[float, Plan]] = []
        for k, sym in SYMBOLS.items():
            if not market_is_open_for_symbol(k):
                continue
            if not session_allowed_for_symbol(k):
                continue
            p = build_plan(state, k, sym)
            if p is None:
                continue
            if p.confidence < MIN_CONF_SCAN:
                continue
            plans.append((p.confidence, p))

        if not plans:
            tg_send_message(chat_id, "⚠️ لا توجد إشارات قوية حالياً.")
            return

        plans.sort(key=lambda x: x[0], reverse=True)
        top = [p for _, p in plans[:max(1, SCAN_TOP_N)]]

        for i, plan in enumerate(top, start=1):
            m30 = yf_download_safe(plan.symbol, LOOKBACK_M30, "30m")
            if m30 is None or m30.empty:
                continue

            tg_send_message(chat_id, f"🔥 TOP AI SIGNAL #{i}\n\n" + format_plan(plan))
            try:
                img = render_chart(m30, plan)
                ok = tg_send_photo(chat_id, f"📉 Technical Chart - {plan.label}", img)
                if not ok:
                    tg_send_message(chat_id, f"⚠️ تعذر إرسال الشارت لـ {plan.label}")
            except Exception as e:
                tg_send_message(chat_id, f"⚠️ Chart error for {plan.label}: {e}")

            register_trade_for_daily(state, plan)
            save_state(state)
            time.sleep(1)
        return


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Missing BOT_TOKEN or CHAT_ID.")
        return

    me = tg_get_me()
    bot_username = me.get("username") if me else None
    if bot_username:
        logger.info("Bot username: @%s", bot_username)

    logger.info("Institutional Adaptive bot started | CHAT_ID=%s", CHAT_ID)

    tg_send_message(
        CHAT_ID,
        "✅ Institutional Adaptive Bot Online (RSI + MACD + Volume + Momentum + ATR + Fib + S/R + Wyckoff + SMC + Learning). اكتب /help"
    )

    last_check = 0.0
    
    tg_delete_webhook()

    state = load_state()

    load_trades()

    while True:
        try:

            # تحديث حالة الصفقات المفتوحة أولاً
            monitor_trades()

            # تحديث نتائج الصفقات في النظام اليومي
            update_trade_outcomes(state)

            # إرسال التقرير بعد تحديث النتائج
            maybe_send_daily_report(state)


            offset = int(state.get("tg_offset", 0))

            upd = tg_get_updates(offset)

            if upd.get("ok") and upd.get("result"):
                for u in upd["result"]:
                    state["tg_offset"] = u["update_id"] + 1
                    handle_command(state, u, bot_username)

                save_state(state)


            if state.get("paused", False):
                time.sleep(1)
                continue


            for label, sym in SYMBOLS.items():

                if not market_is_open_for_symbol(label):
                    continue

                if not session_allowed_for_symbol(label):
                    continue


                plan = build_plan(state, label, sym)

                if plan is None:
                    continue


                if not should_send(state, plan):
                    continue


                m30 = yf_download_safe(
                    plan.symbol,
                    LOOKBACK_M30,
                    "30m"
                )

                if m30 is None or m30.empty:
                    continue


                tg_send_message(
                    CHAT_ID,
                    format_plan(plan)
                )


                try:

                    img = render_chart(m30, plan)

                    ok = tg_send_photo(
                        CHAT_ID,
                        f"📉 Technical Chart - {plan.label}",
                        img
                    )

                    if not ok:
                        tg_send_message(
                            CHAT_ID,
                            f"⚠️ تعذر إرسال الشارت لـ {plan.label}"
                        )


                except Exception as e:

                    tg_send_message(
                        CHAT_ID,
                        f"⚠️ Chart error for {plan.label}: {e}"
                    )


                mark_sent(state, plan)

                # تسجيل الصفقة بعد الإرسال
                register_trade_for_daily(state, plan)

                save_state(state)

                time.sleep(1)


        except Exception as e:

            logger.exception(
                "Main loop error: %s",
                e
            )

            time.sleep(5)



if __name__ == "__main__":
    main()
