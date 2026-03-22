#!/usr/bin/env python3
"""RSI Scalper – strategy.py"""
import json, time, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("RSIScalper")
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_params():
    with open(CONFIG_PATH) as f:
        return json.load(f).get("params", {})

def run():
    log.info("RSI Scalper v1.0 – STARTED")
    while True:
        p = load_params()
        log.info(f"📈 TICK | RSI({p['rsi_period']}) OS={p['rsi_oversold']} OB={p['rsi_overbought']} | TP={p['tp_points']}pts SL={p['sl_points']}pts")
        # Add your broker logic here
        time.sleep(p.get("loop_interval_sec", 60))

if __name__ == "__main__":
    run()
