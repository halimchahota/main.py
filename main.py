# -*- coding: utf-8 -*-
import os
import io
import time
import json
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


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("vip_bot")


# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # @channel OR -100xxxxxxxxxx

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# MODE: vip_retest | vip_mix
MODE = os.getenv("MODE", "vip_retest").strip().lower()

# ATR / Zone params
RETEST_ATR = float(os.getenv("RETEST_ATR", "0.40"))
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.50"))
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
UPDATES_TIMEOUT = 30

# EMA
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))

# Data windows
LOOKBACK_D1 = os.getenv("LOOKBACK_D1", "200d")
LOOKBACK_H4 = os.getenv("LOOKBACK_H4", "140d")
LOOKBACK_M30 = os.getenv("LOOKBACK_M30", "45d")

# State persistence
STATE_PATH = os.getenv("STATE_PATH", "/tmp/state.json")

# Liquidity + BOS
USE_LIQ_BOS = os.getenv("USE_LIQ_BOS", "1").strip() == "1"
SCAN_TOP_N = int(os.getenv("SCAN_TOP_N", "3"))
MIN_CONF_SCAN = int(os.getenv("MIN_CONF_SCAN", "8"))

# Daily report (UTC)
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "23"))
DAILY_REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "59"))

# =========================
# VIP STRONGER FILTERS
# =========================
USE_SESSION_FILTER = os.getenv("USE_SESSION_FILTER", "1").strip() == "1"

# London + New York (UTC)
SESSION_START_UTC = int(os.getenv("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.getenv("SESSION_END_UTC", "21"))

# Crypto يعمل 24/7
CRYPTO_LABELS = {"BTC", "ETH"}

# Smart Entry
SMART_ENTRY = os.getenv("SMART_ENTRY", "1").strip() == "1"
MARKET_ATR_MAX = float(os.getenv("MARKET_ATR_MAX", "0.25"))
LIQ_FAVOR_LIMIT = os.getenv("LIQ_FAVOR_LIMIT", "1").strip() == "1"


# =========================
# SYMBOLS
# =========================
SYMBOLS: Dict[str, str] = {
    # --- FX ---
    "EURUSD": os.getenv("EURUSD_SYMBOL", "EURUSD=X"),
    "GBPUSD": os.getenv("GBPUSD_SYMBOL", "GBPUSD=X"),
    "USDJPY": os.getenv("USDJPY_SYMBOL", "USDJPY=X"),
    "AUDUSD": os.getenv("AUDUSD_SYMBOL", "AUDUSD=X"),
    "USDCAD": os.getenv("USDCAD_SYMBOL", "USDCAD=X"),
    "USDCHF": os.getenv("USDCHF_SYMBOL", "USDCHF=X"),
    "NZDUSD": os.getenv("NZDUSD_SYMBOL", "NZDUSD=X"),

    # --- Indices / Futures ---
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30":  os.getenv("US30_SYMBOL", "^DJI"),
    "SPX":   os.getenv("SPX_SYMBOL", "^GSPC"),
    "DAX":   os.getenv("DAX_SYMBOL", "^GDAXI"),
    "HK50":  os.getenv("HK50_SYMBOL", "^HSI"),

    # Requested additions
    "GER40CASH": os.getenv("GER40CASH_SYMBOL", "^GDAXI"),
    "BRENTCASH": os.getenv("BRENTCASH_SYMBOL", "BZ=F"),

    # --- Commodities ---
    "XAU":    os.getenv("XAU_SYMBOL", "GC=F"),
    "XAG":    os.getenv("XAG_SYMBOL", "SI=F"),
    "OIL":    os.getenv("OIL_SYMBOL", "CL=F"),
    "NG":     os.getenv("NG_SYMBOL", "NG=F"),
    "COPPER": os.getenv("COPPER_SYMBOL", "HG=F"),

    # --- Crypto ---
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "ETH": os.getenv("ETH_SYMBOL", "ETH-USD"),
}

TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


# =========================
# SESSION / MARKET FILTERS
# =========================
def is_crypto_label(label: str) -> bool:
    return label.upper() in CRYPTO_LABELS


def is_asia_allowed_label(label: str) -> bool:
    """
    Gold + indices + oil allowed in Asia too
    """
    asia_allowed = {
        "XAU",
        "US100",
        "US30",
        "SPX",
        "DAX",
        "HK50",
        "GER40CASH",
        "OIL",
        "BRENTCASH",
    }
    return label.upper() in asia_allowed


def session_allowed_for_symbol(label: str) -> bool:
    """
    Crypto: 24/7
    Gold + indices + oil: allowed in Asia too
    Forex + other commodities: London + New York only
    """
    if not USE_SESSION_FILTER:
        return True

    if is_crypto_label(label):
        return True

    if is_asia_allowed_label(label):
        return True

    now = time.gmtime()
    hour = now.tm_hour
    return SESSION_START_UTC <= hour < SESSION_END_UTC


def is_weekend_utc() -> bool:
    # Monday=0 ... Sunday=6
    wd = time.gmtime().tm_wday
    return wd in (5, 6)  # Saturday, Sunday


def market_is_open_for_symbol(label: str) -> bool:
    """
    Crypto: always open
    All non-crypto: closed on weekend
    """
    if is_crypto_label(label):
        return True

    if is_weekend_utc():
        return False

    return True


# =========================
# PRICE FORMATTING
# =========================
def price_decimals(label: str, px: float) -> int:
    label = (label or "").upper()
    if "JPY" in label:
        return 3

    fx_5 = {
        "EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD",
        "EURGBP", "EURCHF", "EURAUD", "EURCAD", "GBPCHF"
    }
    if label in fx_5:
        return 5

    if label in ("BTC", "ETH", "SOL"):
        return 2

    return 2 if abs(px) >= 100 else 5


def fmt_price(label: str, x: float, px_hint: float) -> str:
    d = price_decimals(label, px_hint)
    return f"{x:.{d}f}"


# =========================
# STATE + DAILY HELPERS
# =========================
def _utc_date_str(ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


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
    }


def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)


# =========================
# TELEGRAM
# =========================
def tg_url(method: str) -> str:
    return TELEGRAM_BASE.format(token=BOT_TOKEN, method=method)


def tg_delete_webhook() -> None:
    try:
        r = requests.get(tg_url("deleteWebhook"), params={"drop_pending_updates": False}, timeout=REQUEST_TIMEOUT)
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
        caption = (caption or "")[:1000]
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption}
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
# MARKET DATA
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


