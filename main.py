# -*- coding: utf-8 -*-

import os
import time
import logging
import requests
import pandas as pd
import yfinance as yf


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger("ProMax_VIP")


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()


CHECK_INTERVAL_SEC = int(
    os.getenv("CHECK_INTERVAL_SEC", "300")
)

MIN_SIGNAL_SCORE = float(
    os.getenv("MIN_SIGNAL_SCORE", "70")
)


LOT = 0.01


# =========================
# SYMBOLS
# =========================

SYMBOLS = {

    "XAUUSD": "GC=F",

    "EURUSD": "EURUSD=X",

    "USDJPY": "USDJPY=X",

    "BTCUSD": "BTC-USD",

    "US100": "NQ=F",

    "US30": "^DJI",

    "GER40": "^GDAXI",

    "WTI": "CL=F"

}


# =========================
# TELEGRAM SEND
# =========================

def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        logger.warning(
            "Telegram variables missing"
        )
        return


    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )


    data = {
        "chat_id": CHAT_ID,
        "text": message
    }


    try:

        requests.post(
            url,
            data=data,
            timeout=15
        )

    except Exception as e:

        logger.error(e)
