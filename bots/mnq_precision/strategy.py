#!/usr/bin/env python3
"""
NQ Morning Precision – strategy.py
Bot reads config.json on every loop → live param reload without restart.
"""

import json
import time
import os
import logging
from pathlib import Path
from datetime import datetime, time as dtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("NQPrecision")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_params() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f).get("params", {})


def in_session(p: dict) -> bool:
    now = datetime.now().time()
    return dtime(9, 28) <= now <= dtime(13, 5)


def run():
    log.info("═══════════════════════════════════════")
    log.info("  NQ Morning Precision v2.0 – STARTED  ")
    log.info("═══════════════════════════════════════")

    while True:
        p = load_params()   # Hot-reload every tick

        if not in_session(p):
            log.info("⏳ Outside session – waiting...")
            time.sleep(60)
            continue

        log.info(
            f"📊 TICK | Account: ${p['account_size']:,.0f} | "
            f"DLL: ${p['daily_loss_limit']} | "
            f"Contracts: {p['contracts']} | "
            f"ATR mult: {p['atr_sl_mult']} | "
            f"TP1: {p['tp1_rr']}R TP2: {p['tp2_rr']}R"
        )

        # ── Placeholder for real broker connection ──────────────────────────
        # Replace this section with your broker API calls:
        #
        #   from ib_insync import IB, Future
        #   ib = IB(); ib.connect('127.0.0.1', 7497, clientId=1)
        #   bars = ib.reqHistoricalData(contract, ...)
        #   signal = detect_setup(bars, p)
        #   if signal: place_order(ib, signal, p)
        #
        # All risk params (p['daily_loss_limit'] etc.) are live from config.json
        # ───────────────────────────────────────────────────────────────────

        time.sleep(p.get("loop_interval_sec", 300))


if __name__ == "__main__":
    run()