def last_price(df: pd.DataFrame) -> float:
    return float(df["Close"].iloc[-1])


def trend_score(df: pd.DataFrame) -> int:
    c = df["Close"]
    if len(c) < max(EMA_FAST, EMA_SLOW) + 10:
        return 0
    f = ema(c, EMA_FAST)
    s = ema(c, EMA_SLOW)
    slope = (f.iloc[-1] - f.iloc[-6]) / (abs(f.iloc[-6]) + 1e-9)
    if f.iloc[-1] > s.iloc[-1] and slope > 0:
        return +1
    if f.iloc[-1] < s.iloc[-1] and slope < 0:
        return -1
    return 0


# =========================
# LIQUIDITY + BOS
# =========================
def detect_liq_sweep(df: pd.DataFrame, a: float) -> bool:
    if df is None or len(df) < 60 or a <= 0:
        return False
    d = df.tail(80).copy()
    hi20 = float(d["High"].iloc[-21:-1].max())
    lo20 = float(d["Low"].iloc[-21:-1].min())
    last_h = float(d["High"].iloc[-1])
    last_l = float(d["Low"].iloc[-1])
    last_c = float(d["Close"].iloc[-1])

    sweep_high = (last_h > hi20 + 0.05 * a) and (last_c < hi20)
    sweep_low = (last_l < lo20 - 0.05 * a) and (last_c > lo20)
    return bool(sweep_high or sweep_low)


def detect_bos(df: pd.DataFrame, side: str) -> bool:
    if df is None or len(df) < 60:
        return False
    d = df.tail(80).copy()
    swing_high = float(d["High"].iloc[-21:-1].max())
    swing_low = float(d["Low"].iloc[-21:-1].min())
    last_c = float(d["Close"].iloc[-1])
    return (last_c > swing_high) if side == "BUY" else (last_c < swing_low)


