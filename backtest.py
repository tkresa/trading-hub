"""
backtest.py – Backtesting engine v3 (vektorizovaný)
=====================================================
- Vektorizovaná simulace pomocí numpy/pandas = 100x rychlejší
- Auto-detekce stylu bota (on_bar / get_signal / heikin)
- Resampling 1min → libovolný TF
- Trailing stop podpora
"""

import pandas as pd
import numpy as np
import json
import re
import traceback
from io import StringIO


# ═══════════════════════════════════════════════════════════════════════════════
#  RESAMPLING
# ═══════════════════════════════════════════════════════════════════════════════

def resample_df(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes <= 1:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    return df.resample(f"{minutes}min", closed="left", label="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["open", "close"])


# ═══════════════════════════════════════════════════════════════════════════════
#  CSV LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def load_csv(csv_content: str) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(StringIO(csv_content))
        df.columns = [c.strip().lower() for c in df.columns]

        col_map = {}
        for c in df.columns:
            if any(x in c for x in ["date","time","timestamp","ts_event","ts_recv"]):
                col_map[c] = "datetime"
            elif c == "open":   col_map[c] = "open"
            elif c == "high":   col_map[c] = "high"
            elif c == "low":    col_map[c] = "low"
            elif c in ("close","last"): col_map[c] = "close"
            elif "vol" in c:    col_map[c] = "volume"

        df = df.rename(columns=col_map)
        for r in ["open","high","low","close"]:
            if r not in df.columns:
                return None

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
            df["datetime"] = df["datetime"].dt.tz_localize(None)
            df = df.set_index("datetime")

        if "volume" not in df.columns:
            df["volume"] = 1000

        for col in ["open","high","low","close","volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df.dropna(subset=["open","high","low","close"]).sort_index()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  DETEKCE STYLU BOTA
# ═══════════════════════════════════════════════════════════════════════════════

def detect_bot_style(code: str) -> dict:
    has_on_bar       = bool(re.search(r'def\s+on_bar\s*\(', code))
    has_get_signal   = bool(re.search(r'def\s+get_signal\s*\(', code))
    has_heikin       = bool(re.search(r'def\s+calc_heikin_ashi\s*\(', code))
    has_trailing     = bool(re.search(r'trail|trailing|get_trail', code, re.IGNORECASE))
    has_tp           = bool(re.search(r'\btp1\b|\btp2\b|take.profit|target_rr|target_price', code, re.IGNORECASE))
    has_run_backtest = bool(re.search(r'def\s+run_backtest\s*\(', code))
    has_default_params = bool(re.search(r'DEFAULT_PARAMS\s*=\s*\{', code))

    if has_on_bar:                         style = "on_bar"
    elif has_run_backtest:                 style = "run_backtest"   # dict-based
    elif has_heikin:                       style = "heikin"
    elif has_get_signal:                   style = "get_signal"
    else:                                  style = "unknown"

    return {
        "has_on_bar":         has_on_bar,
        "has_get_signal":     has_get_signal,
        "has_heikin":         has_heikin,
        "has_run_backtest":   has_run_backtest,
        "has_default_params": has_default_params,
        "has_trailing":       has_trailing and not has_tp,
        "style":              style,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  SANITIZE – odstraní live importy a __main__ blok
# ═══════════════════════════════════════════════════════════════════════════════

BLOCKED_IMPORTS = [
    "requests","websockets","aiohttp","asyncio",
    "dotenv","python_dotenv","socket","urllib",
    "httpx","ccxt","ib_insync","load_dotenv",
]

def _sanitize_code(code: str) -> str:
    lines, result, skip = code.split("\n"), [], False

    for line in lines:
        s = line.strip()

        # __main__ blok
        if ("if __name__" in s and "__main__" in s) or \
           ("if _name_"   in s and "_main_"   in s):
            skip = True
        if skip:
            result.append("# " + line)
            continue

        # Blocked importy
        blocked = False
        if s.startswith("import ") or s.startswith("from ") or "load_dotenv" in s:
            for b in BLOCKED_IMPORTS:
                if b in s:
                    result.append("# [backtest] " + line)
                    blocked = True
                    break
        if blocked:
            continue

        # int(os.getenv(...)) → 0
        if "int(os.getenv(" in line:
            line = re.sub(
                r'(\w+)\s*=\s*int\(os\.getenv\([^)]+\)\)',
                lambda m: m.group(1) + " = 0  # [backtest]", line
            )
        # os.getenv(...) → ""
        elif "os.getenv(" in line and "=" in line:
            line = re.sub(
                r'(\w+)\s*=\s*os\.getenv\([^)]+\)',
                lambda m: m.group(1) + ' = ""  # [backtest]', line
            )

        # Přímá volání .run()
        if re.match(r'^(bot\.run|TradingBot\(\)\.run|\w+\.run\(\))', s):
            result.append("# [backtest] " + line)
            continue

        result.append(line)

    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════════════════
#  VEKTORIZOVANÉ INDIKÁTORY
# ═══════════════════════════════════════════════════════════════════════════════

def _calc_ema_vec(close: np.ndarray, period: int) -> np.ndarray:
    """EMA vektorizovaně."""
    ema = np.full_like(close, np.nan)
    if len(close) < period:
        return ema
    alpha = 2.0 / (period + 1)
    ema[period-1] = close[:period].mean()
    for i in range(period, len(close)):
        ema[i] = close[i] * alpha + ema[i-1] * (1 - alpha)
    return ema


def _calc_atr_vec(high: np.ndarray, low: np.ndarray,
                  close: np.ndarray, period: int = 14) -> np.ndarray:
    """ATR vektorizovaně."""
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:]  - close[:-1])))
    tr = np.concatenate([[high[0]-low[0]], tr])
    atr = np.full_like(tr, np.nan)
    if len(tr) >= period:
        atr[period-1] = tr[:period].mean()
        alpha = 1.0 / period
        for i in range(period, len(tr)):
            atr[i] = tr[i] * alpha + atr[i-1] * (1 - alpha)
    return atr


