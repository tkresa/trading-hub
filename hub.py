"""
hub.py – Helper knihovna pro boty.
Každý bot importuje log_trade() a volá ji po uzavření pozice.

Použití v botu:
    from hub import log_trade
    log_trade(bot_id="mnq_bot", bot_name="Pepa", ...)
"""

import sys
import os
from pathlib import Path

# Přidej root složku do sys.path aby import fungoval odkudkoliv
sys.path.insert(0, str(Path(__file__).parent))

from database import log_trade as _db_log_trade


def log_trade(
    bot_id:     str,
    bot_name:   str,
    instrument: str,
    direction:  str,       # "BUY" nebo "SELL"
    contracts:  int,
    entry:      float,
    exit_price: float,
    sl:         float,
    tp:         float,
    pnl:        float,
    entry_time: str,       # ISO formát: "2025-03-18 09:47:32"
    exit_time:  str,
):
    """
    Zapíše uzavřený trade do Trading Hub databáze.
    Volej vždy po uzavření pozice.
    """
    _db_log_trade(
        bot_id=bot_id,
        bot_name=bot_name,
        instrument=instrument,
        direction=direction.upper(),
        contracts=contracts,
        entry=entry,
        exit_price=exit_price,
        sl=sl,
        tp=tp,
        pnl=pnl,
        entry_time=entry_time,
        exit_time=exit_time,
    )
    print(
        f"[LOG_TRADE] {bot_name} | {direction.upper()} {contracts}x {instrument} "
        f"| PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}"
    )
