import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import requests
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =========================
# Environment / Config
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # مثال صحيح: @abdel_tra  أو -1001234567890

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "3"))

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))

# ATR / Zones behavior
TOUCH_ATR_MULT = float(os.getenv("TOUCH_ATR_MULT", "0.35"))           # لمس المنطقة = دخول Market
RETEST_ATR = float(os.getenv("RETEST_ATR", "0.35"))                   # نفس الفكرة (اختياري)
MAX_PENDING_DISTANCE_ATR = float(os.getenv("MAX_PENDING_DISTANCE_ATR", "1.5"))  # أقصى بعد أمر معلق
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.20"))             # هامش SL خارج المنطقة
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.006"))                # فلتر تذبذب مبالغ فيه
INVALID_ATR = float(os.getenv("INVALID_ATR", "0.0"))                  # لو ATR أقل من كذا اعتبره غير صالح

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
YF_RETRIES = int(os.getenv("YF_RETRIES", "3"))

# EMAs
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_MID = int(os.getenv("EMA_MID", "50"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "200"))

# Symbols (Yahoo)
SYMBOLS: Dict[str, List[str]] = {
    # Gold: أحيانًا GC=F يفشل على Railway، لذلك نضع بدائل
    "XAU": ["XAUUSD=X", "GC=F"],
    "BTC": ["BTC-USD"],
    "US100": ["NQ=F"],
    "US30": ["YM=F", "^DJI"],     # YM=F أفضل عادةً، ^DJI بديل
    "OIL": ["CL=F"],
}

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)


