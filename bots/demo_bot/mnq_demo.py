"""
=============================================================
  MNQ Demo Bot – Trading Hub Test
  Strategie: EMA Crossover + Heikin Ashi filtr na 3m TF
  Kompatibilní s: Backtest, Optimalizace, Live Trading
=============================================================

NASTAVENÍ:
    Vyplň přes Trading Hub UI (automaticky detekuje DOPLNIT)

OPTIMALIZACE (označeno # opt: min-max):
    EMA_FAST, EMA_SLOW, SL_POINTS, TP_MULT

LOGIKA:
    LONG:  EMA_FAST překříží EMA_SLOW zdola + HA zelená
    SHORT: EMA_FAST překříží EMA_SLOW shora + HA červená
    EXIT:  Trailing SL na low/high předchozí svíčky

=============================================================
"""

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------
#  PŘIPOJENÍ – vyplň přes Trading Hub UI
# ---------------------------------------------------------
USERNAME    = os.getenv("TOPSTEPX_USERNAME",  "DOPLNIT")
API_KEY     = os.getenv("TOPSTEPX_API_KEY",   "DOPLNIT")
ACCOUNT_ID  = int(os.getenv("TOPSTEPX_ACCOUNT_ID", "0"))
CONTRACT_ID = os.getenv("MNQ_CONTRACT_ID",    "DOPLNIT")

# ---------------------------------------------------------
#  PARAMETRY STRATEGIE
#  Označeno # opt: min-max pro optimalizaci v Trading Hub
# ---------------------------------------------------------
EMA_FAST      = 9     # opt: 5-21
EMA_SLOW      = 21    # opt: 15-50
SL_POINTS     = 18    # opt: 8-40,2
TP_MULT       = 0     # opt: 0-3,0.5   (0 = pouze trailing SL)

# ---------------------------------------------------------
#  OSTATNÍ NASTAVENÍ
# ---------------------------------------------------------
TIMEFRAME_MIN  = 3
BARS_NEEDED    = 100
TRADE_QTY      = 1
MAX_DAILY_LOSS = 500
MAX_DAILY_TRADES = 6
IS_LIVE        = False

API_BASE_URL = "https://api.topstepx.com"

# ---------------------------------------------------------
#  LOGGING
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# =============================================================
#  INDIKÁTORY
# =============================================================

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = df.copy()
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = [(df["open"].iloc[0] + df["close"].iloc[0]) / 2]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + ha["ha_close"].iloc[i - 1]) / 2)
    ha["ha_open"]  = ha_open
    ha["ha_high"]  = ha[["high", "ha_open", "ha_close"]].max(axis=1)
    ha["ha_low"]   = ha[["low",  "ha_open", "ha_close"]].min(axis=1)
    ha["ha_color"] = np.where(ha["ha_close"] >= ha["ha_open"], "green", "red")
    return ha


# =============================================================
#  VSTUPNÍ SIGNÁL
#  → Tato funkce se volá i z backtesteru Trading Hub
# =============================================================

def get_signal(df: pd.DataFrame):
    """
    BUY:  EMA_FAST překříží EMA_SLOW zdola + poslední HA svíčka zelená
    SELL: EMA_FAST překříží EMA_SLOW shora + poslední HA svíčka červená

    Funguje na uzavřených svíčkách – poslední řádek = právě uzavřená svíčka.
    """
    min_bars = max(EMA_FAST, EMA_SLOW) + 3
    if len(df) < min_bars:
        return None

    ema_f = calc_ema(df["close"], EMA_FAST)
    ema_s = calc_ema(df["close"], EMA_SLOW)
    ha    = calc_heikin_ashi(df)

    # Aktuální a předchozí hodnoty
    ef_now  = ema_f.iloc[-1]
    ef_prev = ema_f.iloc[-2]
    es_now  = ema_s.iloc[-1]
    es_prev = ema_s.iloc[-2]

    ha_color = ha.iloc[-1]["ha_color"]

    # BUY: EMA cross zdola + HA zelená
    if ef_prev <= es_prev and ef_now > es_now and ha_color == "green":
        return "BUY"

    # SELL: EMA cross shora + HA červená
    if ef_prev >= es_prev and ef_now < es_now and ha_color == "red":
        return "SELL"

    return None


