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


# =========================
# LOGGING (Railway-friendly)
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s: %(message)s"
)
logger = logging.getLogger("vip_bot")


# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # @groupusername OR -100xxxxxxxxxx

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# MODE:
# vip_retest: يرسل فقط إذا السعر قريب/لمس Zone (أكثر دقة وأقل سبام)
# vip_mix: يسمح بإرسال Pending حتى لو بعيدة قليلاً (حتى MAX_PENDING_DISTANCE_ATR)
MODE = os.getenv("MODE", "vip_retest").strip().lower()

# ATR / Zone params
TOUCH_ATR_MULT = float(os.getenv("TOUCH_ATR_MULT", "0.35"))          # لمس/قرب المنطقة
RETEST_ATR = float(os.getenv("RETEST_ATR", "0.40"))                  # شرط قرب لإرسال limit في vip_retest
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.50"))
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
UPDATES_TIMEOUT = 30

# EMA (للترند + الرسم)
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))

# Data windows
LOOKBACK_D1 = os.getenv("LOOKBACK_D1", "180d")
LOOKBACK_H4 = os.getenv("LOOKBACK_H4", "120d")
LOOKBACK_M30 = os.getenv("LOOKBACK_M30", "30d")

# State persistence
# جرّب اجعلها "state.json" بدل /tmp إذا لاحظت تكرار بسبب restart
STATE_PATH = os.getenv("STATE_PATH", "state.json")

# Symbols (Yahoo Finance)
SYMBOLS: Dict[str, str] = {
    "XAU": os.getenv("XAU_SYMBOL", "GC=F"),       # بديل محتمل: XAUUSD=X
    "BTC": os.getenv("BTC_SYMBOL", "BTC-USD"),
    "US100": os.getenv("US100_SYMBOL", "NQ=F"),
    "US30": os.getenv("US30_SYMBOL", "^DJI"),
    "OIL": os.getenv("OIL_SYMBOL", "CL=F"),
}

TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


# =========================
# STATE
# =========================
def load_state() -> dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load state: %s", e)

    return {
        "last_sent": {},      # symbol -> {ts, hash}
        "last_global": {},    # hash -> ts
        "tg_offset": 0,
        "paused": False,
    }

def save_state(state: dict) -> None:
    try:
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
    """مهم جداً: إذا كان Webhook مفعّل فلن يعمل getUpdates وبالتالي البوت لن يرد على الأوامر."""
    try:
        r = requests.get(tg_url("deleteWebhook"), params={"drop_pending_updates": True}, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            logger.warning("deleteWebhook failed: %s | %s", r.status_code, r.text[:200])
        else:
            logger.info("deleteWebhook OK")
    except Exception as e:
        logger.warning("deleteWebhook error: %s", e)

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
        caption = (caption or "")[:900]  # safe under caption limit
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
        params = {"timeout": UPDATES_TIMEOUT, "offset": offset, "allowed_updates": ["message", "channel_post"]}
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

        # flatten if needed
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
    # extra: pending suggestions (بعيدة/معلقة)
    pending_entries: Optional[List[float]] = None

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

    # Decide direction
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
    pending_entries: List[float] = []

    if zone:
        zone_low, zone_high = float(min(zone[0], zone[1])), float(max(zone[0], zone[1]))
        mid = (zone_low + zone_high) / 2.0
        dist_atr = abs(px - mid) / (a + 1e-9)

        if MODE == "vip_retest":
            # send only if price is close enough to zone (retest)
            if dist_atr <= RETEST_ATR:
                entry_type = "LIMIT"
                entry = mid
            else:
                return None

        else:
            # vip_mix:
            # 1) إذا قريب: LIMIT على منتصف المنطقة
            # 2) إذا بعيد لكن داخل MAX_PENDING_DISTANCE_ATR: نرسل "أوامر معلقة بعيدة" (2 مستويات)
            if dist_atr <= RETEST_ATR:
                entry_type = "LIMIT"
                entry = mid
            elif dist_atr <= MAX_PENDING_DISTANCE_ATR:
                entry_type = "MARKET"  # نرسل كإشارة عامة + نضيف pending مقترحة
                # pending 2 levels: mid + edge
                pending_entries = [mid]
                if side == "BUY":
                    pending_entries.append(zone_low)
                else:
                    pending_entries.append(zone_high)
            else:
                # بعيد جداً
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

    conf = 5
    conf += 2 if abs(overall) == 2 else 1
    conf += 2 if (zone_name in ("OrderBlock", "FVG")) else 0
    conf += 1 if entry_type == "LIMIT" else 0
    conf -= 1 if (a / (abs(px) + 1e-9)) > (0.8 * MAX_ATR_PCT) else 0
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
        pending_entries=[round(x, 2) for x in pending_entries] if pending_entries else None
    )


