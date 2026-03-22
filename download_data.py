#!/usr/bin/env python3
"""
Tradovate Historical Data Downloader
=====================================
Stáhne historická data pro celý rok 2025 + 2026 (MNQ)
Automaticky spojí všechny čtvrtletní kontrakty do jednoho CSV.

Spuštění:  python download_data.py
Výstup:    backtest_data/MNQ_1min_2025_2026.csv
           backtest_data/MNQ_3min_2025_2026.csv
           backtest_data/MNQ_15min_2025_2026.csv
"""

import asyncio
import json
import aiohttp
import websockets
import pandas as pd
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════
#  ⚙️  VYPLŇ SVÉ PŘIHLAŠOVACÍ ÚDAJE
# ══════════════════════════════════════════════════════════

USERNAME    = "DOPLNIT"       # tvůj Tradovate email
PASSWORD    = "DOPLNIT"       # tvoje Tradovate heslo
APP_ID      = "Sample App"
APP_VERSION = "1.0"

# Použij DEMO účet? (True = demo, False = live)
USE_DEMO = False

# ══════════════════════════════════════════════════════════
#  📅  KONTRAKTY — 2025 + 2026
#  MNQ: H=Březen, M=Červen, U=Září, Z=Prosinec
#  Číslo: 5=2025, 6=2026
# ══════════════════════════════════════════════════════════

CONTRACTS = [
    # symbol       od            do
    ("MNQH5",  "2025-01-01", "2025-03-21"),  # Leden–Březen 2025
    ("MNQM5",  "2025-03-21", "2025-06-20"),  # Duben–Červen 2025
    ("MNQU5",  "2025-06-20", "2025-09-19"),  # Červenec–Září 2025
    ("MNQZ5",  "2025-09-19", "2025-12-19"),  # Říjen–Prosinec 2025
    ("MNQH6",  "2025-12-19", "2026-03-20"),  # Leden–Březen 2026
    ("MNQM6",  "2026-03-20", "2026-12-31"),  # Duben–Červen 2026 (aktuální)
]

TIMEFRAMES = [1, 3, 15]   # minuty
OUTPUT_DIR = Path(__file__).parent / "backtest_data"

# ══════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════

if USE_DEMO:
    REST_URL = "https://demo.tradovateapi.com/v1"
    WS_URL   = "wss://md-demo.tradovateapi.com/v1/websocket"
else:
    REST_URL = "https://live.tradovateapi.com/v1"
    WS_URL   = "wss://md.tradovateapi.com/v1/websocket"


async def get_token(session):
    payload = {
        "name": USERNAME, "password": PASSWORD,
        "appId": APP_ID, "appVersion": APP_VERSION,
        "cids": "[]", "sec": "",
    }
    async with session.post(f"{REST_URL}/auth/accesstokenrequest", json=payload) as r:
        data = await r.json()
        if "accessToken" not in data:
            raise Exception(f"Přihlášení selhalo: {data.get('errorText', str(data))}")
        print(f"✓ Přihlášen jako {USERNAME}")
        return data["accessToken"]


