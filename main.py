# -*- coding: utf-8 -*-
import os
import io
import time
import json
import math
import hashlib
import logging
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple, List

import requests
import pandas as pd
import yfinance as yf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # @groupusername OR -100xxxxxxxxxx

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))  # auto scan
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# MODE:
# vip_retest: يرسل فقط إذا السعر قريب/لمس Zone (أقل إشارات وأكثر دقة)
# vip_mix: يسمح بإرسال Pending إذا كانت ضمن MAX_PENDING_DISTANCE_ATR (إشارات أكثر)
MODE = os.getenv("MODE", "vip_retest").strip().lower()

# ATR / Zone params
RETEST_ATR = float(os.getenv("RETEST_ATR", "0.45"))                  # قرب المنطقة لإرسال limit في vip_retest
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.80"))
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.008"))               # فلتر تذبذب قوي

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
UPDATES_TIMEOUT = 30

# EMA (للترند فقط)
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))

# Liquidity + BOS filters
USE_LIQ_BOS = os.getenv("USE_LIQ_BOS", "1").strip() == "1"
SWING_LEN = int(os.getenv("SWING_LEN", "3"))        # swings pivots
LOOKBACK_SWEEP = int(os.getenv("LOOKBACK_SWEEP", "60"))
LOOKBACK_BOS = int(os.getenv("LOOKBACK_BOS", "60"))

# Data windows
LOOKBACK_D1 = os.getenv("LOOKBACK_D1", "200d")
LOOKBACK_H4 = os.getenv("LOOKBACK_H4", "140d")
LOOKBACK_M30 = os.getenv("LOOKBACK_M30", "45d")

# performance tracking
TRACK_PERFORMANCE = os.getenv("TRACK_PERFORMANCE", "1").strip() == "1"
PERF_CHECK_EVERY_SEC = int(os.getenv("PERF_CHECK_EVERY_SEC", "180"))  # reuse check interval
PERF_SUMMARY_EVERY_HOURS = int(os.getenv("PERF_SUMMARY_EVERY_HOURS", "24"))  # auto summary each 24h

# State persistence
STATE_PATH = os.getenv("STATE_PATH", "/tmp/state.json")

# =========================
# SYMBOLS (more assets)
# =========================
# ملاحظة: بعض رموز Yahoo قد تتوقف أحياناً. يمكنك تعديلها من متغيرات Railway أيضاً.
SYMBOLS: Dict[str, str] = {
    # Metals / Commodities
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),
    "XAG": os.getenv("XAG_SYMBOL", "SI=F"),
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),
    "NG":  os.getenv("NG_SYMBOL",  "NG=F"),

    # Crypto
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "ETH": os.getenv("ETH_SYMBOL", "ETH-USD"),
    "SOL": os.getenv("SOL_SYMBOL", "SOL-USD"),

    # Indices
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30":  os.getenv("US30_SYMBOL", "^DJI"),
    "SPX":   os.getenv("SPX_SYMBOL", "^GSPC"),

    # Forex majors
    "EURUSD": os.getenv("EURUSD_SYMBOL", "EURUSD=X"),
    "GBPUSD": os.getenv("GBPUSD_SYMBOL", "GBPUSD=X"),
    "USDJPY": os.getenv("USDJPY_SYMBOL", "JPY=X"),
    "AUDUSD": os.getenv("AUDUSD_SYMBOL", "AUDUSD=X"),
}

TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# =========================
# STATE
# =========================
def load_state() -> dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "last_sent": {},     # symbol -> {ts, hash}
        "tg_offset": 0,
        "paused": False,
        "trades": [],        # active trade tracking
        "perf_last_summary_ts": 0,
    }

def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("Failed to save state: %s", e)


# =========================
# TELEGRAM
# =========================
def tg_url(method: str) -> str:
    return TELEGRAM_BASE.format(token=BOT_TOKEN, method=method)