# =========================
# CHART IMAGE (Candles + EMAs)
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
        rect = plt.Rectangle((x[i] - 0.35, y0), 0.7, max(y1 - y0, 1e-9), fill=False, linewidth=1)
        ax.add_patch(rect)

    # EMAs
    ax.plot(x, d["EMA_FAST"].values, linewidth=1.2, label=f"EMA{EMA_FAST}")
    ax.plot(x, d["EMA_SLOW"].values, linewidth=1.2, label=f"EMA{EMA_SLOW}")

    # zone shading
    if plan.zone_low is not None and plan.zone_high is not None:
        ax.axhspan(plan.zone_low, plan.zone_high, alpha=0.18)

    # levels
    ax.axhline(plan.entry, linewidth=1.3)
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

    pending_txt = ""
    if plan.pending_entries:
        p_lines = []
        if plan.side == "BUY":
            for i, p in enumerate(plan.pending_entries, start=1):
                p_lines.append(f"• Buy Limit #{i}: {p:.2f}")
        else:
            for i, p in enumerate(plan.pending_entries, start=1):
                p_lines.append(f"• Sell Limit #{i}: {p:.2f}")
        pending_txt = "\n\n📌 Pending (بعيدة):\n" + "\n".join(p_lines)

    return (
        f"🔥 VIP M30\n"
        f"📌 {plan.label} ({plan.symbol})\n"
        f"Trend D1/H4: {plan.trend_d1:+d} / {plan.trend_h4:+d} => Overall: {plan.overall:+d}\n\n"
        f"{side_emoji} {order}: {plan.entry:.2f}\n"
        f"SL: {plan.sl:.2f}\n"
        f"TP1: {plan.tp1:.2f}\n"
        f"TP2: {plan.tp2:.2f}\n"
        f"TP3: {plan.tp3:.2f}\n\n"
        f"Zone: {zone_txt}\n"
        f"Risk: {RISK_PCT:.1f}% (~${plan.risk_usd:.2f})\n"
        f"Confidence: {plan.confidence}/10\n"
        f"Mode: {MODE}\n"
        f"{pending_txt}"
    )

def signal_hash(plan: Plan) -> str:
    key = (
        f"{plan.symbol}|{plan.side}|{plan.entry_type}|{plan.entry:.2f}|"
        f"{plan.sl:.2f}|{plan.tp1:.2f}|{plan.tp2:.2f}|{plan.tp3:.2f}|"
        f"{plan.zone_name}|{plan.zone_low}|{plan.zone_high}|{plan.pending_entries}"
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def should_send(state: dict, plan: Plan) -> bool:
    now = time.time()
    cooldown = COOLDOWN_MINUTES * 60

    h = signal_hash(plan)

    # 1) Global anti-duplicate (حتى لو restart)
    last_global = state.get("last_global", {})
    ts_g = float(last_global.get(h, 0))
    if (now - ts_g) < cooldown:
        return False

    # 2) Per-symbol cooldown
    rec = state.get("last_sent", {}).get(plan.symbol, {})
    last_ts = float(rec.get("ts", 0))
    if (now - last_ts) < cooldown:
        return False

    return True

def mark_sent(state: dict, plan: Plan) -> None:
    now = time.time()
    h = signal_hash(plan)
    state.setdefault("last_sent", {})[plan.symbol] = {"ts": now, "hash": h}
    state.setdefault("last_global", {})[h] = now

    # تنظيف hashes القديمة حتى لا يكبر الملف
    ttl = max(3600, COOLDOWN_MINUTES * 60 * 4)
    lg = state.get("last_global", {})
    for k in list(lg.keys()):
        if (now - float(lg.get(k, 0))) > ttl:
            lg.pop(k, None)


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "✅ أوامر VIP:\n"
    "/help\n"
    "/status\n"
    "/symbols\n"
    "/mode vip_retest  أو  /mode vip_mix\n"
    "/analyze XAU  (أو BTC / US100 / US30 / OIL)\n"
    "/scan  (أفضل إشارة)\n"
    "/pause  |  /resume\n\n"
    "ملاحظة: داخل المجموعات أحياناً تحتاج:\n"
    "/help@اسم_البوت\n"
)

