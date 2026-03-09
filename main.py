import os
import time
import json
import math
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Optional

import requests
import pytz

# =========================================
# CONFIG
# =========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
RISK_PCT = float(os.getenv("RISK_PCT", "1.0"))  # الافتراضي 1%
STATE_FILE = os.getenv("STATE_FILE", "ghost_bot_state.json")

REQUEST_TIMEOUT = 10

SYMBOLS = {
    "US100": "NQ=F",
    "US30": "^DJI",
    "BTC": "BTC-USD",
    "XAU": "GC=F",
}

# =========================================
# LOGGING
# =========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ghost_bot")


# =========================================
# TELEGRAM
# =========================================
def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("BOT_TOKEN or CHAT_ID missing.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            logger.error("Telegram error: %s", response.text)
    except Exception as e:
        logger.exception("Failed to send telegram message: %s", e)


# =========================================
# STATE MANAGEMENT
# =========================================
def default_state() -> Dict:
    return {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "daily_stats": {
            "losses_in_row": 0,
            "daily_pnl_pct": 0.0,
            "trading_paused": False,
            "signals_sent_today": 0
        },
        "open_positions": [],
        "closed_alerts": []
    }


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            return state
    except Exception as e:
        logger.exception("Error loading state: %s", e)
        return default_state()


def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Error saving state: %s", e)


def reset_daily_state_if_needed(state: Dict) -> Dict:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"] = today
        state["daily_stats"] = {
            "losses_in_row": 0,
            "daily_pnl_pct": 0.0,
            "trading_paused": False,
            "signals_sent_today": 0
        }
    return state


# =========================================
# HELPERS
# =========================================
def round2(x: float) -> float:
    return round(float(x), 2)


def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def in_range(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def percent_distance(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return abs(a - b) / abs(a)


# =========================================
# CANDLE / STRUCTURE LOGIC
# =========================================
def candle_body(candle: Dict) -> float:
    return abs(candle["close"] - candle["open"])


def upper_wick(candle: Dict) -> float:
    return candle["high"] - max(candle["open"], candle["close"])


def lower_wick(candle: Dict) -> float:
    return min(candle["open"], candle["close"]) - candle["low"]


def bearish_rejection_candle(candle: Dict) -> bool:
    body = candle_body(candle)
    uw = upper_wick(candle)
    lw = lower_wick(candle)

    if body <= 0:
        return False

    return candle["close"] < candle["open"] and uw > body * 1.2 and uw > lw


def bullish_rejection_candle(candle: Dict) -> bool:
    body = candle_body(candle)
    uw = upper_wick(candle)
    lw = lower_wick(candle)

    if body <= 0:
        return False

    return candle["close"] > candle["open"] and lw > body * 1.2 and lw > uw


def bearish_engulfing(prev_candle: Dict, candle: Dict) -> bool:
    return (
        prev_candle["close"] > prev_candle["open"]
        and candle["close"] < candle["open"]
        and candle["open"] >= prev_candle["close"]
        and candle["close"] <= prev_candle["open"]
    )


def bullish_engulfing(prev_candle: Dict, candle: Dict) -> bool:
    return (
        prev_candle["close"] < prev_candle["open"]
        and candle["close"] > candle["open"]
        and candle["open"] <= prev_candle["close"]
        and candle["close"] >= prev_candle["open"]
    )


def swept_liquidity_up(recent_highs: List[float], current_high: float) -> bool:
    if not recent_highs:
        return False
    return current_high > max(recent_highs)


def swept_liquidity_down(recent_lows: List[float], current_low: float) -> bool:
    if not recent_lows:
        return False
    return current_low < min(recent_lows)


def broke_structure_bearish(last_swing_low: float, current_close: float) -> bool:
    return current_close < last_swing_low


def broke_structure_bullish(last_swing_high: float, current_close: float) -> bool:
    return current_close > last_swing_high


# =========================================
# TREND / ATR / SESSION FILTERS
# =========================================
def is_bearish_trend(d1_trend: int, h4_trend: int) -> bool:
    return d1_trend < 0 and h4_trend < 0


def is_bullish_trend(d1_trend: int, h4_trend: int) -> bool:
    return d1_trend > 0 and h4_trend > 0


def atr_filter(atr_value: float, price: float, min_pct=0.0015, max_pct=0.008) -> bool:
    if price <= 0:
        return False
    atr_pct = atr_value / price
    return min_pct <= atr_pct <= max_pct


def is_ny_session() -> bool:
    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.now(ny_tz)

    # من 9:30 إلى 12:59 تقريبًا
    if now_ny.hour == 9 and now_ny.minute >= 30:
        return True
    if 10 <= now_ny.hour <= 12:
        return True
    return False


# =========================================
# CORRELATION / RISK CONTROLS
# =========================================
def correlated_assets_block(symbol: str, open_positions: List[Dict]) -> bool:
    correlated_groups = [
        {"US100", "US30"}
    ]

    for group in correlated_groups:
        if symbol in group:
            for pos in open_positions:
                if pos["status"] == "OPEN" and pos["symbol"] in group:
                    return True
    return False


def calculate_position_size(balance: float, risk_pct: float, entry: float, stop: float) -> float:
    risk_amount = balance * (risk_pct / 100.0)
    stop_distance = abs(entry - stop)

    if stop_distance <= 0:
        return 0.0

    size = risk_amount / stop_distance
    return round(size, 4)


# =========================================
# ENTRY VALIDATION
# =========================================
def valid_sell_entry(data: Dict) -> Tuple[bool, str]:
    price = data["price"]
    zone_low = data["zone_low"]
    zone_high = data["zone_high"]
    d1_trend = data["d1_trend"]
    h4_trend = data["h4_trend"]
    m15_trend = data["m15_trend"]
    confirm_candle = data["confirm_candle"]
    prev_candle = data["prev_candle"]
    recent_highs = data["recent_highs"]
    last_swing_low = data["last_swing_low"]
    current_high = data["current_high"]
    current_close = data["current_close"]
    atr = data["atr"]

    if not is_ny_session():
        return False, "Outside NY session"

    if not is_bearish_trend(d1_trend, h4_trend):
        return False, "D1/H4 not bearish"

    if m15_trend > 0:
        return False, "M15 still bullish"

    if not in_range(price, zone_low, zone_high):
        return False, "Price not inside sell zone"

    if not atr_filter(atr, price):
        return False, "ATR filter failed"

    liq = swept_liquidity_up(recent_highs, current_high)
    rej = bearish_rejection_candle(confirm_candle) or bearish_engulfing(prev_candle, confirm_candle)
    bos = broke_structure_bearish(last_swing_low, current_close)

    if not liq:
        return False, "No liquidity sweep"
    if not rej:
        return False, "No bearish rejection candle"
    if not bos:
        return False, "No bearish BOS"

    return True, "Valid SELL setup"


def valid_buy_entry(data: Dict) -> Tuple[bool, str]:
    price = data["price"]
    zone_low = data["zone_low"]
    zone_high = data["zone_high"]
    d1_trend = data["d1_trend"]
    h4_trend = data["h4_trend"]
    m15_trend = data["m15_trend"]
    confirm_candle = data["confirm_candle"]
    prev_candle = data["prev_candle"]
    recent_lows = data["recent_lows"]
    last_swing_high = data["last_swing_high"]
    current_low = data["current_low"]
    current_close = data["current_close"]
    atr = data["atr"]

    if not is_ny_session():
        return False, "Outside NY session"

    if not is_bullish_trend(d1_trend, h4_trend):
        return False, "D1/H4 not bullish"

    if m15_trend < 0:
        return False, "M15 still bearish"

    if not in_range(price, zone_low, zone_high):
        return False, "Price not inside buy zone"

    if not atr_filter(atr, price):
        return False, "ATR filter failed"

    liq = swept_liquidity_down(recent_lows, current_low)
    rej = bullish_rejection_candle(confirm_candle) or bullish_engulfing(prev_candle, confirm_candle)
    bos = broke_structure_bullish(last_swing_high, current_close)

    if not liq:
        return False, "No liquidity sweep"
    if not rej:
        return False, "No bullish rejection candle"
    if not bos:
        return False, "No bullish BOS"

    return True, "Valid BUY setup"


# =========================================
# EXIT / CLOSE LOGIC
# =========================================
def classify_exit_signal(reversal_candle: bool, bos: bool, trend_flip: bool) -> str:
    score = 0
    if reversal_candle:
        score += 1
    if bos:
        score += 2
    if trend_flip:
        score += 2

    if score >= 4:
        return "FULL_EXIT"
    if score >= 2:
        return "CLOSE_ALERT"
    return "HOLD"


def should_close_sell_trade(position: Dict, data: Dict) -> Tuple[bool, str, str]:
    current_candle = data["confirm_candle"]
    current_close = data["current_close"]
    last_swing_high = data["last_swing_high"]
    h4_trend = data["h4_trend"]
    m15_trend = data["m15_trend"]
    bars_since_entry = data.get("bars_since_entry", 0)
    reached_tp1 = data.get("reached_tp1", False)

    reversal_candle = bullish_rejection_candle(current_candle)
    bos_bullish = broke_structure_bullish(last_swing_high, current_close)
    trend_flip = h4_trend > 0 and m15_trend > 0

    exit_type = classify_exit_signal(reversal_candle, bos_bullish, trend_flip)

    if bars_since_entry >= 8 and not reached_tp1 and reversal_candle:
        return True, "Time-based weakness against SELL", "CLOSE_ALERT"

    if exit_type == "FULL_EXIT":
        return True, "Bullish reversal + bullish BOS + trend flip", "FULL_EXIT"

    if exit_type == "CLOSE_ALERT":
        return True, "Possible bullish reversal against SELL", "CLOSE_ALERT"

    return False, "No close condition", "HOLD"


def should_close_buy_trade(position: Dict, data: Dict) -> Tuple[bool, str, str]:
    current_candle = data["confirm_candle"]
    current_close = data["current_close"]
    last_swing_low = data["last_swing_low"]
    h4_trend = data["h4_trend"]
    m15_trend = data["m15_trend"]
    bars_since_entry = data.get("bars_since_entry", 0)
    reached_tp1 = data.get("reached_tp1", False)

    reversal_candle = bearish_rejection_candle(current_candle)
    bos_bearish = broke_structure_bearish(last_swing_low, current_close)
    trend_flip = h4_trend < 0 and m15_trend < 0

    exit_type = classify_exit_signal(reversal_candle, bos_bearish, trend_flip)

    if bars_since_entry >= 8 and not reached_tp1 and reversal_candle:
        return True, "Time-based weakness against BUY", "CLOSE_ALERT"

    if exit_type == "FULL_EXIT":
        return True, "Bearish reversal + bearish BOS + trend flip", "FULL_EXIT"

    if exit_type == "CLOSE_ALERT":
        return True, "Possible bearish reversal against BUY", "CLOSE_ALERT"

    return False, "No close condition", "HOLD"


# =========================================
# MESSAGE BUILDERS
# =========================================
def build_entry_message(position: Dict) -> str:
    side_icon = "🔴" if position["side"] == "SELL" else "🟢"

    return (
        f"<b>بوت الشبح المطور 👻</b>\n"
        f"{position['symbol']}\n\n"
        f"{side_icon} <b>{position['side']} CONFIRMED</b>\n"
        f"Entry: <b>{position['entry']}</b>\n"
        f"SL: <b>{position['sl']}</b>\n"
        f"TP1: <b>{position['tp1']}</b>\n"
        f"TP2: <b>{position['tp2']}</b>\n"
        f"TP3: <b>{position['tp3']}</b>\n"
        f"Zone: [{position['zone_low']} - {position['zone_high']}]\n"
        f"Confidence: <b>{position['confidence']}/10</b>\n"
        f"Reason: {position['reason']}"
    )


def build_close_message(position: Dict, current_price: float, reason: str, exit_type: str) -> str:
    icon = "⚠️" if exit_type == "CLOSE_ALERT" else "⛔"

    return (
        f"<b>بوت الشبح - {exit_type}</b>\n"
        f"{position['symbol']}\n\n"
        f"{icon} <b>{exit_type}</b>\n"
        f"Side: <b>{position['side']}</b>\n"
        f"Entry: <b>{position['entry']}</b>\n"
        f"Current: <b>{round2(current_price)}</b>\n"
        f"SL: <b>{position['sl']}</b>\n"
        f"Reason: {reason}"
    )


# =========================================
# TRADE / DAILY STATS
# =========================================
def update_after_closed_trade(state: Dict, pnl_pct: float) -> None:
    stats = state["daily_stats"]
    stats["daily_pnl_pct"] += pnl_pct

    if pnl_pct < 0:
        stats["losses_in_row"] += 1
    else:
        stats["losses_in_row"] = 0

    if stats["losses_in_row"] >= 2 or stats["daily_pnl_pct"] <= -4.0:
        stats["trading_paused"] = True
        send_telegram_message(
            "🛑 <b>تم إيقاف التداول لبقية اليوم</b>\n"
            "السبب: حد الخسائر اليومية أو خسارتين متتاليتين."
        )


# =========================================
# POSITION TRACKING
# =========================================
def create_position(symbol: str, side: str, data: Dict, account_balance: float) -> Dict:
    entry = round2(data["entry"])
    sl = round2(data["sl"])
    tp1 = round2(data["tp1"])
    tp2 = round2(data["tp2"])
    tp3 = round2(data["tp3"])

    size = calculate_position_size(account_balance, RISK_PCT, entry, sl)

    return {
        "id": f"{symbol}_{side}_{int(time.time())}",
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "zone_low": round2(data["zone_low"]),
        "zone_high": round2(data["zone_high"]),
        "confidence": data.get("confidence", 8),
        "reason": data.get("reason", "Confirmed setup"),
        "size": size,
        "status": "OPEN",
        "created_at": datetime.utcnow().isoformat(),
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "close_alert_sent": False,
        "full_exit_sent": False
    }


def process_position_targets(position: Dict, current_price: float) -> Optional[float]:
    """
    يرجع pnl تقريبي عند الإغلاق النهائي فقط.
    """

    if position["side"] == "BUY":
        if current_price >= position["tp1"] and not position["tp1_hit"]:
            position["tp1_hit"] = True
            send_telegram_message(
                f"✅ <b>{position['symbol']}</b>\n"
                f"TP1 HIT\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )

        if current_price >= position["tp2"] and not position["tp2_hit"]:
            position["tp2_hit"] = True
            send_telegram_message(
                f"✅ <b>{position['symbol']}</b>\n"
                f"TP2 HIT\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )

        if current_price >= position["tp3"] and not position["tp3_hit"]:
            position["tp3_hit"] = True
            position["status"] = "CLOSED"
            send_telegram_message(
                f"🏁 <b>{position['symbol']}</b>\n"
                f"TP3 HIT - TRADE CLOSED\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )
            return 3.0

        if current_price <= position["sl"]:
            position["status"] = "CLOSED"
            send_telegram_message(
                f"❌ <b>{position['symbol']}</b>\n"
                f"SL HIT - TRADE CLOSED\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )
            return -float(RISK_PCT)

    elif position["side"] == "SELL":
        if current_price <= position["tp1"] and not position["tp1_hit"]:
            position["tp1_hit"] = True
            send_telegram_message(
                f"✅ <b>{position['symbol']}</b>\n"
                f"TP1 HIT\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )

        if current_price <= position["tp2"] and not position["tp2_hit"]:
            position["tp2_hit"] = True
            send_telegram_message(
                f"✅ <b>{position['symbol']}</b>\n"
                f"TP2 HIT\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )

        if current_price <= position["tp3"] and not position["tp3_hit"]:
            position["tp3_hit"] = True
            position["status"] = "CLOSED"
            send_telegram_message(
                f"🏁 <b>{position['symbol']}</b>\n"
                f"TP3 HIT - TRADE CLOSED\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )
            return 3.0

        if current_price >= position["sl"]:
            position["status"] = "CLOSED"
            send_telegram_message(
                f"❌ <b>{position['symbol']}</b>\n"
                f"SL HIT - TRADE CLOSED\n"
                f"Side: {position['side']}\n"
                f"Price: {round2(current_price)}"
            )
            return -float(RISK_PCT)

    return None


def monitor_open_positions(state: Dict, market_data: Dict[str, Dict]) -> None:
    for position in state["open_positions"]:
        if position["status"] != "OPEN":
            continue

        symbol = position["symbol"]
        if symbol not in market_data:
            continue

        data = market_data[symbol]
        current_price = data["price"]

        # 1) فحص الأهداف والوقف
        pnl_result = process_position_targets(position, current_price)
        if pnl_result is not None:
            update_after_closed_trade(state, pnl_result)
            continue

        # 2) فحص انعكاس الصفقة
        if position["side"] == "SELL":
            close_now, reason, exit_type = should_close_sell_trade(position, data)
        else:
            close_now, reason, exit_type = should_close_buy_trade(position, data)

        if not close_now:
            continue

        if exit_type == "CLOSE_ALERT" and not position["close_alert_sent"]:
            send_telegram_message(build_close_message(position, current_price, reason, exit_type))
            position["close_alert_sent"] = True

        if exit_type == "FULL_EXIT" and not position["full_exit_sent"]:
            send_telegram_message(build_close_message(position, current_price, reason, exit_type))
            position["full_exit_sent"] = True


# =========================================
# SIGNAL SCAN
# =========================================
def should_send_signal(state: Dict, symbol: str, data: Dict) -> Tuple[bool, str, Optional[str]]:
    stats = state["daily_stats"]

    if stats["trading_paused"]:
        return False, "Trading paused", None

    if stats["signals_sent_today"] >= 2:
        return False, "Daily signal limit reached", None

    if correlated_assets_block(symbol, state["open_positions"]):
        return False, "Correlated asset already open", None

    side = data.get("side", "").upper()

    if side == "SELL":
        ok, reason = valid_sell_entry(data)
        return ok, reason, "SELL" if ok else None

    if side == "BUY":
        ok, reason = valid_buy_entry(data)
        return ok, reason, "BUY" if ok else None

    return False, "Unknown side", None


def scan_for_new_entries(state: Dict, market_data: Dict[str, Dict], account_balance: float) -> None:
    for symbol, data in market_data.items():
        if any(p["symbol"] == symbol and p["status"] == "OPEN" for p in state["open_positions"]):
            continue

        ok, reason, side = should_send_signal(state, symbol, data)
        if not ok or not side:
            logger.info("%s skipped: %s", symbol, reason)
            continue

        data["reason"] = reason
        position = create_position(symbol, side, data, account_balance)
        state["open_positions"].append(position)
        state["daily_stats"]["signals_sent_today"] += 1
        send_telegram_message(build_entry_message(position))


# =========================================
# MARKET DATA PLACEHOLDER
# =========================================
def get_mock_market_data() -> Dict[str, Dict]:
    """
    هذه مجرد بيانات تجريبية.
    استبدلها ببياناتك الحقيقية من yfinance أو API آخر.
    """

    return {
        "US100": {
            "symbol": "US100",
            "side": "SELL",
            "price": 24695.0,
            "entry": 24699.88,
            "sl": 24757.33,
            "tp1": 24668.0,
            "tp2": 24608.75,
            "tp3": 24592.75,
            "zone_low": 24653.75,
            "zone_high": 24746.0,
            "confidence": 8,

            "d1_trend": -1,
            "h4_trend": -1,
            "m15_trend": -1,

            "atr": 55.0,

            "recent_highs": [24670.0, 24682.0, 24690.0],
            "recent_lows": [24580.0, 24570.0, 24565.0],

            "last_swing_low": 24685.0,
            "last_swing_high": 24720.0,

            "current_high": 24696.5,
            "current_low": 24660.0,
            "current_close": 24680.0,

            "confirm_candle": {
                "open": 24692.0,
                "high": 24702.0,
                "low": 24678.0,
                "close": 24681.0
            },
            "prev_candle": {
                "open": 24685.0,
                "high": 24694.0,
                "low": 24680.0,
                "close": 24691.0
            },

            "bars_since_entry": 3,
            "reached_tp1": False
        },

        "US30": {
            "symbol": "US30",
            "side": "SELL",
            "price": 47150.0,
            "entry": 47167.88,
            "sl": 47245.94,
            "tp1": 47089.81,
            "tp2": 47011.74,
            "tp3": 46933.68,
            "zone_low": 47109.14,
            "zone_high": 47226.61,
            "confidence": 8,

            "d1_trend": -1,
            "h4_trend": -1,
            "m15_trend": -1,

            "atr": 85.0,

            "recent_highs": [47130.0, 47140.0, 47149.0],
            "recent_lows": [46990.0, 46980.0, 46970.0],

            "last_swing_low": 47120.0,
            "last_swing_high": 47190.0,

            "current_high": 47155.0,
            "current_low": 47100.0,
            "current_close": 47110.0,

            "confirm_candle": {
                "open": 47145.0,
                "high": 47165.0,
                "low": 47105.0,
                "close": 47112.0
            },
            "prev_candle": {
                "open": 47120.0,
                "high": 47150.0,
                "low": 47115.0,
                "close": 47140.0
            },

            "bars_since_entry": 2,
            "reached_tp1": False
        }
    }


# =========================================
# MAIN LOOP
# =========================================
def main():
    account_balance = float(os.getenv("ACCOUNT_BALANCE", "100"))

    send_telegram_message("👻 <b>بوت الشبح المطور بدأ التشغيل</b>")

    while True:
        try:
            state = load_state()
            state = reset_daily_state_if_needed(state)

            market_data = get_mock_market_data()

            monitor_open_positions(state, market_data)
            scan_for_new_entries(state, market_data, account_balance)

            # تنظيف الصفقات المغلقة من القائمة إن أردت
            # state["open_positions"] = [p for p in state["open_positions"] if p["status"] == "OPEN"]

            save_state(state)
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Bot stopped manually.")
            break
        except Exception as e:
            logger.exception("Main loop error: %s", e)
            send_telegram_message(f"⚠️ Bot error: {str(e)}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