def tg_delete_webhook() -> None:
    # مهم لحل 409 conflict إذا كان webhook مفعل سابقاً
    try:
        r = requests.get(tg_url("deleteWebhook"), params={"drop_pending_updates": False}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            logging.info("deleteWebhook OK")
        else:
            logging.warning("deleteWebhook failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("deleteWebhook error: %s", e)

def tg_send_message(chat_id: str, text: str) -> bool:
    try:
        payload = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
        r = requests.post(tg_url("sendMessage"), json=payload, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            logging.error("sendMessage failed: %s | %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        logging.error("sendMessage error: %s", e)
        return False

def tg_send_photo(chat_id: str, caption: str, image_bytes: bytes) -> bool:
    try:
        caption = (caption or "")[:1000]
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption}
        r = requests.post(tg_url("sendPhoto"), data=data, files=files, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            logging.error("sendPhoto failed: %s | %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        logging.error("sendPhoto error: %s", e)
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
        logging.error("getUpdates error: %s", e)
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
        logging.error("yfinance failed %s %s %s: %s", symbol, period, interval, e)
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
# LIQUIDITY + BOS (simple but effective)
# =========================
def pivots(series: pd.Series, left: int, right: int) -> Tuple[List[int], List[int]]:
    # returns pivot_high_idx, pivot_low_idx
    highs, lows = [], []
    vals = series.values
    for i in range(left, len(vals) - right):
        window = vals[i-left:i+right+1]
        if vals[i] == max(window):
            highs.append(i)
        if vals[i] == min(window):
            lows.append(i)
    return highs, lows

def detect_liquidity_sweep(df: pd.DataFrame, side: str) -> bool:
    """
    Sweep (أخذ سيولة) بشكل بسيط:
    - BUY: شمعة عملت low أقل من آخر pivot low ثم أغلقت فوقه
    - SELL: شمعة عملت high أعلى من آخر pivot high ثم أغلقت تحته
    """
    d = df.tail(max(LOOKBACK_SWEEP, 100)).copy().reset_index(drop=True)
    if len(d) < 30:
        return False

    pivot_highs, pivot_lows = pivots(d["Close"], SWING_LEN, SWING_LEN)
    if not pivot_highs and not pivot_lows:
        return False

    last = d.iloc[-1]
    prev = d.iloc[-2]

    if side == "BUY":
        if not pivot_lows:
            return False
        last_pivot_idx = pivot_lows[-1]
        pivot_price = float(d.loc[last_pivot_idx, "Low"])
        # sweep: wick below pivot then close above
        return (float(last["Low"]) < pivot_price) and (float(last["Close"]) > pivot_price)

    else:
        if not pivot_highs:
            return False
        last_pivot_idx = pivot_highs[-1]
        pivot_price = float(d.loc[last_pivot_idx, "High"])
        return (float(last["High"]) > pivot_price) and (float(last["Close"]) < pivot_price)

def detect_bos(df: pd.DataFrame, side: str) -> bool:
    """
    BOS بسيط:
    - BUY: إغلاق فوق آخر pivot high
    - SELL: إغلاق تحت آخر pivot low
    """
    d = df.tail(max(LOOKBACK_BOS, 100)).copy().reset_index(drop=True)
    if len(d) < 30:
        return False

    pivot_highs, pivot_lows = pivots(d["Close"], SWING_LEN, SWING_LEN)
    close_now = float(d["Close"].iloc[-1])

    if side == "BUY":
        if not pivot_highs:
            return False
        last_pivot_idx = pivot_highs[-1]
        level = float(d.loc[last_pivot_idx, "High"])
        return close_now > level

    else:
        if not pivot_lows:
            return False
        last_pivot_idx = pivot_lows[-1]
        level = float(d.loc[last_pivot_idx, "Low"])
        return close_now < level


# =========================
# ZONES (OrderBlock + FVG simple)
# =========================
def find_orderblock(df: pd.DataFrame, direction: str) -> Optional[Tuple[float, float]]:
    if len(df) < 60:
        return None
    d = df.tail(90).copy()
    a = atr(d, 14).iloc[-1]
    if a is None or pd.isna(a) or a <= 0:
        return None

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
# PLAN / SIGNAL
# =========================
@dataclass
class Plan:
    label: str
    symbol: str
    timeframe: str
    trend_d1: int
    trend_h4: int
    overall: int
    side: str          # BUY/SELL
    entry_type: str    # MARKET/LIMIT
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
    r = max(r, 1e-6)
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

    # Volatility guard
    if (a / (abs(px) + 1e-9)) > MAX_ATR_PCT:
        return None

    # zones priority
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

    entry_type = "MARKET"
    entry = px
    zone_low = None
    zone_high = None

    if zone:
        zone_low, zone_high = float(min(zone[0], zone[1])), float(max(zone[0], zone[1]))
        mid = (zone_low + zone_high) / 2.0
        dist_atr = abs(px - mid) / (a + 1e-9)

        if MODE == "vip_retest":
            if dist_atr <= RETEST_ATR:
                entry_type = "LIMIT"
                entry = mid
            else:
                return None
        else:
            if dist_atr <= MAX_PENDING_DISTANCE_ATR:
                entry_type = "LIMIT"
                entry = mid

    # Liquidity + BOS filters (رفع الجودة)
    liq_ok = True
    bos_ok = True
    if USE_LIQ_BOS:
        # نطلب أحدهما على الأقل (liq أو bos) + الأفضل الاثنين
        liq_ok = detect_liquidity_sweep(m30, side)
        bos_ok = detect_bos(m30, side)
        if not (liq_ok or bos_ok):
            return None

    # SL beyond zone with buffer, else ATR based
    if side == "BUY":
        if zone_low is not None:
            sl = zone_low - (SL_BUFFER_ATR * a)
        else:
            sl = entry - (1.5 * a)
    else:
        if zone_high is not None:
            sl = zone_high + (SL_BUFFER_ATR * a)
        else:
            sl = entry + (1.5 * a)

    tp1, tp2, tp3 = calc_rr_targets(entry, sl, side)

    # confidence (simple)
    conf = 5
    conf += 2 if abs(overall) == 2 else 1
    conf += 2 if entry_type == "LIMIT" and zone_name in ("OrderBlock", "FVG") else 0
    conf += 2 if USE_LIQ_BOS and liq_ok else 0
    conf += 2 if USE_LIQ_BOS and bos_ok else 0
    conf -= 1 if (a / (abs(px) + 1e-9)) > (0.85 * MAX_ATR_PCT) else 0
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
        entry=round(float(entry), 2),
        sl=round(float(sl), 2),
        tp1=round(float(tp1), 2),
        tp2=round(float(tp2), 2),
        tp3=round(float(tp3), 2),
        zone_name=zone_name,
        zone_low=round(zone_low, 2) if zone_low is not None else None,
        zone_high=round(zone_high, 2) if zone_high is not None else None,
        atr_value=float(a),
        confidence=conf,
        risk_usd=round(float(risk_usd), 2),
        liq=bool(liq_ok),
        bos=bool(bos_ok),
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

    # candles
    for i in range(len(d)):
        ax.plot([x[i], x[i]], [l[i], h[i]], linewidth=1)
        y0 = min(o[i], c[i])
        y1 = max(o[i], c[i])
        rect = plt.Rectangle((x[i]-0.35, y0), 0.7, max(y1 - y0, 1e-9), fill=False, linewidth=1)
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
    step = max(1, len(d)//6)
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
# FORMAT + DEDUP
# =========================
def format_plan(plan: Plan) -> str:
    side_emoji = "🟢" if plan.side == "BUY" else "🔴"
    if plan.entry_type == "LIMIT":
        order = "Buy Limit" if plan.side == "BUY" else "Sell Limit"
    else:
        order = "BUY Market" if plan.side == "BUY" else "SELL Market"

    zone_txt = "—"
    if plan.zone_low is not None and plan.zone_high is not None:
        zone_txt = f"{plan.zone_name} [{plan.zone_low:.2f} - {plan.zone_high:.2f}]"

    liq_txt = "✅" if plan.liq else "❌"
    bos_txt = "✅" if plan.bos else "❌"

    return (
        f"🔥 VIP M30\n"
        f"📌 {plan.label} ({plan.symbol})\n"
        f"Trend D1/H4: {plan.trend_d1:+d} / {plan.trend_h4:+d} => Overall: {plan.overall:+d}\n"
        f"Liq Sweep: {liq_txt} | BOS: {bos_txt}\n\n"
        f"{side_emoji} {order}: {plan.entry:.2f}\n"
        f"SL: {plan.sl:.2f}\n"
        f"TP1: {plan.tp1:.2f}\n"
        f"TP2: {plan.tp2:.2f}\n"
        f"TP3: {plan.tp3:.2f}\n\n"
        f"Zone: {zone_txt}\n"
        f"Risk: {RISK_PCT:.1f}% (~${plan.risk_usd:.2f})\n"
        f"Confidence: {plan.confidence}/10\n"
        f"Mode: {MODE}\n"
    )

def signal_hash(plan: Plan) -> str:
    key = f"{plan.symbol}|{plan.side}|{plan.entry_type}|{plan.entry:.2f}|{plan.sl:.2f}|{plan.tp1:.2f}|{plan.tp2:.2f}|{plan.tp3:.2f}|{plan.zone_name}|{plan.zone_low}|{plan.zone_high}|liq={int(plan.liq)}|bos={int(plan.bos)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def should_send(state: dict, plan: Plan) -> bool:
    now = time.time()
    rec = state.get("last_sent", {}).get(plan.symbol, {})
    last_ts = float(rec.get("ts", 0))
    last_hash = rec.get("hash", "")

    h = signal_hash(plan)
    cooldown = COOLDOWN_MINUTES * 60

    # 1) block during cooldown for this symbol
    if (now - last_ts) < cooldown:
        return False

    # 2) block same hash even بعد cooldown قصير (احتياط)
    if h == last_hash and (now - last_ts) < (2 * cooldown):
        return False

    return True

def mark_sent(state: dict, plan: Plan) -> None:
    state.setdefault("last_sent", {})[plan.symbol] = {"ts": time.time(), "hash": signal_hash(plan)}


# =========================
# PERFORMANCE TRACKING
# =========================
def trade_id(plan: Plan) -> str:
    base = f"{plan.symbol}|{plan.side}|{plan.entry_type}|{plan.entry:.2f}|{plan.sl:.2f}|{plan.tp1:.2f}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:10]

def add_trade(state: dict, plan: Plan, source: str) -> None:
    if not TRACK_PERFORMANCE:
        return

    tid = trade_id(plan)
    # avoid duplicates
    for t in state.get("trades", []):
        if t.get("id") == tid and t.get("status") == "OPEN":
            return

    state.setdefault("trades", []).append({
        "id": tid,
        "label": plan.label,
        "symbol": plan.symbol,
        "side": plan.side,
        "entry": plan.entry,
        "sl": plan.sl,
        "tp1": plan.tp1,
        "tp2": plan.tp2,
        "tp3": plan.tp3,
        "opened_ts": time.time(),
        "status": "OPEN",   # OPEN / TP1 / SL / TP2 / TP3
        "source": source,   # AUTO / CMD
    })

def check_trades(state: dict) -> int:
    """
    تحديث صفقات OPEN:
    نعتبر التنفيذ حصل عند إرسال الإشارة (تقريب).
    ثم نراقب: إذا High/Low لمس TP1 أو SL أولاً.
    """
    if not TRACK_PERFORMANCE:
        return 0

    changed = 0
    trades = state.get("trades", [])
    if not trades:
        return 0

    for t in trades:
        if t.get("status") != "OPEN":
            continue

        sym = t["symbol"]
        df = yf_download_safe(sym, "7d", "30m")
        if df is None or df.empty:
            continue

        high = float(df["High"].iloc[-1])
        low = float(df["Low"].iloc[-1])

        side = t["side"]
        sl = float(t["sl"])
        tp1 = float(t["tp1"])
        tp2 = float(t["tp2"])
        tp3 = float(t["tp3"])

        # check touch
        if side == "BUY":
            sl_hit = low <= sl
            tp1_hit = high >= tp1
            tp2_hit = high >= tp2
            tp3_hit = high >= tp3
        else:
            sl_hit = high >= sl
            tp1_hit = low <= tp1
            tp2_hit = low <= tp2
            tp3_hit = low <= tp3

        # priority: SL vs TP1 first cannot be perfect without intrabar order
        # approximation: if both hit same bar => نعتبر SL أولاً للحذر
        if sl_hit and tp1_hit:
            t["status"] = "SL"
            changed += 1
        elif sl_hit:
            t["status"] = "SL"
            changed += 1
        elif tp3_hit:
            t["status"] = "TP3"
            changed += 1
        elif tp2_hit:
            t["status"] = "TP2"
            changed += 1
        elif tp1_hit:
            t["status"] = "TP1"
            changed += 1

    if changed:
        save_state(state)
    return changed

def performance_summary(state: dict) -> str:
    trades = state.get("trades", [])
    if not trades:
        return "📊 الأداء: لا توجد صفقات مسجلة بعد."

    total = len(trades)
    open_n = sum(1 for t in trades if t.get("status") == "OPEN")
    tp1 = sum(1 for t in trades if t.get("status") == "TP1")
    tp2 = sum(1 for t in trades if t.get("status") == "TP2")
    tp3 = sum(1 for t in trades if t.get("status") == "TP3")
    sl = sum(1 for t in trades if t.get("status") == "SL")

    closed = total - open_n
    win = tp1 + tp2 + tp3
    winrate = (100.0 * win / closed) if closed > 0 else 0.0

    return (
        "📊 VIP PERFORMANCE\n"
        f"Total signals tracked: {total}\n"
        f"OPEN: {open_n}\n"
        f"TP1: {tp1} | TP2: {tp2} | TP3: {tp3}\n"
        f"SL: {sl}\n"
        f"Winrate (closed): {winrate:.1f}%\n"
    )


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "✅ أوامر VIP:\n"
    "/help\n"
    "/status\n"
    "/symbols\n"
    "/mode vip_retest  أو  /mode vip_mix\n"
    "/analyze XAU  (أو BTC / US100 / US30 / OIL ...)\n"
    "/scan  (أفضل إشارة)\n"
    "/performance\n"
    "/pause  |  /resume\n"
)

def handle_command(state: dict, update: dict) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", "")) or CHAT_ID  # يرد في نفس الكروب

    parts = text.split()
    cmd = parts[0].lower().split("@")[0]  # يسمح /help أو /help@bot

    global MODE

    if cmd in ("/halp", "/halp@"):  # لو كتبتها غلط
        cmd = "/help"

    if cmd == "/help":
        tg_send_message(chat_id, HELP_TEXT)
        return

    if cmd == "/status":
        tg_send_message(
            chat_id,
            f"MODE={MODE}\nCHECK_INTERVAL_SEC={CHECK_INTERVAL_SEC}\nCOOLDOWN_MINUTES={COOLDOWN_MINUTES}\n"
            f"RETEST_ATR={RETEST_ATR}\nMAX_PENDING_DISTANCE_ATR={MAX_PENDING_DISTANCE_ATR}\n"
            f"SL_BUFFER_ATR={SL_BUFFER_ATR}\nMAX_ATR_PCT={MAX_ATR_PCT}\n"
            f"USE_LIQ_BOS={int(USE_LIQ_BOS)}\nSWING_LEN={SWING_LEN}\n"
            f"RISK_PCT={RISK_PCT}\nPAUSED={state.get('paused', False)}"
        )
        return

    if cmd == "/symbols":
        tg_send_message(chat_id, "Symbols: " + ", ".join([f"{k}={v}" for k, v in SYMBOLS.items()]))
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

    if cmd == "/performance":
        # تحديث سريع قبل عرض الملخص
        try:
            check_trades(state)
        except Exception:
            pass
        tg_send_message(chat_id, performance_summary(state))
        return

    if cmd == "/analyze" and len(parts) >= 2:
        sym_key = parts[1].upper().strip()
        if sym_key not in SYMBOLS:
            tg_send_message(chat_id, "❌ الرمز غير معروف. جرّب /symbols")
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

        add_trade(state, plan, source="CMD")
        save_state(state)
        return

    if cmd == "/scan":
        best = None
        for k, sym in SYMBOLS.items():
            p = build_plan(k, sym)
            if p is None:
                continue
            if best is None or p.confidence > best.confidence:
                best = p

        if best is None:
            tg_send_message(chat_id, "⚠️ لا توجد إشارات حالياً.")
            return

        m30 = yf_download_safe(best.symbol, LOOKBACK_M30, "30m")
        if m30 is None or m30.empty:
            tg_send_message(chat_id, "⚠️ تعذر جلب بيانات الشارت.")
            return

        caption = "🔥 BEST VIP SIGNAL\n\n" + format_plan(best)
        img = render_chart(m30, best)

        if not tg_send_photo(chat_id, caption, img):
            tg_send_message(chat_id, caption)

        add_trade(state, best, source="CMD")
        save_state(state)
        return


# =========================
# MAIN LOOP
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Missing BOT_TOKEN or CHAT_ID in Railway Variables.")
        return

    # مهم جداً لتجنب 409 conflict
    tg_delete_webhook()

    state = load_state()
    logging.info("VIP bot started. MODE=%s | CHAT_ID=%s", MODE, CHAT_ID)

    tg_send_message(CHAT_ID, "✅ VIP Bot Online (More Assets + Liquidity/BOS + Performance). اكتب /help")

    last_check = 0.0
    last_perf_check = 0.0

    while True:
        try:
            # 1) Read Telegram commands
            offset = int(state.get("tg_offset", 0))
            upd = tg_get_updates(offset)
            if upd.get("ok") and upd.get("result"):
                for u in upd["result"]:
                    state["tg_offset"] = u["update_id"] + 1
                    handle_command(state, u)
                save_state(state)

            # 2) Performance checks
            now = time.time()
            if TRACK_PERFORMANCE and (now - last_perf_check) >= PERF_CHECK_EVERY_SEC:
                last_perf_check = now
                changed = check_trades(state)
                if changed:
                    logging.info("Performance updated: %s trades changed", changed)

                # auto summary each N hours
                last_sum = float(state.get("perf_last_summary_ts", 0))
                if (now - last_sum) >= (PERF_SUMMARY_EVERY_HOURS * 3600):
                    state["perf_last_summary_ts"] = now
                    save_state(state)
                    tg_send_message(CHAT_ID, performance_summary(state))

            # 3) Auto signals
            if state.get("paused", False):
                time.sleep(1)
                continue

            if now - last_check < CHECK_INTERVAL_SEC:
                time.sleep(1)
                continue
            last_check = now

            for label, sym in SYMBOLS.items():
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
                add_trade(state, plan, source="AUTO")
                save_state(state)
                time.sleep(1)  # avoid Telegram burst

        except Exception as e:
            logging.exception("Main loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