# =========================
# ZONES
# =========================
def find_orderblock(df: pd.DataFrame, direction: str) -> Optional[Tuple[float, float]]:
    if len(df) < 60:
        return None
    d = df.tail(90).copy()
    a = atr(d, 14).iloc[-1]
    if a is None or pd.isna(a) or a <= 0:
        return None
    a = float(a)

    o = d["Open"].values
    c = d["Close"].values
    h = d["High"].values
    l = d["Low"].values

    for i in range(len(d) - 10, 10, -1):
        body = abs(c[i] - o[i])
        if body < 0.8 * a:
            continue

        if direction == "BUY":
            if c[i] > o[i] and c[i] > h[i - 1] and c[i - 1] < o[i - 1]:
                return float(l[i - 1]), float(h[i - 1])
        else:
            if c[i] < o[i] and c[i] < l[i - 1] and c[i - 1] > o[i - 1]:
                return float(l[i - 1]), float(h[i - 1])
    return None


def find_fvg(df: pd.DataFrame, direction: str) -> Optional[Tuple[float, float]]:
    if len(df) < 60:
        return None
    d = df.tail(160).copy().reset_index(drop=True)
    px = float(d["Close"].iloc[-1])

    zones = []
    for i in range(2, len(d) - 2):
        h_prev2 = float(d.loc[i - 2, "High"])
        l_prev2 = float(d.loc[i - 2, "Low"])
        h_i = float(d.loc[i, "High"])
        l_i = float(d.loc[i, "Low"])

        if direction == "BUY":
            if l_i > h_prev2:
                zones.append((h_prev2, l_i))
        else:
            if h_i < l_prev2:
                zones.append((h_i, l_prev2))

    if not zones:
        return None

    best = None
    best_d = 1e18
    for z0, z1 in zones[-25:]:
        mid = (z0 + z1) / 2.0
        dist = abs(px - mid)
        if dist < best_d:
            best_d = dist
            best = (float(min(z0, z1)), float(max(z0, z1)))
    return best


# =========================
# PLAN
# =========================
@dataclass
class Plan:
    label: str
    symbol: str
    timeframe: str
    trend_d1: int
    trend_h4: int
    overall: int
    side: str
    entry_type: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    zone_name: str
    zone_low: Optional[float]
    zone_high: Optional[float]
    atr_value: float
    confidence: int
    risk_usd: float
    liq: bool
    bos: bool


def calc_rr_targets(entry: float, sl: float, side: str) -> Tuple[float, float, float]:
    r = abs(entry - sl)
    r = max(r, 1e-9)
    if side == "BUY":
        return entry + 1*r, entry + 2*r, entry + 3*r
    else:
        return entry - 1*r, entry - 2*r, entry - 3*r