# =============================================================
#  on_bar() – BACKTEST ROZHRANÍ pro Trading Hub
#  Backtester volá tuto funkci svíčku po svíčce
# =============================================================

def on_bar(df, i, params):
    """
    Rozhraní pro backtester v Trading Hub.
    df     = DataFrame uzavřených svíček (open/high/low/close/volume)
    i      = index aktuální svíčky
    params = dict parametrů z konfigurace bota

    Vrátí signál nebo None.
    """
    # Načti parametry (live hodnoty nebo z backtesteru)
    global EMA_FAST, EMA_SLOW, SL_POINTS, TP_MULT
    ef = int(params.get("EMA_FAST",   EMA_FAST))
    es = int(params.get("EMA_SLOW",   EMA_SLOW))
    sl = float(params.get("SL_POINTS", SL_POINTS))
    tp = float(params.get("TP_MULT",   TP_MULT))

    min_bars = max(ef, es) + 3
    if len(df) < min_bars:
        return None

    ema_f = calc_ema(df["close"], ef)
    ema_s = calc_ema(df["close"], es)
    ha    = calc_heikin_ashi(df)

    ef_now  = float(ema_f.iloc[-1])
    ef_prev = float(ema_f.iloc[-2])
    es_now  = float(ema_s.iloc[-1])
    es_prev = float(ema_s.iloc[-2])
    ha_color = ha.iloc[-1]["ha_color"]

    price = float(df["close"].iloc[-1])

    # BUY signal
    if ef_prev <= es_prev and ef_now > es_now and ha_color == "green":
        sl_price = price - sl
        tp_price = (price + sl * tp) if tp > 0 else None
        return {
            "type":     "LONG",
            "entry":    price,
            "sl":       round(sl_price, 2),
            "tp1":      round(tp_price, 2) if tp_price else None,
            "trailing": tp == 0,   # trailing SL pokud není pevný TP
        }

    # SELL signal
    if ef_prev >= es_prev and ef_now < es_now and ha_color == "red":
        sl_price = price + sl
        tp_price = (price - sl * tp) if tp > 0 else None
        return {
            "type":     "SHORT",
            "entry":    price,
            "sl":       round(sl_price, 2),
            "tp1":      round(tp_price, 2) if tp_price else None,
            "trailing": tp == 0,
        }

    return None


# =============================================================
#  TRAILING STOP
# =============================================================

def get_trail_sl_price(df: pd.DataFrame, position: dict, current_sl: float):
    """
    LONG:  SL → LOW předchozí svíčky (jen nahoru)
    SHORT: SL → HIGH předchozí svíčky (jen dolů)
    """
    if len(df) < 2:
        return None

    pos_type = position.get("type", 1)   # 1=LONG, 2=SHORT
    prev     = df.iloc[-2]

    if pos_type == 1:
        candidate = float(prev["low"])
        if candidate > current_sl:
            return candidate
    else:
        candidate = float(prev["high"])
        if current_sl == 0 or candidate < current_sl:
            return candidate

    return None


# =============================================================
#  TOPSTEPX API KLIENT
# =============================================================

def seconds_until_candle_close(tf_minutes: int) -> float:
    now       = datetime.now(timezone.utc)
    tf_secs   = tf_minutes * 60
    elapsed   = (now.minute * 60 + now.second) % tf_secs
    remaining = tf_secs - elapsed
    return remaining + 2   # 2s buffer