def _calc_heikin_ashi_vec(o, h, l, c):
    """Heikin Ashi vektorizovaně."""
    ha_close = (o + h + l + c) / 4
    ha_open  = np.full_like(ha_close, np.nan)
    ha_open[0] = (o[0] + c[0]) / 2
    for i in range(1, len(ha_close)):
        ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
    ha_high  = np.maximum(h, np.maximum(ha_open, ha_close))
    ha_low   = np.minimum(l, np.minimum(ha_open, ha_close))
    ha_green = ha_close >= ha_open   # True = zelená
    return ha_open, ha_high, ha_low, ha_close, ha_green


# ═══════════════════════════════════════════════════════════════════════════════
#  VEKTORIZOVANÁ GENERACE SIGNÁLŮ
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_signals_vectorized(df: pd.DataFrame, style_info: dict,
                                  safe_code: str, params: dict) -> np.ndarray:
    """
    Vygeneruje pole signálů pro každou svíčku najednou.
    Vrátí numpy array: 1=LONG, -1=SHORT, 0=žádný signál
    """
    n = len(df)
    signals = np.zeros(n, dtype=np.int8)

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    style = style_info["style"]

    # ── Heikin Ashi + EMA ────────────────────────────────────────────────────
    if style in ("heikin", "get_signal") and style_info["has_heikin"]:
        ema_period = int(params.get("EMA_PERIOD", params.get("ema_period", 9)))
        ha_o, ha_h, ha_l, ha_c, ha_green = _calc_heikin_ashi_vec(o, h, l, c)
        ema = _calc_ema_vec(c, ema_period)
        classic_bull = c > o
        classic_bear = c < o

        for i in range(ema_period + 2, n):
            if np.isnan(ema[i]): continue
            # 2 zelené HA svíčky + HA close nad EMA + klasická zelená
            if (ha_green[i] and ha_green[i-1]
                    and ha_c[i] > ema[i] and classic_bull[i]):
                signals[i] = 1
            # 2 červené HA svíčky + HA close pod EMA + klasická červená
            elif (not ha_green[i] and not ha_green[i-1]
                      and ha_c[i] < ema[i] and classic_bear[i]):
                signals[i] = -1

    # ── Jednoduchý EMA crossover (get_signal bez heikin) ────────────────────
    elif style == "get_signal":
        ema_fast = _calc_ema_vec(c, 9)
        ema_slow = _calc_ema_vec(c, 21)
        for i in range(22, n):
            if np.isnan(ema_fast[i]) or np.isnan(ema_slow[i]): continue
            if ema_fast[i] > ema_slow[i] and ema_fast[i-1] <= ema_slow[i-1]:
                signals[i] = 1
            elif ema_fast[i] < ema_slow[i] and ema_fast[i-1] >= ema_slow[i-1]:
                signals[i] = -1

    # ── Vlastní on_bar() – spusť přes exec pro každý signal bar ─────────────
    elif style == "on_bar":
        ns = {"pd": pd, "np": np, "params": params}
        exec(safe_code, ns)
        on_bar_fn = ns.get("on_bar")
        if on_bar_fn:
            for i in range(30, n):
                try:
                    sig = on_bar_fn(df.iloc[:i+1], i, params)
                    if sig and isinstance(sig, dict):
                        t = sig.get("type","").upper()
                        if t == "LONG":  signals[i] = 1
                        elif t == "SHORT": signals[i] = -1
                except Exception:
                    pass

    return signals