# =========================
# Telegram Helpers
# =========================
def _telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def send_telegram_photo_with_caption(photo_bytes: bytes, caption: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("BOT_TOKEN or CHAT_ID missing.")
        return False

    try:
        files = {"photo": ("chart.png", photo_bytes, "image/png")}
        data = {
            "chat_id": CHAT_ID,
            "caption": caption[:1024],  # Telegram caption limit
            "parse_mode": "HTML"
        }
        r = requests.post(_telegram_url("sendPhoto"), data=data, files=files, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            logging.error("Telegram sendPhoto failed: %s | %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        logging.error("Telegram sendPhoto error: %s", e)
        return False


# =========================
# Market Data (yfinance)
# =========================
def yf_download(symbol: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    import yfinance as yf
    last_err = None

    for _ in range(max(1, YF_RETRIES)):
        try:
            df = yf.download(
                symbol,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
                threads=False
            )
            if df is None or df.empty:
                last_err = f"empty df for {symbol} {period} {interval}"
                time.sleep(1.2)
                continue

            # Normalize columns
            if isinstance(df.columns, pd.MultiIndex):
                # Take first level if needed (sometimes yfinance returns ('Close', 'BTC-USD') style)
                df.columns = [c[0] for c in df.columns]

            needed = {"Open", "High", "Low", "Close"}
            if not needed.issubset(set(df.columns)):
                last_err = f"missing columns {needed - set(df.columns)} for {symbol}"
                time.sleep(1.2)
                continue

            df = df.dropna()
            if df.empty:
                last_err = f"all NaN after dropna for {symbol}"
                time.sleep(1.2)
                continue

            return df

        except Exception as e:
            last_err = str(e)
            time.sleep(1.2)

    logging.error("yfinance download failed: %s | %s", symbol, last_err)
    return None

def get_best_df(symbol_candidates: List[str], period: str, interval: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    for sym in symbol_candidates:
        df = yf_download(sym, period=period, interval=interval)
        if df is not None and not df.empty:
            return df, sym
    return None, None


# =========================
# Indicators
# =========================
def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(n).mean()

def trend_score(df: pd.DataFrame) -> int:
    c = df["Close"]
    e50 = ema(c, EMA_MID)
    e200 = ema(c, EMA_SLOW)

    if len(c) < EMA_SLOW + 5:
        return 0

    # بسيط وعملي
    if e50.iloc[-1] > e200.iloc[-1]:
        return +1
    elif e50.iloc[-1] < e200.iloc[-1]:
        return -1
    return 0


# =========================
# S/R (simple swing)
# =========================
def swing_levels(df: pd.DataFrame, lookback: int = 120) -> Tuple[List[float], List[float]]:
    d = df.tail(lookback).copy()
    highs = d["High"].values
    lows = d["Low"].values

    # استخراج قمم/قيعان بسيطة
    resistances = []
    supports = []

    for i in range(2, len(d) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistances.append(float(highs[i]))
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            supports.append(float(lows[i]))

    # خذ أقرب 3 مستويات
    price = float(d["Close"].iloc[-1])
    supports = sorted(set(supports), key=lambda x: abs(price - x))[:3]
    resistances = sorted(set(resistances), key=lambda x: abs(price - x))[:3]

    return supports, resistances


# =========================
# FVG (Fair Value Gap)
# =========================
def find_fvg(df: pd.DataFrame, max_items: int = 2) -> List[Tuple[str, float, float]]:
    """
    Bullish FVG: Low[i] > High[i-2]  => gap [High[i-2], Low[i]]
    Bearish FVG: High[i] < Low[i-2]  => gap [High[i], Low[i-2]]
    """
    out = []
    if len(df) < 5:
        return out

    d = df.tail(200).copy().reset_index(drop=True)
    for i in range(2, len(d)):
        h2 = float(d.loc[i-2, "High"])
        l2 = float(d.loc[i-2, "Low"])
        hi = float(d.loc[i, "High"])
        li = float(d.loc[i, "Low"])

        if li > h2:
            out.append(("BULL_FVG", h2, li))
        if hi < l2:
            out.append(("BEAR_FVG", hi, l2))

    # آخر الفجوات أهم
    return out[-max_items:]


# =========================
# Order Block (practical heuristic)
# =========================
def find_order_block(df: pd.DataFrame, direction: str) -> Optional[Tuple[float, float]]:
    """
    Heuristic:
    - BUY: آخر شمعة هابطة قبل تسارع صاعد (close أعلى من EMA20)
    - SELL: آخر شمعة صاعدة قبل تسارع هابط (close أقل من EMA20)
    """
    if len(df) < 50:
        return None

    d = df.tail(200).copy()
    c = d["Close"]
    e20 = ema(c, EMA_FAST)

    # امشي للخلف وابحث عن "آخر شمعة عكس الاتجاه" قبل بروز الاتجاه الحالي
    for i in range(len(d) - 2, 10, -1):
        o = float(d["Open"].iloc[i])
        cl = float(d["Close"].iloc[i])
        hi = float(d["High"].iloc[i])
        lo = float(d["Low"].iloc[i])

        if direction == "BUY":
            # شمعة هابطة
            if cl < o and float(c.iloc[i+1]) > float(e20.iloc[i+1]) and float(c.iloc[-1]) > float(e20.iloc[-1]):
                # zone = جسم الشمعة (أفضل من كامل الذيل)
                top = max(o, cl)
                bot = min(o, cl)
                # fallback لو جسم صغير جدًا
                if abs(top - bot) < (hi - lo) * 0.25:
                    top, bot = hi, lo
                return (bot, top)

        if direction == "SELL":
            # شمعة صاعدة
            if cl > o and float(c.iloc[i+1]) < float(e20.iloc[i+1]) and float(c.iloc[-1]) < float(e20.iloc[-1]):
                top = max(o, cl)
                bot = min(o, cl)
                if abs(top - bot) < (hi - lo) * 0.25:
                    top, bot = hi, lo
                return (bot, top)

    return None


# =========================
# Signal logic (M30 + D1/H4 bias)
# =========================
@dataclass
class Signal:
    symbol_key: str
    symbol_used: str
    price: float

    trend_d1: int
    trend_h4: int
    overall: int

    side: str  # "BUY" or "SELL" or "NONE"
    order_type: str  # "MARKET" or "LIMIT" or "NONE"

    entry: Optional[float]
    sl: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]

    zone_name: str
    zone_low: Optional[float]
    zone_high: Optional[float]

    fvg_items: List[Tuple[str, float, float]]
    supports: List[float]
    resistances: List[float]

    confidence: int
    risk_usd: float


def compute_confidence(overall: int, m30_dir: int, touched: bool, has_zone: bool) -> int:
    score = 5
    score += 2 if overall > 0 else 0
    score += 2 if overall < 0 else 0
    score += 2 if m30_dir != 0 and (overall == 0 or (overall > 0 and m30_dir > 0) or (overall < 0 and m30_dir < 0)) else -1
    score += 1 if has_zone else 0
    score += 1 if touched else 0
    return max(1, min(10, score))


def m30_direction(df30: pd.DataFrame) -> int:
    c = df30["Close"]
    e20 = ema(c, EMA_FAST)
    e50 = ema(c, EMA_MID)

    if len(c) < 60:
        return 0

    # شرط بسيط: EMA20 فوق EMA50 + آخر إغلاق فوق EMA20 = صعود
    if e20.iloc[-1] > e50.iloc[-1] and c.iloc[-1] > e20.iloc[-1]:
        return +1
    if e20.iloc[-1] < e50.iloc[-1] and c.iloc[-1] < e20.iloc[-1]:
        return -1
    return 0


def build_signal(symbol_key: str) -> Optional[Signal]:
    # D1/H4 bias
    d1, used1 = get_best_df(SYMBOLS[symbol_key], period="180d", interval="1d")
    h4, used4 = get_best_df(SYMBOLS[symbol_key], period="120d", interval="4h")
    m30, used30 = get_best_df(SYMBOLS[symbol_key], period="60d", interval="30m")

    symbol_used = used30 or used4 or used1
    if d1 is None or h4 is None or m30 is None or symbol_used is None:
        logging.error("[%s] No data (D1/H4/M30) - skip", symbol_key)
        return None

    price = float(m30["Close"].iloc[-1])

    t_d1 = trend_score(d1)
    t_h4 = trend_score(h4)
    overall = t_d1 + t_h4

    # ATR on M30
    a = atr(m30, 14)
    atr_now = float(a.iloc[-1]) if not math.isnan(a.iloc[-1]) else 0.0
    if atr_now <= INVALID_ATR:
        logging.error("[%s] Invalid ATR (%.6f).", symbol_key, atr_now)
        return None

    # فلتر تذبذب مبالغ فيه
    if price > 0 and (atr_now / price) > MAX_ATR_PCT:
        logging.warning("[%s] ATR pct too high (%.4f) => skip", symbol_key, atr_now / price)
        return None

    # M30 direction
    m30_dir = m30_direction(m30)

    # تحديد اتجاه نهائي:
    # - إذا overall قوي (+2/-2) نعتمده
    # - إذا overall ضعيف نأخذ M30
    side = "NONE"
    if overall >= 1 and m30_dir >= 0:
        side = "BUY"
    elif overall <= -1 and m30_dir <= 0:
        side = "SELL"
    else:
        # fallback
        if m30_dir > 0:
            side = "BUY"
        elif m30_dir < 0:
            side = "SELL"
        else:
            side = "NONE"

    if side == "NONE":
        return Signal(
            symbol_key=symbol_key,
            symbol_used=symbol_used,
            price=price,
            trend_d1=t_d1,
            trend_h4=t_h4,
            overall=overall,
            side="NONE",
            order_type="NONE",
            entry=None, sl=None, tp1=None, tp2=None, tp3=None,
            zone_name="NONE", zone_low=None, zone_high=None,
            fvg_items=find_fvg(m30),
            supports=swing_levels(h4)[0],
            resistances=swing_levels(h4)[1],
            confidence=3,
            risk_usd=ACCOUNT_BALANCE * (RISK_PCT / 100.0),
        )

    # Zones
    ob = find_order_block(m30, side)
    zone_low, zone_high = (None, None)
    zone_name = "NONE"
    if ob:
        zone_low, zone_high = float(ob[0]), float(ob[1])
        zone_name = "OrderBlock"

    fvg_items = find_fvg(m30)

    # هل السعر لمس المنطقة؟
    touched = False
    if zone_low is not None and zone_high is not None:
        mid = (zone_low + zone_high) / 2.0
        dist = abs(price - mid)
        touched = dist <= (TOUCH_ATR_MULT * atr_now)

    # نوع الأمر:
    # - إذا لمس: Market
    # - إذا بعيد: Limit على منتصف الـ OB (إذا المسافة معقولة)
    order_type = "MARKET" if touched else "LIMIT"

    entry = price if order_type == "MARKET" else None
    if order_type == "LIMIT" and zone_low is not None and zone_high is not None:
        mid = (zone_low + zone_high) / 2.0
        if abs(price - mid) <= (MAX_PENDING_DISTANCE_ATR * atr_now):
            entry = mid
        else:
            # بعيد جدًا => لا نرسل إشارة
            logging.info("[%s] Pending too far. Skip.", symbol_key)
            return None

    # SL/TP
    risk_usd = ACCOUNT_BALANCE * (RISK_PCT / 100.0)

    # SL خلف المنطقة + buffer ATR
    if side == "BUY":
        base_sl = zone_low if zone_low is not None else (price - 1.2 * atr_now)
        sl = float(base_sl - SL_BUFFER_ATR * atr_now)
        r = max(atr_now * 1.5, (price - sl))  # مسافة منطقية
        tp1 = float((entry or price) + 1.5 * r)
        tp2 = float((entry or price) + 2.5 * r)
        tp3 = float((entry or price) + 4.0 * r)

    else:
        base_sl = zone_high if zone_high is not None else (price + 1.2 * atr_now)
        sl = float(base_sl + SL_BUFFER_ATR * atr_now)
        r = max(atr_now * 1.5, (sl - price))
        tp1 = float((entry or price) - 1.5 * r)
        tp2 = float((entry or price) - 2.5 * r)
        tp3 = float((entry or price) - 4.0 * r)

    supports, resistances = swing_levels(h4)

    conf = compute_confidence(overall=overall, m30_dir=m30_dir, touched=touched, has_zone=(zone_low is not None))

    return Signal(
        symbol_key=symbol_key,
        symbol_used=symbol_used,
        price=price,
        trend_d1=t_d1,
        trend_h4=t_h4,
        overall=overall,
        side=side,
        order_type=order_type,
        entry=float(entry) if entry is not None else None,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        zone_name=zone_name,
        zone_low=zone_low,
        zone_high=zone_high,
        fvg_items=fvg_items,
        supports=supports,
        resistances=resistances,
        confidence=conf,
        risk_usd=risk_usd,
    )


# =========================
# Chart rendering (candles + EMAs)
# =========================
def plot_chart_m30(df30: pd.DataFrame, title: str) -> bytes:
    d = df30.tail(120).copy()
    d = d.reset_index()

    # Convert index/time for labels
    if "Datetime" in d.columns:
        x = d["Datetime"]
    elif "Date" in d.columns:
        x = d["Date"]
    else:
        x = d.iloc[:, 0]

    close = d["Close"]
    e20 = ema(close, EMA_FAST)
    e50 = ema(close, EMA_MID)
    e200 = ema(close, EMA_SLOW) if len(close) > EMA_SLOW else None

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.set_title(title)

    # Candles
    for i in range(len(d)):
        o = float(d.loc[i, "Open"])
        h = float(d.loc[i, "High"])
        l = float(d.loc[i, "Low"])
        c = float(d.loc[i, "Close"])

        # wick
        ax.plot([i, i], [l, h], linewidth=1)

        # body
        body_low = min(o, c)
        body_high = max(o, c)
        height = max(1e-9, body_high - body_low)
        rect = plt.Rectangle(
            (i - 0.35, body_low),
            0.7,
            height,
            fill=True,
            alpha=0.6
        )
        ax.add_patch(rect)

    # EMAs
    ax.plot(range(len(d)), e20, linewidth=1.2, label=f"EMA{EMA_FAST}")
    ax.plot(range(len(d)), e50, linewidth=1.2, label=f"EMA{EMA_MID}")
    if e200 is not None:
        ax.plot(range(len(d)), e200, linewidth=1.2, label=f"EMA{EMA_SLOW}")

    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.2)
    ax.set_xlim(0, len(d) - 1)

    # x ticks sparse
    step = max(1, len(d)//6)
    ax.set_xticks(list(range(0, len(d), step)))
    ax.set_xticklabels([str(x.iloc[i])[:16] for i in range(0, len(d), step)], rotation=0)

    import io
    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png", dpi=160)
    plt.close(fig)
    return buf.getvalue()


# =========================
# Message formatting (VIP)
# =========================
def fmt_num(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "-"
    return f"{x:.{digits}f}"

def build_caption(sig: Signal) -> str:
    risk_usd = sig.risk_usd
    overall_text = f"{sig.trend_d1:+d} / {sig.trend_h4:+d}  => Overall: {sig.overall:+d}"

    # أمر
    if sig.side == "NONE" or sig.entry is None:
        return (
            f"<b>بوت الشبح VIP • M30</b>\n"
            f"📌 <b>{sig.symbol_key}</b> ({sig.symbol_used})\n"
            f"Trend D1/H4: {overall_text}\n"
            f"Signal: ⚪ NONE\n"
            f"Price: {fmt_num(sig.price, 2)}\n"
        )

    if sig.order_type == "MARKET":
        order_line = "🟢 BUY (Market)" if sig.side == "BUY" else "🔴 SELL (Market)"
        entry_line = f"Entry: {fmt_num(sig.entry, 2)}"
    else:
        order_line = "🟢 Buy Limit" if sig.side == "BUY" else "🔴 Sell Limit"
        entry_line = f"{order_line}: {fmt_num(sig.entry, 2)}"

    zone_line = "-"
    if sig.zone_low is not None and sig.zone_high is not None:
        zone_line = f"{sig.zone_name} [{fmt_num(sig.zone_low,2)} - {fmt_num(sig.zone_high,2)}]"

    # FVG text
    fvg_txt = ""
    if sig.fvg_items:
        lines = []
        for k, a, b in sig.fvg_items:
            lines.append(f"{k}: [{fmt_num(a,2)} - {fmt_num(b,2)}]")
        fvg_txt = "\n".join(lines)
    else:
        fvg_txt = "-"

    supports_txt = ", ".join(fmt_num(x, 2) for x in sig.supports) if sig.supports else "-"
    res_txt = ", ".join(fmt_num(x, 2) for x in sig.resistances) if sig.resistances else "-"

    return (
        f"<b>🔥 نسخة M30 الاحترافية • VIP + Retest</b>\n\n"
        f"📌 <b>{sig.symbol_key}</b> ({sig.symbol_used})\n"
        f"Trend D1/H4: {overall_text}\n\n"
        f"{entry_line}\n"
        f"SL: {fmt_num(sig.sl, 2)}\n"
        f"TP1: {fmt_num(sig.tp1, 2)}\n"
        f"TP2: {fmt_num(sig.tp2, 2)}\n"
        f"TP3: {fmt_num(sig.tp3, 2)}\n\n"
        f"Zone: {zone_line}\n"
        f"Supports: {supports_txt}\n"
        f"Resistances: {res_txt}\n\n"
        f"FVG:\n{fvg_txt}\n\n"
        f"Risk: {RISK_PCT:.1f}% (~${risk_usd:.1f})\n"
        f"Confidence: {sig.confidence}/10\n"
    )


# =========================
# Cooldown control
# =========================
_last_sent: Dict[str, float] = {}

def in_cooldown(key: str) -> bool:
    if key not in _last_sent:
        return False
    return (time.time() - _last_sent[key]) < (COOLDOWN_MINUTES * 60)

def mark_sent(key: str):
    _last_sent[key] = time.time()


# =========================
# Main loop
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Set BOT_TOKEN and CHAT_ID in Railway Variables.")
        return

    logging.info("Bot started. Symbols: %s", list(SYMBOLS.keys()))
    while True:
        try:
            for k in SYMBOLS.keys():
                if in_cooldown(k):
                    continue

                sig = build_signal(k)
                if sig is None:
                    continue

                # chart from M30
                df30, _ = get_best_df(SYMBOLS[k], period="60d", interval="30m")
                if df30 is None or df30.empty:
                    continue

                title = f"{k} ({sig.symbol_used}) • M30"
                img = plot_chart_m30(df30, title=title)
                caption = build_caption(sig)

                ok = send_telegram_photo_with_caption(img, caption)
                if ok:
                    mark_sent(k)

                time.sleep(2)  # حتى لا نضغط على Telegram

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            logging.error("Loop error: %s", e)
            time.sleep(min(60, CHECK_INTERVAL_SEC))


if __name__ == "__main__":
    main()