class TopStepXClient:
    def __init__(self, username: str, api_key: str, account_id: int):
        self.username   = username
        self.api_key    = api_key
        self.account_id = account_id
        self.token      = None
        self.token_time = None
        self.session    = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def authenticate(self):
        log.info("Přihlašování do TopStepX API...")
        resp = self.session.post(
            f"{API_BASE_URL}/api/Auth/loginKey",
            json={"userName": self.username, "apiKey": self.api_key},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(
                f"Auth selhala: {data.get('errorMessage')} | "
                f"Zkontroluj USERNAME a API_KEY"
            )
        self.token = data["token"]
        self.token_time = datetime.now(timezone.utc)
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        log.info("Přihlášení úspěšné.")

    def _ensure_token(self):
        if not self.token or \
           (datetime.now(timezone.utc) - self.token_time) > timedelta(hours=23):
            self.authenticate()

    def _post(self, endpoint: str, payload: dict) -> dict:
        self._ensure_token()
        resp = self.session.post(
            f"{API_BASE_URL}/api{endpoint}", json=payload, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def get_candles(self, tf_minutes: int, count: int) -> pd.DataFrame:
        end_time   = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=tf_minutes * count * 2)
        data = self._post("/History/retrieveBars", {
            "contractId": CONTRACT_ID, "live": IS_LIVE,
            "startTime":  start_time.isoformat(),
            "endTime":    end_time.isoformat(),
            "unit": 2, "unitNumber": tf_minutes,
            "limit": count, "includePartialBar": False
        })
        if not data.get("success"):
            raise RuntimeError(f"History chyba: {data.get('errorMessage')}")
        bars = data.get("bars", [])
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df = df.rename(columns={"t":"time","o":"open","h":"high","l":"low","c":"close","v":"volume"})
        df["time"]  = pd.to_datetime(df["time"], utc=True)
        df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
        return df.sort_values("time").reset_index(drop=True)

    def get_position(self):
        data = self._post("/Position/searchOpen", {"accountId": self.account_id})
        if not data.get("success"): return None
        for p in data.get("positions", []):
            if p.get("contractId") == CONTRACT_ID and p.get("size", 0) != 0:
                return p
        return None

    def get_open_orders(self) -> list:
        data = self._post("/Order/searchOpen", {"accountId": self.account_id})
        return data.get("orders", [])

    def place_market_order(self, side_int: int, qty: int) -> dict:
        payload = {
            "accountId": self.account_id, "contractId": CONTRACT_ID,
            "type": 2, "side": side_int, "size": qty,
            "limitPrice": None, "stopPrice": None, "trailPrice": None, "customTag": None,
        }
        log.info(f"MARKET ORDER {'BUY' if side_int==0 else 'SELL'} {qty}x {CONTRACT_ID}")
        data = self._post("/Order/place", payload)
        if not data.get("success"):
            raise RuntimeError(f"Market order chyba: {data.get('errorMessage')}")
        return data

    def place_stop_order(self, side_int: int, qty: int, stop_price: float) -> dict:
        payload = {
            "accountId": self.account_id, "contractId": CONTRACT_ID,
            "type": 4, "side": side_int, "size": qty,
            "limitPrice": None, "stopPrice": round(stop_price, 2),
            "trailPrice": None, "customTag": None,
        }
        log.info(f"STOP ORDER side={side_int} stopPrice={stop_price:.2f}")
        data = self._post("/Order/place", payload)
        if not data.get("success"):
            raise RuntimeError(f"Stop order chyba: {data.get('errorMessage')}")
        return data

    def modify_order(self, order_id: int, new_stop: float) -> dict:
        data = self._post("/Order/modify", {
            "accountId": self.account_id,
            "orderId": order_id,
            "stopPrice": round(new_stop, 2)
        })
        log.info(f"MODIFY SL → {new_stop:.2f}")
        return data

    def cancel_order(self, order_id: int) -> dict:
        return self._post("/Order/cancel", {"accountId": self.account_id, "orderId": order_id})


# =============================================================
#  RISK MANAGEMENT
# =============================================================

class RiskManager:
    def __init__(self, max_daily_loss: float, max_trades: int):
        self.max_daily_loss = max_daily_loss
        self.max_trades     = max_trades
        self.daily_pnl      = 0.0
        self.daily_trades   = 0
        self.last_reset     = datetime.now(timezone.utc).date()

    def _reset_if_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.last_reset:
            log.info("Nový den – reset denních statistik")
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.last_reset   = today

    def can_trade(self) -> bool:
        self._reset_if_new_day()
        if self.daily_pnl <= -self.max_daily_loss:
            log.warning(f"Denní limit ztráty dosažen ({self.daily_pnl:.0f} USD)")
            return False
        if self.daily_trades >= self.max_trades:
            log.warning(f"Max obchodů za den ({self.daily_trades})")
            return False
        return True

    def record_trade(self, pnl: float = 0.0):
        self.daily_pnl    += pnl
        self.daily_trades += 1
        log.info(f"Denní P&L: {self.daily_pnl:.0f} USD | Obchody: {self.daily_trades}")


# =============================================================
#  HLAVNÍ BOT
# =============================================================

class TradingBot:
    def __init__(self):
        self.client      = TopStepXClient(USERNAME, API_KEY, ACCOUNT_ID)
        self.risk        = RiskManager(MAX_DAILY_LOSS, MAX_DAILY_TRADES)
        self.position    = None
        self.sl_order_id = None
        self.current_sl  = 0.0
        self.entry_price = 0.0
        self.entry_time  = None

    def _find_sl_order(self):
        try:
            for o in self.client.get_open_orders():
                if o.get("contractId") == CONTRACT_ID and o.get("type") == 4:
                    return o.get("id")
        except Exception as e:
            log.warning(f"Nelze načíst ordery: {e}")
        return None

    def run_cycle(self):
        try:
            df = self.client.get_candles(TIMEFRAME_MIN, BARS_NEEDED)
            if df.empty or len(df) < max(EMA_FAST, EMA_SLOW) + 5:
                log.warning("Nedostatek dat...")
                return

            self.position = self.client.get_position()

            # ── Správa otevřené pozice ──────────────────────────
            if self.position:
                new_sl = get_trail_sl_price(df, self.position, self.current_sl)
                if new_sl:
                    if not self.sl_order_id:
                        self.sl_order_id = self._find_sl_order()
                    if self.sl_order_id:
                        self.client.modify_order(self.sl_order_id, new_sl)
                        log.info(f"Trailing SL: {self.current_sl:.2f} → {new_sl:.2f}")
                        self.current_sl = new_sl
                return

            # ── Reset po uzavření pozice ────────────────────────
            if self.sl_order_id or self.current_sl != 0.0:
                log.info("Pozice uzavřena – reset stavu")
                self.sl_order_id = None
                self.current_sl  = 0.0
                self.entry_price = 0.0

            # ── Hledej nový vstup ───────────────────────────────
            if not self.risk.can_trade():
                return

            signal = get_signal(df)
            if not signal:
                log.debug("Žádný signál.")
                return

            entry     = float(df["close"].iloc[-1])
            side_int  = 0 if signal == "BUY" else 1
            exit_side = 1 if side_int == 0 else 0

            if signal == "BUY":
                sl_price = entry - SL_POINTS
            else:
                sl_price = entry + SL_POINTS

            log.info(f"SIGNÁL: {signal} | Entry~{entry:.2f} | SL={sl_price:.2f} | Qty={TRADE_QTY}")

            # 1) Market vstup
            mkt = self.client.place_market_order(side_int, TRADE_QTY)
            log.info(f"Market order ID: {mkt.get('orderId')}")
            self.entry_price = entry
            self.entry_time  = datetime.now(timezone.utc)
            time.sleep(1.5)

            # 2) Stop Loss
            sl_res = self.client.place_stop_order(exit_side, TRADE_QTY, sl_price)
            self.sl_order_id = sl_res.get("orderId")
            self.current_sl  = sl_price

            self.risk.record_trade()
            log.info(f"Obchod otevřen | {signal} {TRADE_QTY}x {CONTRACT_ID} | SL={sl_price:.2f}")

        except requests.HTTPError as e:
            log.error(f"HTTP chyba: {e.response.status_code} – {e.response.text[:300]}")
        except Exception as e:
            log.exception(f"Neočekávaná chyba: {e}")

    def run(self):
        log.info("=" * 60)
        log.info("  MNQ Demo Bot – Trading Hub")
        log.info(f"  Strategie : EMA{EMA_FAST}/{EMA_SLOW} cross + HA filtr | {TIMEFRAME_MIN}m")
        log.info(f"  Kontrakt  : {CONTRACT_ID}")
        log.info(f"  SL        : {SL_POINTS} bodů | TP: {'trailing' if TP_MULT==0 else f'{TP_MULT}x SL'}")
        log.info(f"  Qty       : {TRADE_QTY} | Max ztráta: ${MAX_DAILY_LOSS}")
        log.info(f"  Režim     : {'LIVE' if IS_LIVE else 'DEMO'}")
        log.info("=" * 60)

        self.client.authenticate()

        while True:
            wait = seconds_until_candle_close(TIMEFRAME_MIN)
            log.info(f"Čekám na uzavření svíčky za ~{wait:.0f}s...")
            time.sleep(wait)
            self.run_cycle()


# =============================================================
#  SPUŠTĚNÍ
# =============================================================
if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
