# -*- coding: utf-8 -*-
import os
import io
import time
import json
import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

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
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

CHECK_INTERVAL_SEC = 180
EMA_FAST = 20
EMA_SLOW = 50


# =========================
# SYMBOLS
# =========================
SYMBOLS = {
    "XAU": "GC=F",
    "US100": "NQ=F",
    "US30": "^DJI",
    "SPX": "^GSPC",
    "GER40CASH": "^GDAXI",
    "OIL": "CL=F",
    "BRENTCASH": "BZ=F",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
}


# =========================
# TELEGRAM
# =========================
def tg_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def tg_send_message(text):
    try:
        requests.post(
            tg_url("sendMessage"),
            json={"chat_id": CHAT_ID, "text": text}
        )
    except:
        pass


def tg_send_photo(caption, img):
    try:
        files = {"photo": ("chart.png", img)}
        data = {"chat_id": CHAT_ID, "caption": caption}
        requests.post(tg_url("sendPhoto"), files=files, data=data)
    except:
        pass


# =========================
# DATA
# =========================
def get_data(symbol):
    df = yf.download(symbol, period="30d", interval="30m", progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    return df


def ema(series, n):
    return series.ewm(span=n).mean()


def atr(df):
    h = df["High"]
    l = df["Low"]
    c = df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h-l),(h-prev).abs(),(l-prev).abs()],axis=1).max(axis=1)
    return tr.rolling(14).mean()


# =========================
# TREND
# =========================
def trend(df):
    c = df["Close"]
    f = ema(c, EMA_FAST)
    s = ema(c, EMA_SLOW)

    if f.iloc[-1] > s.iloc[-1]:
        return 1
    if f.iloc[-1] < s.iloc[-1]:
        return -1
    return 0


# =========================
# SIGNAL
# =========================
@dataclass
class Signal:
    label: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float


def build_signal(label, symbol):

    df = get_data(symbol)
    if df is None:
        return None

    tr = trend(df)
    px = df["Close"].iloc[-1]
    a = atr(df).iloc[-1]

    if tr == 1:
        side = "BUY"
        entry = px
        sl = px - a*1.5
    elif tr == -1:
        side = "SELL"
        entry = px
        sl = px + a*1.5
    else:
        return None

    r = abs(entry-sl)

    tp1 = entry + r if side=="BUY" else entry - r
    tp2 = entry + r*2 if side=="BUY" else entry - r*2
    tp3 = entry + r*3 if side=="BUY" else entry - r*3

    return Signal(label,side,entry,sl,tp1,tp2,tp3)


# =========================
# CHART
# =========================
def render_chart(df,signal):

    d = df.tail(100)

    x = list(range(len(d)))

    fig = plt.figure(figsize=(10,5))
    ax = plt.gca()

    for i in range(len(d)):
        o=d["Open"].iloc[i]
        h=d["High"].iloc[i]
        l=d["Low"].iloc[i]
        c=d["Close"].iloc[i]

        ax.plot([x[i],x[i]],[l,h])

        rect = plt.Rectangle(
            (x[i]-0.3,min(o,c)),
            0.6,
            abs(o-c)
        )
        ax.add_patch(rect)

    ax.axhline(signal.entry)
    ax.axhline(signal.sl)
    ax.axhline(signal.tp1)
    ax.axhline(signal.tp2)
    ax.axhline(signal.tp3)

    ax.set_title(signal.label)

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf,format="png")
    plt.close(fig)

    buf.seek(0)
    return buf.read()


# =========================
# FORMAT
# =========================
def format_signal(sig):

    side="🟢 BUY" if sig.side=="BUY" else "🔴 SELL"

    return f"""
🔥 VIP SIGNAL

{sig.label}

{side}
Entry: {sig.entry:.2f}

SL: {sig.sl:.2f}

TP1: {sig.tp1:.2f}
TP2: {sig.tp2:.2f}
TP3: {sig.tp3:.2f}
"""


# =========================
# DAILY REPORT
# =========================
def daily_report(wins,losses):

    closed = wins+losses

    if closed == 0:
        winrate = 0
    else:
        winrate = (wins/closed)*100

    return f"""
📊 Daily Report

Wins: {wins}
Losses: {losses}

WinRate: {winrate:.1f}%
"""


# =========================
# MAIN LOOP
# =========================
def main():

    tg_send_message("VIP BOT STARTED")

    while True:

        try:

            for label,symbol in SYMBOLS.items():

                sig = build_signal(label,symbol)

                if sig is None:
                    continue

                df = get_data(symbol)
                img = render_chart(df,sig)

                caption = format_signal(sig)

                tg_send_photo(caption,img)

                time.sleep(2)

        except Exception as e:
            logger.exception(e)

        time.sleep(CHECK_INTERVAL_SEC)


# =========================
# START
# =========================
if __name__ == "__main__":
    main()