def build_plan(label: str, symbol: str) -> Optional[Plan]:
    d1 = yf_download_safe(symbol, LOOKBACK_D1, "1d")
    h4 = yf_download_safe(symbol, LOOKBACK_H4, "4h")
    m30 = yf_download_safe(symbol, LOOKBACK_M30, "30m")
    if d1 is None or h4 is None or m30 is None:
        return None

    t_d1 = trend_score(d1)
    t_h4 = trend_score(h4)
    overall = t_d1 + t_h4

    if overall >= 1:
        side = "BUY"
    elif overall <= -1:
        side = "SELL"
    else:
        return None

    px = last_price(m30)
    a = atr(m30, 14).iloc[-1]
    if a is None or pd.isna(a) or a <= 0:
        return None
    a = float(a)

    if (a / (abs(px) + 1e-9)) > MAX_ATR_PCT:
        return None

    liq = detect_liq_sweep(m30, a)
    bos = detect_bos(m30, side)

    if USE_LIQ_BOS and not (liq or bos):
        return None

    direction = "BUY" if side == "BUY" else "SELL"
    ob = find_orderblock(m30, direction)
    fvg = find_fvg(m30, direction)

    zone_name = "None"
    zone = None
    if ob:
        zone_name = "OrderBlock"
        zone = ob
    elif fvg:
        zone_name = "FVG"
        zone = fvg

    if zone is None:
        return None

    zone_low, zone_high = float(min(zone[0], zone[1])), float(max(zone[0], zone[1]))
    mid = (zone_low + zone_high) / 2.0
    dist_atr = abs(px - mid) / (a + 1e-9)

    entry_type = "LIMIT"
    entry = mid

    strong_trend = abs(overall) == 2
    allow_market = SMART_ENTRY and bos and strong_trend and (dist_atr <= MARKET_ATR_MAX)

    if LIQ_FAVOR_LIMIT and liq and not bos:
        allow_market = False

    if MODE == "vip_retest":
        if dist_atr > RETEST_ATR:
            return None

        if allow_market:
            entry_type = "MARKET"
            entry = px
        else:
            entry_type = "LIMIT"
            entry = mid
    else:
        if allow_market:
            entry_type = "MARKET"
            entry = px
        elif dist_atr <= MAX_PENDING_DISTANCE_ATR:
            entry_type = "LIMIT"
            entry = mid
        else:
            return None

    if side == "BUY":
        sl = zone_low - (SL_BUFFER_ATR * a)
    else:
        sl = zone_high + (SL_BUFFER_ATR * a)

    min_r = 0.25 * a
    if abs(entry - sl) < min_r:
        sl = (entry - 1.5 * a) if side == "BUY" else (entry + 1.5 * a)

    tp1, tp2, tp3 = calc_rr_targets(entry, sl, side)

    conf = 5
    conf += 2 if strong_trend else 1
    conf += 2 if zone_name in ("OrderBlock", "FVG") else 0
    conf += 1 if liq else 0
    conf += 1 if bos else 0
    conf += 1 if entry_type == "MARKET" and bos else 0
    conf = int(max(1, min(10, conf)))

    risk_usd = ACCOUNT_BALANCE * (RISK_PCT / 100.0)

    return Plan(
        label=label,
        symbol=symbol,
        timeframe="M30",
        trend_d1=t_d1,
        trend_h4=t_h4,
        overall=overall,
        side=side,
        entry_type=entry_type,
        entry=float(entry),
        sl=float(sl),
        tp1=float(tp1),
        tp2=float(tp2),
        tp3=float(tp3),
        zone_name=zone_name,
        zone_low=float(zone_low),
        zone_high=float(zone_high),
        atr_value=float(a),
        confidence=conf,
        risk_usd=float(risk_usd),
        liq=bool(liq),
        bos=bool(bos),
    )


