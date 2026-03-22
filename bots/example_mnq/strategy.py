#!/usr/bin/env python3
"""
Příklad bota kompatibilního s Trading Hub.

1) Obsahuje funkci on_bar() pro backtest
2) Volá log_trade() pro zápis do Trade Logu
3) Proměnné k doplnění jsou označeny 'DOPLNIT'
"""

import sys
import time
import os
from pathlib import Path
from datetime import datetime

# ── Parametry k doplnění přes Trading Hub UI ─────────────────────────────────
API_KEY    = 'DOPLNIT'
ACCOUNT_ID = 'DOPLNIT'
CONTRACT   = 'DOPLNIT'

# ── Přidej root cestu pro import hub ─────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from hub import log_trade

BOT_NAME = os.environ.get('BOT_NAME', 'NQ Příklad')
BOT_ID   = os.environ.get('BOT_ID', 'example')


# ═══ BACKTEST FUNKCE ══════════════════════════════════════════════════════════
# Tato funkce je volána backtesterem svíčku po svíčce.
# df = DataFrame se sloupci: open, high, low, close, volume
# i  = index aktuální svíčky
# params = slovník parametrů z config.json

def on_bar(df, i, params):
    """
    Jednoduchý EMA crossover příklad.
    Vrátí signál nebo None.
    """
    if i < 25:
        return None

    import numpy as np

    close = df['close']
    ema9  = close.ewm(span=9,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()

    curr_price = close.iloc[-1]
    atr_val = (df['high'] - df['low']).rolling(14).mean().iloc[-1]

    # LONG: EMA9 překříží EMA21 zdola
    if ema9.iloc[-1] > ema21.iloc[-1] and ema9.iloc[-2] <= ema21.iloc[-2]:
        sl = curr_price - atr_val * 1.5
        tp = curr_price + atr_val * 2.5
        return {"type": "LONG", "entry": curr_price, "sl": sl, "tp1": tp}

    # SHORT: EMA9 překříží EMA21 shora
    if ema9.iloc[-1] < ema21.iloc[-1] and ema9.iloc[-2] >= ema21.iloc[-2]:
        sl = curr_price + atr_val * 1.5
        tp = curr_price - atr_val * 2.5
        return {"type": "SHORT", "entry": curr_price, "sl": sl, "tp1": tp}

    return None


# ═══ LIVE TRADING LOOP ════════════════════════════════════════════════════════

def run():
    print(f"[{BOT_NAME}] Spouštím... API_KEY={API_KEY[:8]}... CONTRACT={CONTRACT}")

    while True:
        now = datetime.now()
        print(f"[{BOT_NAME}] Tick {now.strftime('%H:%M:%S')} | Připojeno k {CONTRACT}")

        # ── Zde přidej logiku napojení na broker API ──────────────────────
        # Příklad záznamu tradu po uzavření pozice:
        #
        # log_trade(
        #     bot_id     = BOT_ID,
        #     bot_name   = BOT_NAME,
        #     instrument = CONTRACT,
        #     direction  = "BUY",
        #     contracts  = 2,
        #     entry      = 19250.0,
        #     exit_price = 19275.0,
        #     sl         = 19238.0,
        #     tp         = 19275.0,
        #     pnl        = 94.0,
        #     entry_time = "2025-03-18 09:47:32",
        #     exit_time  = "2025-03-18 10:23:15",
        # )

        time.sleep(300)  # Čekej 5 minut na další tick


if __name__ == '__main__':
    run()