async def fetch_bars(token, symbol, minutes, date_from, date_to):
    """Stáhne bary pro jeden kontrakt a jedno období."""
    bars = []
    dt_from = datetime.fromisoformat(date_from)
    dt_to   = datetime.fromisoformat(date_to)
    # Omezeně na dnešek
    now = datetime.utcnow()
    if dt_to > now:
        dt_to = now

    if dt_from >= dt_to:
        return []

    try:
        async with websockets.connect(WS_URL, ping_interval=20) as ws:
            # Auth
            await ws.send(f"authorize\n1\n\n{token}")
            await asyncio.wait_for(ws.recv(), timeout=10)

            payload = {
                "symbol": symbol,
                "chartDescription": {
                    "underlyingType":  "MinuteBar",
                    "elementSize":     minutes,
                    "elementSizeUnit": "UnderlyingUnits",
                    "withHistogram":   False,
                },
                "timeRange": {
                    "closestTimestamp": dt_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "asFarAsTimestamp": dt_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                },
            }
            await ws.send(f"md/getchart\n2\n\n{json.dumps(payload)}")

            # Čekej na data
            tries = 0
            while tries < 15:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    tries += 1
                    continue

                if not msg.strip():
                    continue

                parts = msg.split("\n", 3)
                if len(parts) >= 4:
                    try:
                        body = json.loads(parts[3])
                        if isinstance(body, dict) and "bars" in body:
                            for b in body["bars"]:
                                bars.append({
                                    "datetime": b.get("timestamp", "")[:19].replace("T", " "),
                                    "open":     b.get("open",  0),
                                    "high":     b.get("high",  0),
                                    "low":      b.get("low",   0),
                                    "close":    b.get("close", 0),
                                    "volume":   b.get("upVolume", 0) + b.get("downVolume", 0),
                                })
                            break
                    except Exception:
                        pass
                tries += 1

    except Exception as e:
        print(f"    ⚠️  WebSocket chyba pro {symbol}: {e}")

    return bars


async def download_timeframe(token, minutes):
    """Stáhne a spojí všechny kontrakty pro jeden timeframe."""
    print(f"\n{'═'*55}")
    print(f"  ⏱  Timeframe: {minutes} min")
    print(f"{'═'*55}")

    all_bars = []

    for symbol, date_from, date_to in CONTRACTS:
        # Přeskočit kontrakty v budoucnosti
        if datetime.fromisoformat(date_from) > datetime.utcnow():
            continue

        print(f"  📥 {symbol}  {date_from} → {date_to}")
        bars = await fetch_bars(token, symbol, minutes, date_from, date_to)

        if bars:
            all_bars.extend(bars)
            print(f"     ✓ {len(bars)} barů")
        else:
            print(f"     ⚠️  Žádná data (kontrakt možná expirovál nebo ještě nezačal)")

        await asyncio.sleep(1)  # Pauza mezi kontrakty

    if not all_bars:
        print(f"  ❌ Žádná data pro {minutes}min")
        return

    # Spoj, seřaď a odstraň duplikáty
    df = pd.DataFrame(all_bars)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

    # Ulož
    OUTPUT_DIR.mkdir(exist_ok=True)
    filename = OUTPUT_DIR / f"MNQ_{minutes}min_2025_2026.csv"
    df.to_csv(filename, index=False)

    print(f"\n  💾 Uloženo: {filename.name}")
    print(f"     📊 Celkem barů: {len(df):,}")
    print(f"     📅 Od: {df['datetime'].min()}")
    print(f"     📅 Do: {df['datetime'].max()}")


async def main():
    print("\n" + "═"*55)
    print("  📊  TRADOVATE DATA DOWNLOADER")
    print("  Rok 2025 + 2026 | MNQ 1min, 3min, 15min")
    print("═"*55)

    if "DOPLNIT" in (USERNAME, PASSWORD):
        print("""
  ⚠️  ZASTAV SE — nejdřív vyplň přihlašovací údaje!

  1. Otevři soubor download_data.py v Poznámkovém bloku
  2. Na řádku USERNAME vlož svůj Tradovate email
  3. Na řádku PASSWORD vlož své Tradovate heslo
  4. Ulož soubor a spusť znovu
        """)
        input("Stiskni Enter pro zavření...")
        return

    async with aiohttp.ClientSession() as session:
        token = await get_token(session)

    for minutes in TIMEFRAMES:
        await download_timeframe(token, minutes)

    print(f"\n{'═'*55}")
    print("  ✅  HOTOVO!")
    print(f"  📁  Soubory jsou ve složce: backtest_data/")
    print(f"      MNQ_1min_2025_2026.csv")
    print(f"      MNQ_3min_2025_2026.csv")
    print(f"      MNQ_15min_2025_2026.csv")
    print(f"{'═'*55}\n")
    input("Stiskni Enter pro zavření...")


if __name__ == "__main__":
    asyncio.run(main())