def _normalize_symbol_text(txt: str) -> Optional[str]:
    t = (txt or "").strip().upper()
    if not t:
        return None
    # aliases
    aliases = {
        "GOLD": "XAU",
        "XAUUSD": "XAU",
        "XAU/USD": "XAU",
        "NAS100": "US100",
        "NQ": "US100",
        "US-100": "US100",
        "DOW": "US30",
        "DJI": "US30",
        "US-30": "US30",
        "WTI": "OIL",
        "OIL": "OIL",
    }
    return aliases.get(t, t)

def _strip_botname(cmd: str) -> str:
    # /help@mybot -> /help
    if "@" in cmd:
        return cmd.split("@", 1)[0]
    return cmd

def handle_command(state: dict, update: dict) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    text = (msg.get("text") or "").strip()
    if not text:
        return

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", "")) if chat.get("id") is not None else ""
    if not chat_id:
        return

    # إذا تريد تقييد الأوامر فقط على نفس CHAT_ID:
    # اجعل ALLOW_ANY_CHAT=0
    allow_any = os.getenv("ALLOW_ANY_CHAT", "1").strip() == "1"
    if (not allow_any) and CHAT_ID and (chat_id != CHAT_ID) and (str(chat.get("username", "")) != CHAT_ID.lstrip("@")):
        return

    # أوامر تبدأ بـ /
    if not text.startswith("/"):
        return

    parts = text.split()
    cmd_raw = parts[0].lower()
    cmd = _strip_botname(cmd_raw)

    global MODE

    if cmd in ("/help", "/halp"):  # دعم خطأ كتابي شائع
        tg_send_message(chat_id, HELP_TEXT)
        return

    if cmd == "/status":
        tg_send_message(
            chat_id,
            f"MODE={MODE}\nCHECK_INTERVAL_SEC={CHECK_INTERVAL_SEC}\nCOOLDOWN_MINUTES={COOLDOWN_MINUTES}\n"
            f"RETEST_ATR={RETEST_ATR}\nMAX_PENDING_DISTANCE_ATR={MAX_PENDING_DISTANCE_ATR}\n"
            f"SL_BUFFER_ATR={SL_BUFFER_ATR}\nMAX_ATR_PCT={MAX_ATR_PCT}\nRISK_PCT={RISK_PCT}\nPAUSED={state.get('paused', False)}"
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

    # analyze + aliases shortcuts: /xau /btc /us100 ...
    if cmd in ("/analyze", "/xau", "/btc", "/us100", "/us30", "/oil", "/xauusd"):
        if cmd != "/analyze":
            sym_key = _normalize_symbol_text(cmd.replace("/", ""))
        else:
            if len(parts) < 2:
                tg_send_message(chat_id, "اكتب مثال: /analyze XAU")
                return
            sym_key = _normalize_symbol_text(parts[1])

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
        return


# =========================
# MAIN LOOP
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Missing BOT_TOKEN or CHAT_ID in Railway Variables.")
        return

    # مهم للأوامر:
    tg_delete_webhook()

    state = load_state()
    logger.info("VIP bot started. MODE=%s | CHAT_ID=%s", MODE, CHAT_ID)

    # Startup message
    tg_send_message(CHAT_ID, "✅ VIP Bot Online (M30 + Chart + Auto + Commands). اكتب /help")

    last_check = 0.0

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

            # 2) Auto signals
            if state.get("paused", False):
                time.sleep(1)
                continue

            now = time.time()
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
                save_state(state)
                time.sleep(1)  # avoid Telegram burst

        except Exception as e:
            logger.exception("Main loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