# =========================
# CHART IMAGE
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

    fig = plt.figure(figsize=(10.5, 6.2), dpi=150)
    ax = plt.gca()

    for i in range(len(d)):
        ax.plot([x[i], x[i]], [l[i], h[i]], linewidth=1)
        y0 = min(o[i], c[i])
        y1 = max(o[i], c[i])
        rect = plt.Rectangle((x[i] - 0.35, y0), 0.7, max(y1 - y0, 1e-9), fill=False, linewidth=1)
        ax.add_patch(rect)

    ax.plot(x, d["EMA_FAST"].values, linewidth=1.2, label=f"EMA{EMA_FAST}")
    ax.plot(x, d["EMA_SLOW"].values, linewidth=1.2, label=f"EMA{EMA_SLOW}")

    if plan.zone_low is not None and plan.zone_high is not None:
        ax.axhspan(plan.zone_low, plan.zone_high, alpha=0.15)

    ax.axhline(plan.entry, linewidth=1.2)
    ax.axhline(plan.sl, linewidth=1.2)
    ax.axhline(plan.tp1, linewidth=1.0)
    ax.axhline(plan.tp2, linewidth=1.0)
    ax.axhline(plan.tp3, linewidth=1.0)

    ax.set_title(f"{plan.label} ({plan.symbol}) | {plan.timeframe} | {plan.side} {plan.entry_type}")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=9)

    idx = d.index
    step = max(1, len(d) // 6)
    ticks = list(range(0, len(d), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(idx[i])[:16] for i in ticks], rotation=15, ha="right", fontsize=8)

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================
# DAILY PERFORMANCE
# =========================
def _hit_in_bar(high_: float, low_: float, level: float) -> bool:
    return low_ <= level <= high_


def register_trade_for_daily(state: dict, plan: Plan) -> str:
    tid = hashlib.sha1(f"{plan.symbol}|{plan.side}|{plan.entry}|{time.time()}".encode("utf-8")).hexdigest()[:18]
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
        "tp1": float(plan.tp1),
        "tp2": float(plan.tp2),
        "tp3": float(plan.tp3),
        "status": "OPEN",
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
    daily["avg_conf_sum"] += int(plan.confidence)
    daily["avg_conf_n"] += 1
    daily["open"] += 1
    return tid


def update_trade_outcomes(state: dict) -> None:
    open_trades = state.get("open_trades", {})
    if not open_trades:
        return

    for tid, tr in list(open_trades.items()):
        if tr.get("status") != "OPEN":
            continue

        symbol = tr.get("symbol")
        if not symbol:
            continue

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

        sl = float(tr.get("sl", 0))
        tp1 = float(tr.get("tp1", 0))
        tp2 = float(tr.get("tp2", 0))
        tp3 = float(tr.get("tp3", 0))

        resolved = None
        for _, row in df2.iterrows():
            hi = float(row["High"])
            lo = float(row["Low"])

            sl_hit = _hit_in_bar(hi, lo, sl)
            tp1_hit = _hit_in_bar(hi, lo, tp1)
            tp2_hit = _hit_in_bar(hi, lo, tp2)
            tp3_hit = _hit_in_bar(hi, lo, tp3)

            if sl_hit:
                resolved = "SL"
                break
            if tp3_hit:
                resolved = "TP3"
                break
            if tp2_hit:
                resolved = "TP2"
                break
            if tp1_hit:
                resolved = "TP1"
                break

        if resolved:
            tr["status"] = resolved
            day = tr.get("date") or _utc_date_str(ts)
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
            daily["open"] = max(0, int(daily.get("open", 0)) - 1)

            if resolved == "SL":
                daily["losses"] += 1
            else:
                daily["wins"] += 1
                if resolved == "TP1":
                    daily["tp1"] += 1
                elif resolved == "TP2":
                    daily["tp2"] += 1
                elif resolved == "TP3":
                    daily["tp3"] += 1


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
    winrate = (wins / max(1, closed))) * 100.0

    return (
        f"📊 الحصيلة اليومية {day} (UTC)\n"
        f"— إشارات: {total}\n"
        f"— متوسط الثقة: {avg_conf:.1f}/10\n"
        f"— صفقات مُغلقة: {closed}\n"
        f"✅ Wins: {wins} | ❌ Losses: {losses} | WinRate: {winrate:.1f}%\n"
        f"🎯 TP1: {int(d.get('tp1', 0))} | TP2: {int(d.get('tp2', 0))} | TP3: {int(d.get('tp3', 0))}\n"
        f"⏳ Open: {open_}\n\n"
        f"ملاحظة: التقييم تقريبي اعتماداً على شموع Yahoo M30."
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
# FORMAT + DEDUP
# =========================
def format_plan(plan: Plan) -> str:
    px_hint = plan.entry
    side_emoji = "🟢" if plan.side == "BUY" else "🔴"

    if plan.entry_type == "LIMIT":
        order = "Buy Limit" if plan.side == "BUY" else "Sell Limit"
    else:
        order = "BUY Market" if plan.side == "BUY" else "SELL Market"

    zone_txt = "—"
    if plan.zone_low is not None and plan.zone_high is not None:
        zone_txt = f"{plan.zone_name} [{fmt_price(plan.label, plan.zone_low, px_hint)} - {fmt_price(plan.label, plan.zone_high, px_hint)}]"

    liq_txt = "✅" if plan.liq else "❌"
    bos_txt = "✅" if plan.bos else "❌"

    return (
        f"🔥 VIP M30\n"
        f"📌 {plan.label} ({plan.symbol})\n"
        f"Trend D1/H4: {plan.trend_d1:+d} / {plan.trend_h4:+d} => Overall: {plan.overall:+d}\n"
        f"Liq Sweep: {liq_txt} | BOS: {bos_txt}\n\n"
        f"{side_emoji} {order}: {fmt_price(plan.label, plan.entry, px_hint)}\n"
        f"SL: {fmt_price(plan.label, plan.sl, px_hint)}\n"
        f"TP1: {fmt_price(plan.label, plan.tp1, px_hint)}\n"
        f"TP2: {fmt_price(plan.label, plan.tp2, px_hint)}\n"
        f"TP3: {fmt_price(plan.label, plan.tp3, px_hint)}\n\n"
        f"Zone: {zone_txt}\n"
        f"Risk: {RISK_PCT:.1f}% (~${plan.risk_usd:.2f})\n"
        f"Confidence: {plan.confidence}/10\n"
        f"Mode: {MODE} | SMART_ENTRY={int(SMART_ENTRY)}\n"
    )


def signal_hash(plan: Plan) -> str:
    key = (
        f"{plan.symbol}|{plan.side}|{plan.entry_type}|{plan.entry}|{plan.sl}|"
        f"{plan.tp1}|{plan.tp2}|{plan.tp3}|{plan.zone_name}|{plan.zone_low}|{plan.zone_high}|"
        f"liq={plan.liq}|bos={plan.bos}"
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
    state.setdefault("last_sent", {})[plan.symbol] = {"ts": time.time(), "hash": signal_hash(plan)}


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "✅ أوامر VIP:\n"
    "/help\n"
    "/status\n"
    "/symbols\n"
    "/mode vip_retest  أو  /mode vip_mix\n"
    "/analyze XAU  (أو BTC / US100 / US30 / OIL / GER40CASH / BRENTCASH ...)\n"
    "/scan  (TOP إشارات)\n"
    "/daily (الحصيلة اليومية)\n"
    "/pause  |  /resume\n"
)

ALIASES = {
    "/xau": "XAU",
    "/btc": "BTC",
    "/eth": "ETH",
    "/us30": "US30",
    "/us100": "US100",
    "/oil": "OIL",
    "/eurusd": "EURUSD",
    "/gbpusd": "GBPUSD",
    "/usdjpy": "USDJPY",
    "/ger40": "GER40CASH",
    "/brent": "BRENTCASH",
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

    global MODE

    if cmd in ("/help", "/halp"):
        tg_send_message(chat_id, HELP_TEXT)
        return

    if cmd == "/status":
        tg_send_message(
            chat_id,
            f"MODE={MODE}\nCHECK_INTERVAL_SEC={CHECK_INTERVAL_SEC}\nCOOLDOWN_MINUTES={COOLDOWN_MINUTES}\n"
            f"RETEST_ATR={RETEST_ATR}\nMAX_PENDING_DISTANCE_ATR={MAX_PENDING_DISTANCE_ATR}\n"
            f"SL_BUFFER_ATR={SL_BUFFER_ATR}\nMAX_ATR_PCT={MAX_ATR_PCT}\n"
            f"SMART_ENTRY={int(SMART_ENTRY)} | MARKET_ATR_MAX={MARKET_ATR_MAX}\n"
            f"USE_SESSION_FILTER={int(USE_SESSION_FILTER)}\n"
            f"SESSION_UTC={SESSION_START_UTC:02d}:00 -> {SESSION_END_UTC:02d}:00\n"
            f"ASIA_ALLOWED=XAU,US100,US30,SPX,DAX,HK50,GER40CASH,OIL,BRENTCASH\n"
            f"WEEKEND_FILTER=1\n"
            f"USE_LIQ_BOS={int(USE_LIQ_BOS)}\n"
            f"MIN_CONF_SCAN={MIN_CONF_SCAN}\n"
            f"DAILY_REPORT_UTC={DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d}\n"
            f"RISK_PCT={RISK_PCT}\nPAUSED={state.get('paused', False)}"
        )
        return

    if cmd == "/symbols":
        items = sorted(SYMBOLS.items(), key=lambda x: x[0])
        tg_send_message(chat_id, "Symbols: " + ", ".join([f"{k}={v}" for k, v in items]))
        return

    if cmd == "/mode" and len(parts) >= 2:
        m = parts[1].strip().lower()
        if m in ("vip_retest", "vip_mix"):
            MODE = m
            tg_send_message(chat_id, f"✅ MODE set to {MODE}")
        else:
            tg_send_message(chat_id, "❌ mode يجب أن يكون vip_retest أو vip_mix")
        return

    if cmd == "/pause":
        state["paused"] = True
        save_state(state)
        tg_send_message(chat_id, "⏸️ تم إيقاف الإشارات التلقائية مؤقتاً.")
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
            tg_send_message(chat_id, f"⏰ {sym_key} خارج جلسة التداول المسموح بها الآن.")
            return

        plan = build_plan(sym_key, SYMBOLS[sym_key])
        if plan is None:
            tg_send_message(chat_id, f"⚠️ لا توجد إشارة حالياً لـ {sym_key} حسب شروط VIP.")
            return

        m30 = yf_download_safe(plan.symbol, LOOKBACK_M30, "30m")
        if m30 is None or m30.empty:
            tg_send_message(chat_id, "⚠️ تعذر جلب بيانات الشارت.")
            return

        caption = format_plan(plan)
        img = render_chart(m30, plan)
        if not tg_send_photo(chat_id, caption, img):
            tg_send_message(chat_id, caption)

        register_trade_for_daily(state, plan)
        save_state(state)
        return

    if cmd == "/scan":
        plans: List[Tuple[int, Plan]] = []

        for k, sym in SYMBOLS.items():
            if not market_is_open_for_symbol(k):
                continue
            if not session_allowed_for_symbol(k):
                continue

            p = build_plan(k, sym)
            if p is None:
                continue
            if p.confidence < MIN_CONF_SCAN:
                continue

            score = p.confidence + (1 if p.liq else 0) + (1 if p.bos else 0)
            plans.append((score, p))

        if not plans:
            tg_send_message(chat_id, "⚠️ لا توجد إشارات قوية حالياً.")
            return

        plans.sort(key=lambda x: x[0], reverse=True)
        top = [p for _, p in plans[:max(1, SCAN_TOP_N)]]

        for i, plan in enumerate(top, start=1):
            m30 = yf_download_safe(plan.symbol, LOOKBACK_M30, "30m")
            if m30 is None or m30.empty:
                continue

            caption = f"🔥 TOP VIP SIGNAL #{i}\n\n" + format_plan(plan)
            img = render_chart(m30, plan)

            if not tg_send_photo(chat_id, caption, img):
                tg_send_message(chat_id, caption)

            register_trade_for_daily(state, plan)
            save_state(state)
            time.sleep(1)
        return


# =========================
# MAIN LOOP
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Missing BOT_TOKEN or CHAT_ID in Railway Variables.")
        return

    tg_delete_webhook()

    me = tg_get_me()
    bot_username = me.get("username") if me else None
    if bot_username:
        logger.info("Bot username: @%s", bot_username)

    state = load_state()
    logger.info(
        "VIP bot started. MODE=%s | SMART_ENTRY=%s | SESSION_FILTER=%s | CHAT_ID=%s",
        MODE, int(SMART_ENTRY), int(USE_SESSION_FILTER), CHAT_ID
    )

    tg_send_message(CHAT_ID, "✅ VIP Bot Online (VIP Strong + Smart Entry + Smart Session + Asia Gold/Indices/Oil + Weekend Filter + Daily). اكتب /help")

    last_check = 0.0

    while True:
        try:
            update_trade_outcomes(state)
            maybe_send_daily_report(state)

            # Commands
            offset = int(state.get("tg_offset", 0))
            upd = tg_get_updates(offset)
            if upd.get("ok") and upd.get("result"):
                for u in upd["result"]:
                    state["tg_offset"] = u["update_id"] + 1
                    handle_command(state, u, bot_username)
                save_state(state)

            # Auto signals
            if state.get("paused", False):
                time.sleep(1)
                continue

            now = time.time()
            if now - last_check < CHECK_INTERVAL_SEC:
                time.sleep(1)
                continue
            last_check = now

            for label, sym in SYMBOLS.items():
                if not market_is_open_for_symbol(label):
                    continue
                if not session_allowed_for_symbol(label):
                    continue

                plan = build_plan(label, sym)
                if plan is None:
                    continue

                if not should_send(state, plan):
                    continue

                m30 = yf_download_safe(plan.symbol, LOOKBACK_M30, "30m")
                if m30 is None or m30.empty:
                    continue

                caption = format_plan(plan)
                img = render_chart(m30, plan)

                ok = tg_send_photo(CHAT_ID, caption, img)
                if not ok:
                    tg_send_message(CHAT_ID, caption)

                mark_sent(state, plan)
                register_trade_for_daily(state, plan)

                save_state(state)
                time.sleep(1)

        except Exception as e:
            logger.exception("Main loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