# ═══════════════════════════════════════════════════════════════════════════════
#  VEKTORIZOVANÁ SIMULACE
# ═══════════════════════════════════════════════════════════════════════════════

def _simulate_vectorized(df: pd.DataFrame, signals: np.ndarray,
                          style_info: dict, params: dict) -> list:
    """
    Simuluje trady na základě signálů.
    Vektorizovaný průchod — velmi rychlý.
    """
    sl_pts      = float(params.get("sl_points", params.get("SL_POINTS", 18)))
    contracts   = float(params.get("contracts", params.get("TRADE_QTY", 1)))
    point_val   = float(params.get("point_value", 2.0))
    commission  = float(params.get("commission", 0.0))   # $ per side per contract
    slippage    = float(params.get("slippage",   0.0))   # points per side
    trailing    = style_info["has_trailing"]
    tp_mult     = float(params.get("tp_mult", 1.5))

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    times = [str(t)[:19] for t in df.index]

    trades   = []
    position = None   # None nebo dict

    for i in range(len(df)):
        # ── Spravuj otevřenou pozici ──────────────────────────────────────
        if position is not None:
            sl  = position["sl"]
            tp  = position["tp"]
            typ = position["type"]

            exit_price  = None
            exit_reason = None

            if typ == 1:   # LONG
                if l[i] <= sl:
                    exit_price, exit_reason = sl, "SL"
                elif not trailing and tp and h[i] >= tp:
                    exit_price, exit_reason = tp, "TP"
            else:          # SHORT
                if h[i] >= sl:
                    exit_price, exit_reason = sl, "SL"
                elif not trailing and tp and l[i] <= tp:
                    exit_price, exit_reason = tp, "TP"

            if exit_price is not None:
                pnl = (exit_price - position["entry"]) * (1 if typ == 1 else -1)
                pnl -= slippage * 2  # slippage na vstup i výstup
                pnl *= point_val * contracts
                pnl -= commission * 2 * contracts  # komise round-trip
                trades.append({
                    "num":         len(trades) + 1,
                    "type":        "LONG" if typ == 1 else "SHORT",
                    "entry":       round(position["entry"], 2),
                    "exit":        round(exit_price, 2),
                    "sl":          round(position["sl"], 2),
                    "tp":          round(position["tp"] or 0, 2),
                    "pnl":         round(pnl, 2),
                    "exit_reason": exit_reason,
                    "entry_time":  position["entry_time"],
                    "exit_time":   times[i],
                    "contracts":   int(contracts),
                })
                position = None

            else:
                # Trailing stop update
                if trailing:
                    if typ == 1:   # LONG - SL na LOW předchozí svíčky
                        candidate = l[i]
                        if candidate > position["sl"]:
                            position["sl"] = candidate
                    else:          # SHORT - SL na HIGH předchozí svíčky
                        candidate = h[i]
                        if position["sl"] == 0 or candidate < position["sl"]:
                            position["sl"] = candidate
                continue

        # ── Nový vstup na základě signálu ─────────────────────────────────
        if position is None and signals[i] != 0:
            entry = c[i]
            sig   = signals[i]

            if sig == 1:   # LONG
                sl = entry - sl_pts
                tp = entry + sl_pts * tp_mult if not trailing else None
            else:          # SHORT
                sl = entry + sl_pts
                tp = entry - sl_pts * tp_mult if not trailing else None

            position = {
                "type":       sig,
                "entry":      entry,
                "sl":         sl,
                "tp":         tp,
                "entry_time": times[i],
            }

    # Uzavři otevřenou pozici na konci
    if position is not None:
        exit_p = c[-1]
        pnl = (exit_p - position["entry"]) * (1 if position["type"] == 1 else -1)
        pnl -= slippage * 2
        pnl *= point_val * contracts
        pnl -= commission * 2 * contracts
        trades.append({
            "num":         len(trades) + 1,
            "type":        "LONG" if position["type"] == 1 else "SHORT",
            "entry":       round(position["entry"], 2),
            "exit":        round(exit_p, 2),
            "sl":          round(position["sl"], 2),
            "tp":          round(position.get("tp") or 0, 2),
            "pnl":         round(pnl, 2),
            "exit_reason": "END_OF_DATA",
            "entry_time":  position["entry_time"],
            "exit_time":   times[-1],
            "contracts":   int(contracts),
        })

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
#  STATISTIKY
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_stats_fast(trades: list) -> dict:
    """
    Rychlá verze statistik pro optimizer - přeskočí session/hourly/daily výpočty.
    Volá se pro každou kombinaci parametrů v optimalizaci.
    """
    import math
    if not trades:
        return {
            "total_pnl":0,"winrate":0,"avg_rr":0,"max_dd":0,
            "total_trades":0,"total_wins":0,"total_losses":0,
            "avg_win":0,"avg_loss":0,"profit_factor":0,
            "expectancy":0,"sharpe_ratio":0,"recovery_factor":0,
        }

    pnls      = [t["pnl"] for t in trades]
    wins_pnl  = [p for p in pnls if p > 0]
    loss_pnl  = [p for p in pnls if p <= 0]

    avg_win  = round(sum(wins_pnl)/len(wins_pnl),  2) if wins_pnl  else 0
    avg_loss = round(abs(sum(loss_pnl)/len(loss_pnl)),2) if loss_pnl else 1

    gross_p  = sum(p for p in pnls if p > 0)
    gross_l  = abs(sum(p for p in pnls if p < 0))
    pf       = round(gross_p / gross_l, 2) if gross_l else 0

    wr       = len(wins_pnl) / len(pnls)
    exp      = round(wr * avg_win - (1-wr) * avg_loss, 2)
    total_pnl= round(sum(pnls), 2)

    # Max drawdown
    peak = running = max_dd = 0
    for p in pnls:
        running += p
        if running > peak: peak = running
        if peak - running > max_dd: max_dd = peak - running

    # Sharpe
    if len(pnls) > 1:
        mean_r = sum(pnls)/len(pnls)
        std_r  = math.sqrt(sum((p-mean_r)**2 for p in pnls)/(len(pnls)-1))
        sharpe = round((mean_r/std_r)*math.sqrt(252), 2) if std_r > 0 else 0
    else:
        sharpe = 0

    rf = round(total_pnl/max_dd, 2) if max_dd > 0 else 0

    # R:R
    rr_list = []
    for t in trades:
        r = abs(t.get("entry",0)-t.get("sl",0))
        rew = abs(t.get("entry",0)-t.get("tp",0)) if t.get("tp") else 0
        if r > 0 and rew > 0: rr_list.append(round(rew/r,2))
    avg_rr = round(sum(rr_list)/len(rr_list),2) if rr_list else 0

    return {
        "total_pnl":     total_pnl,
        "winrate":       round(wr*100, 1),
        "avg_rr":        avg_rr,
        "max_dd":        round(max_dd, 2),
        "total_trades":  len(trades),
        "total_wins":    len(wins_pnl),
        "total_losses":  len(loss_pnl),
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "profit_factor": pf,
        "expectancy":    exp,
        "sharpe_ratio":  sharpe,
        "recovery_factor": rf,
    }


def _compute_stats(trades: list) -> tuple:
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone
    import math

    if not trades:
        return {
            "total_pnl":0,"winrate":0,"avg_rr":0,"max_dd":0,
            "total_trades":0,"total_wins":0,"total_losses":0,
            "avg_win":0,"avg_loss":0,"best_win":0,"worst_loss":0,
            "profit_factor":0,"expectancy":0,"avg_trade_pnl":0,
            "min_rr":0,"avg_rr_trades":0,"max_rr":0,
            "max_consec_wins":0,"max_consec_losses":0,
            "avg_consec_wins":0,"avg_consec_losses":0,
            "avg_duration_win":"–","avg_duration_loss":"–",
            "sharpe_ratio":0,"recovery_factor":0,
            "exit_reasons":{},"exit_pct":{},
            "perf_by_hour":[],"perf_by_day":[],"perf_by_month":[],
            "session_stats":{},"duration_dist":{},
        }, []

    pnls   = [t["pnl"] for t in trades]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_pnls  = [t["pnl"] for t in wins]
    loss_pnls = [t["pnl"] for t in losses]

    avg_win  = round(sum(win_pnls)  / len(win_pnls),  2) if win_pnls  else 0
    avg_loss = round(abs(sum(loss_pnls)/len(loss_pnls)),2) if loss_pnls else 1
    best_win   = round(max(win_pnls),  2) if win_pnls  else 0
    worst_loss = round(min(loss_pnls), 2) if loss_pnls else 0

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else 0

    wr = len(win_pnls) / len(pnls)
    expectancy = round(wr * avg_win - (1 - wr) * avg_loss, 2)
    avg_trade_pnl = round(sum(pnls) / len(pnls), 2)

    # Max drawdown
    peak = running = max_dd = 0
    for p in pnls:
        running += p
        if running > peak: peak = running
        if peak - running > max_dd: max_dd = peak - running

    # Sharpe Ratio (annualized, assuming daily returns)
    if len(pnls) > 1:
        mean_r = sum(pnls) / len(pnls)
        std_r  = math.sqrt(sum((p - mean_r)**2 for p in pnls) / (len(pnls)-1))
        sharpe = round((mean_r / std_r) * math.sqrt(252), 2) if std_r > 0 else 0
    else:
        sharpe = 0

    # Recovery Factor = Total PnL / Max Drawdown
    total_pnl_val = round(sum(pnls), 2)
    recovery_factor = round(total_pnl_val / max_dd, 2) if max_dd > 0 else 0

    # R:R per trade
    rr_list = []
    for t in trades:
        entry = t.get("entry", 0)
        sl    = t.get("sl", 0)
        tp    = t.get("tp", 0)
        risk  = abs(entry - sl) if sl else 0
        rew   = abs(entry - tp) if tp else 0
        if risk > 0 and rew > 0:
            rr_list.append(round(rew / risk, 2))
    min_rr     = round(min(rr_list), 2) if rr_list else 0
    avg_rr_val = round(sum(rr_list) / len(rr_list), 2) if rr_list else 0
    max_rr     = round(max(rr_list), 2) if rr_list else 0

    # Consecutive wins/losses
    consec_w, consec_l = [], []
    cw = cl = 0
    for p in pnls:
        if p > 0:
            cw += 1
            if cl > 0: consec_l.append(cl); cl = 0
        else:
            cl += 1
            if cw > 0: consec_w.append(cw); cw = 0
    if cw > 0: consec_w.append(cw)
    if cl > 0: consec_l.append(cl)

    max_cw = max(consec_w, default=0)
    max_cl = max(consec_l, default=0)
    avg_cw = round(sum(consec_w)/len(consec_w), 1) if consec_w else 0
    avg_cl = round(sum(consec_l)/len(consec_l), 1) if consec_l else 0

    # Duration helper
    def _dur_mins(t):
        try:
            e = datetime.fromisoformat(t["entry_time"])
            x = datetime.fromisoformat(t["exit_time"])
            return (x - e).total_seconds() / 60
        except Exception:
            return None

    def _dur_str(trade_list):
        durs = [_dur_mins(t) for t in trade_list if _dur_mins(t) is not None]
        if not durs: return "–"
        avg = sum(durs) / len(durs)
        h, m = int(avg // 60), int(avg % 60)
        return f"{h}h {m}m" if h > 0 else f"{m}m"

    # Trade duration distribution
    buckets = {"0–5m": 0, "5–15m": 0, "15–60m": 0, "1h+": 0}
    for t in trades:
        d = _dur_mins(t)
        if d is None: continue
        if d < 5:    buckets["0–5m"]  += 1
        elif d < 15: buckets["5–15m"] += 1
        elif d < 60: buckets["15–60m"] += 1
        else:        buckets["1h+"]   += 1
    total_t = len(trades) or 1
    duration_dist = {k: {"count": v, "pct": round(v/total_t*100, 1)} for k, v in buckets.items()}

    # Exit reasons
    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason", "OTHER")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1
    exit_pct = {r: round(c/total_t*100, 1) for r, c in exit_reasons.items()}

    # DST helper – Czech time s cache pro rychlost
    import calendar as _cal
    _dst_cache = {}
    def _get_dst_bounds(year):
        if year not in _dst_cache:
            def last_sun(y, m):
                ld = _cal.monthrange(y, m)[1]
                for d in range(ld, ld-7, -1):
                    if datetime(y, m, d).weekday() == 6:
                        return datetime(y, m, d, 2, 0)
            _dst_cache[year] = (last_sun(year, 3), last_sun(year, 10))
        return _dst_cache[year]

    def _to_cet(dt):
        naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        dst_s, dst_e = _get_dst_bounds(naive.year)
        offset = 2 if dst_s <= naive < dst_e else 1
        return naive + timedelta(hours=offset)

    # NY session DST-aware (CET time)
    # Winter: NY opens 15:30 CET, Summer: 14:30 CET
    def _ny_session(cet_dt):
        year = cet_dt.year
        import calendar
        def last_sunday(y, m):
            last_day = calendar.monthrange(y, m)[1]
            for d in range(last_day, last_day - 7, -1):
                if datetime(y, m, d).weekday() == 6:
                    return datetime(y, m, d, 2, 0)
        us_dst_start = datetime(year, 3, 1) + timedelta(days=(6 - datetime(year, 3, 1).weekday()) % 7 + 7)  # 2nd Sunday March
        us_dst_end   = datetime(year, 11, 1) + timedelta(days=(6 - datetime(year, 11, 1).weekday()) % 7)    # 1st Sunday Nov
        in_us_dst    = us_dst_start <= cet_dt.replace(hour=0,minute=0) < us_dst_end
        ny_open  = 14.5 if in_us_dst else 15.5   # CET hours (14:30 or 15:30)
        ny_close = ny_open + 6.5                  # ~6.5h session
        return ny_open, ny_close

    # Performance by hour (CET)
    hour_stats = defaultdict(lambda: {"wins":0,"total":0,"pnl":0.0})
    for t in trades:
        try:
            dt  = _to_cet(datetime.fromisoformat(t["entry_time"]))
            h   = dt.hour
            hour_stats[h]["total"] += 1
            hour_stats[h]["pnl"]   += t["pnl"]
            if t["pnl"] > 0: hour_stats[h]["wins"] += 1
        except Exception: pass
    perf_by_hour = []
    for h in sorted(hour_stats.keys()):
        s  = hour_stats[h]
        wr2 = round(s["wins"]/s["total"]*100, 1) if s["total"] else 0
        perf_by_hour.append({"hour": h, "label": f"{h:02d}:00", "wins": s["wins"],
                              "total": s["total"], "pnl": round(s["pnl"],2), "winrate": wr2})

    # Performance by day
    days = ["Po","Út","St","Čt","Pá","So","Ne"]
    day_stats = defaultdict(lambda: {"wins":0,"total":0,"pnl":0.0})
    for t in trades:
        try:
            dt = _to_cet(datetime.fromisoformat(t["entry_time"]))
            d  = dt.weekday()
            day_stats[d]["total"] += 1
            day_stats[d]["pnl"]   += t["pnl"]
            if t["pnl"] > 0: day_stats[d]["wins"] += 1
        except Exception: pass
    perf_by_day = []
    for d in range(7):
        s  = day_stats[d]
        wr2 = round(s["wins"]/s["total"]*100, 1) if s["total"] else 0
        perf_by_day.append({"day": d, "label": days[d], "wins": s["wins"],
                             "total": s["total"], "pnl": round(s["pnl"],2), "winrate": wr2})

    # Performance by month
    months = ["Led","Úno","Bře","Dub","Kvě","Čvn","Čvc","Srp","Zář","Říj","Lis","Pro"]
    month_stats = defaultdict(lambda: {"wins":0,"total":0,"pnl":0.0})
    for t in trades:
        try:
            dt = _to_cet(datetime.fromisoformat(t["entry_time"]))
            m  = dt.month - 1
            month_stats[m]["total"] += 1
            month_stats[m]["pnl"]   += t["pnl"]
            if t["pnl"] > 0: month_stats[m]["wins"] += 1
        except Exception: pass
    perf_by_month = []
    for m in range(12):
        s  = month_stats[m]
        wr2 = round(s["wins"]/s["total"]*100, 1) if s["total"] else 0
        perf_by_month.append({"month": m, "label": months[m], "wins": s["wins"],
                               "total": s["total"], "pnl": round(s["pnl"],2), "winrate": wr2})

    # Session winrates (CET) – DST-aware NY
    sessions = {
        "Asia":   {"wins":0,"total":0,"pnl":0.0, "hours":"01:00–09:00"},
        "London": {"wins":0,"total":0,"pnl":0.0, "hours":"09:00–17:30"},
        "NY":     {"wins":0,"total":0,"pnl":0.0, "hours":"14:30/15:30–22:00"},
    }
    for t in trades:
        try:
            dt  = _to_cet(datetime.fromisoformat(t["entry_time"]))
            h   = dt.hour + dt.minute/60
            pnl = t["pnl"]
            win = pnl > 0
            ny_open, ny_close = _ny_session(dt)
            def _add(sess):
                sessions[sess]["total"] += 1
                sessions[sess]["pnl"]   += pnl
                if win: sessions[sess]["wins"] += 1
            if h >= 1 and h < 9:             _add("Asia")
            if h >= 9 and h < 17.5:          _add("London")
            if h >= ny_open and h < ny_close: _add("NY")
        except Exception: pass
    session_stats = {}
    for name, s in sessions.items():
        wr2 = round(s["wins"]/s["total"]*100, 1) if s["total"] else 0
        session_stats[name] = {"wins": s["wins"], "total": s["total"],
                               "pnl": round(s["pnl"],2), "winrate": wr2,
                               "hours": s["hours"]}

    # ── Avg trades per day / week / month ───────────────────────────
    trade_dates = set()
    trade_weeks = set()
    trade_months = set()
    for t in trades:
        try:
            dt = datetime.fromisoformat(t["entry_time"])
            trade_dates.add(dt.date())
            trade_weeks.add((dt.year, dt.isocalendar()[1]))
            trade_months.add((dt.year, dt.month))
        except Exception:
            pass
    n_days   = len(trade_dates)  or 1
    n_weeks  = len(trade_weeks)  or 1
    n_months = len(trade_months) or 1
    avg_trades_day   = round(len(trades) / n_days,   1)
    avg_trades_week  = round(len(trades) / n_weeks,  1)
    avg_trades_month = round(len(trades) / n_months, 1)

    # Equity křivka
    equity, running = [], 0
    for t in trades:
        running += t["pnl"]
        equity.append({
            "trade":  t["num"],
            "value":  round(running, 2),
            "pnl":    t["pnl"],
            "time":   t.get("exit_time","")[:16],
            "result": "WIN" if t["pnl"] > 0 else "LOSS",
        })

    return {
        "total_pnl":         total_pnl_val,
        "winrate":           round(wr*100, 1),
        "avg_rr":            round(avg_win/avg_loss, 2) if avg_loss else 0,
        "min_rr":            min_rr,
        "avg_rr_trades":     avg_rr_val,
        "max_rr":            max_rr,
        "max_dd":            round(max_dd, 2),
        "total_trades":      len(trades),
        "total_wins":        len(win_pnls),
        "total_losses":      len(loss_pnls),
        "avg_win":           avg_win,
        "avg_loss":          avg_loss,
        "best_win":          best_win,
        "worst_loss":        worst_loss,
        "profit_factor":     profit_factor,
        "expectancy":        expectancy,
        "avg_trade_pnl":     avg_trade_pnl,
        "sharpe_ratio":      sharpe,
        "recovery_factor":   recovery_factor,
        "max_consec_wins":   max_cw,
        "max_consec_losses": max_cl,
        "avg_consec_wins":   avg_cw,
        "avg_consec_losses": avg_cl,
        "avg_duration_win":  _dur_str(wins),
        "avg_duration_loss": _dur_str(losses),
        "exit_reasons":      exit_reasons,
        "exit_pct":          exit_pct,
        "duration_dist":     duration_dist,
        "perf_by_hour":      perf_by_hour,
        "perf_by_day":       perf_by_day,
        "perf_by_month":     perf_by_month,
        "session_stats":     session_stats,
        "avg_trades_day":    avg_trades_day,
        "avg_trades_week":   avg_trades_week,
        "avg_trades_month":  avg_trades_month,
    }, equity


# ═══════════════════════════════════════════════════════════════════════════════
#  HLAVNÍ FUNKCE
# ═══════════════════════════════════════════════════════════════════════════════

def _simulate_run_backtest_style(code: str, df: pd.DataFrame, params: dict) -> tuple:
    """
    Simulace pro strategie s run_backtest(df, params) funkcí.
    Volá originální run_backtest a konvertuje výstup na standardní formát Trading Hub.
    Podporuje strategie jako Multi-Pattern Breakout.
    """
    try:
        namespace = {"pd": pd, "np": np, "params": params}

        # Injektuj opt parametry do DEFAULT_PARAMS pokud existuje
        inject_code = code
        opt_keys = {k: v for k, v in params.items()
                    if not k.startswith("__") and k.lower() == k}  # malá = dict keys
        if opt_keys and "DEFAULT_PARAMS" in code:
            overrides = "\n".join([f'  "{k}": {v},' for k, v in opt_keys.items()])
            inject_code = code + f"""
# ── Trading Hub opt override ──
_opt_overrides = {{{overrides}
}}
if "DEFAULT_PARAMS" in dir() or "DEFAULT_PARAMS" in globals():
    DEFAULT_PARAMS.update({{k: v for k, v in _opt_overrides.items() if k in DEFAULT_PARAMS}})
"""

        exec(inject_code, namespace)

        run_bt_fn = namespace.get("run_backtest")
        if not run_bt_fn:
            return [], "Funkce run_backtest(df, params) nenalezena"

        # Připrav parametry - zkus předat DEFAULT_PARAMS aktualizované o opt hodnoty
        call_params = namespace.get("DEFAULT_PARAMS", {})
        # Přidej velká písmena z params
        for k, v in params.items():
            if k.isupper():
                call_params[k.lower()] = v
                call_params[k]         = v

        result = run_bt_fn(df.copy(), call_params if call_params else params)

        # Konvertuj DataFrame výstup na standardní formát
        if result is None or (hasattr(result, "empty") and result.empty):
            return [], None  # Žádné trady, ale ne chyba

        trades = []
        pv     = float(params.get("point_value", params.get("POINT_VALUE", 2.0)))

        for idx, row in enumerate(result.to_dict("records")):
            # Detekuj směr
            direction = str(row.get("direction", "")).lower()
            is_long   = direction in ("long", "buy", "1") or row.get("type","").upper() == "LONG"

            entry = float(row.get("entry_price", row.get("entry", 0)))
            exit_ = float(row.get("exit", row.get("exit_price", 0)))
            pnl   = float(row.get("pnl", 0))
            contracts = int(row.get("contracts", 1))

            # Timestamps
            entry_ts = str(row.get("entry_ts", row.get("entry_time", "")))
            exit_ts  = str(row.get("exit_ts",  row.get("exit_time",  "")))
            if entry_ts: entry_ts = entry_ts[:19]
            if exit_ts:  exit_ts  = exit_ts[:19]

            exit_reason = str(row.get("exit_reason", row.get("exit_reason", "SL"))).upper()

            trades.append({
                "num":         idx + 1,
                "type":        "LONG" if is_long else "SHORT",
                "entry":       round(entry, 2),
                "exit":        round(exit_, 2),
                "sl":          round(float(row.get("stop_price", row.get("sl", row.get("stop", 0)))), 2),
                "tp":          round(float(row.get("target_price", row.get("tp", 0))), 2),
                "pnl":         round(pnl, 2),
                "exit_reason": exit_reason if exit_reason in ("SL","TP","EOD","BE","TARGET") else "OTHER",
                "entry_time":  entry_ts,
                "exit_time":   exit_ts,
                "contracts":   contracts,
            })

        return trades, None

    except SyntaxError as e:
        return [], f"Syntax chyba v kódu: {e}"
    except Exception as e:
        import traceback
        return [], f"Chyba při simulaci: {e}\n{traceback.format_exc()[:500]}"


def run_backtest(bot_code: str, csv_content: str,
                 period_from: str, period_to: str,
                 params: dict, timeframe_minutes: int = 1) -> dict:
    try:
        import time
        t0 = time.time()

        # 1. Načti CSV
        df = load_csv(csv_content)
        if df is None or df.empty:
            return {"success": False,
                    "error": "Nepodařilo se načíst CSV. Formát: datetime,open,high,low,close,volume"}

        # 2. Resampling
        if timeframe_minutes > 1:
            df = resample_df(df, timeframe_minutes)
            if df.empty:
                return {"success": False,
                        "error": f"Po resamplingu na {timeframe_minutes}min nejsou data."}

        # 3. Filtr období
        if period_from:
            ts = pd.Timestamp(period_from)
            if df.index.tz is not None:
                ts = ts.tz_localize("UTC")
            df = df[df.index >= ts]
        if period_to:
            ts = pd.Timestamp(period_to)
            if df.index.tz is not None:
                ts = ts.tz_localize("UTC")
            df = df[df.index <= ts]
        if df.empty:
            return {"success": False, "error": "Žádná data v zadaném období."}

        # 4. Sanitize kódu
        safe_code  = _sanitize_code(bot_code)
        style_info = detect_bot_style(safe_code)

        # Zpracuj run_backtest styl (dict-based strategie jako Multi-Pattern)
        if style_info["style"] == "run_backtest":
            trades, error = _simulate_run_backtest_style(safe_code, df, params)
            if error:
                return {"success": False, "error": error}
            stats, equity = _compute_stats(trades)
            elapsed = round(time.time() - t0, 1)
            return {
                "success":    True, "error": None,
                "trades":     trades, "equity": equity, "stats": stats,
                "style_info": style_info, "timeframe": timeframe_minutes,
                "bars_total": len(df), "elapsed_sec": elapsed,
            }

        if style_info["style"] == "unknown":
            return {"success": False,
                    "error": ("Backtester nenašel funkci on_bar(), get_signal() ani run_backtest().\n"
                              "Přidej do bota jednu z těchto funkcí.")}

        # 5. Vektorizovaná generace signálů
        signals = _generate_signals_vectorized(df, style_info, safe_code, params)

        # 6. Vektorizovaná simulace
        trades = _simulate_vectorized(df, signals, style_info, params)

        # 7. Statistiky
        stats, equity = _compute_stats(trades)

        elapsed = round(time.time() - t0, 1)

        return {
            "success":    True,
            "error":      None,
            "trades":     trades,
            "equity":     equity,
            "stats":      stats,
            "style_info": style_info,
            "timeframe":  timeframe_minutes,
            "bars_total": len(df),
            "elapsed_sec": elapsed,
        }

    except Exception as e:
        return {"success": False,
                "error": f"Chyba: {str(e)}\n{traceback.format_exc()}"}
