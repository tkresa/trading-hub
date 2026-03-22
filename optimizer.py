"""
optimizer.py – Walk-Forward Optimization Engine
=================================================
1. Detekuje optimalizovatelné parametry v kódu bota
2. Spustí grid search na in-sample období
3. Nejlepší kombinaci ověří na out-of-sample období
4. Vrátí seřazené výsledky + doporučení
"""

import re
import itertools
import time
from backtest import run_backtest, load_csv, load_parquet, resample_df, _compute_stats_fast, _simulate_vectorized, _simulate_run_backtest_style, detect_bot_style, _sanitize_code, _generate_signals_vectorized


# ═══════════════════════════════════════════════════════════════════════════════
#  DETEKCE OPTIMALIZOVATELNÝCH PARAMETRŮ
# ═══════════════════════════════════════════════════════════════════════════════

def detect_opt_params(code: str) -> list:
    """
    Detekuje optimalizovatelné parametry ze dvou formátů:

    Formát 1 — proměnné s komentářem:
        EMA_PERIOD = 9    # opt: 5-21
        SL_POINTS  = 18   # opt: 10-30,2

    Formát 2 — slovník DEFAULT_PARAMS s komentářem:
        DEFAULT_PARAMS = {
            "flag_range_mult": 1.0,   # opt: 0.5-2.0,0.1
            "target_rr":       1.5,   # opt: 1.0-3.0,0.5
        }

    Formát 3 — dict entry bez komentáře ale s # opt inline:
        "ema_fast": 20,    # opt: 5-50,5
    """
    params = []
    seen   = set()

    # Formát 1: VELKA_PROMENNA = hodnota  # opt: min-max[,step]
    pat1 = r'([A-Z_][A-Z0-9_]*)\s*=\s*([\d.]+)\s*#\s*opt:\s*([\d.]+)-([\d.]+)(?:,([\d.]+))?'
    for m in re.finditer(pat1, code):
        key = m.group(1)
        if key in seen: continue
        seen.add(key)
        params.append(_make_param(key, float(m.group(2)),
                                  float(m.group(3)), float(m.group(4)),
                                  m.group(5)))

    # Formát 2 & 3: "klic": hodnota,  # opt: min-max[,step]
    pat2 = r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:\s*([\d.]+).*?#\s*opt:\s*([\d.]+)-([\d.]+)(?:,([\d.]+))?'
    for m in re.finditer(pat2, code):
        key = m.group(1).upper()   # Normalizuj na VELKA_PISMENA pro konzistenci
        raw_key = m.group(1)       # Původní klíč pro dict přístup
        if key in seen: continue
        seen.add(key)
        p = _make_param(key, float(m.group(2)),
                        float(m.group(3)), float(m.group(4)),
                        m.group(5))
        p["raw_key"] = raw_key     # Uloží původní klíč pro dict strategie
        p["is_dict_param"] = True  # Příznak že jde o dict parametr
        params.append(p)

    return params


def _make_param(key, default, min_val, max_val, step_str):
    step   = float(step_str) if step_str else _auto_step(min_val, max_val)
    values = []
    v = min_val
    while v <= max_val + 1e-9:
        values.append(round(v, 4))
        v = round(v + step, 10)
    return {
        "key":          key,
        "default":      default,
        "min":          min_val,
        "max":          max_val,
        "step":         step,
        "values":       values,
        "raw_key":      key,
        "is_dict_param":False,
    }


def _auto_step(mn, mx):
    rng = mx - mn
    if rng <= 5:    return 0.5
    if rng <= 20:   return 1.0
    if rng <= 50:   return 2.0
    return 5.0


def estimate_combinations(params: list) -> int:
    total = 1
    for p in params:
        total *= len(p["values"])
    return total


# ═══════════════════════════════════════════════════════════════════════════════
#  SKÓROVACÍ FUNKCE
# ═══════════════════════════════════════════════════════════════════════════════

def score_result(stats: dict, goals: dict) -> float:
    """
    Vypočítá skóre výsledku backtestů podle cílů.
    Vrátí None pouze pokud ŽÁDNÉ trady nebo nesplňuje povinné cíle.
    Pokud nejsou žádné cíle zadány → vrátí skóre pro všechny výsledky.
    """
    if not stats:
        return None

    total = stats.get("total_trades", 0)
    min_t = goals.get("min_trades") or 3   # výrazně sníženo na 3
    if total < min_t:
        return None

    # Zkontroluj povinné podmínky POUZE pokud jsou zadány (> 0)
    min_wr = goals.get("min_winrate")
    if min_wr and float(min_wr) > 0 and stats["winrate"] < float(min_wr):
        return None

    min_rr = goals.get("min_rr")
    if min_rr and float(min_rr) > 0 and stats["avg_rr"] < float(min_rr):
        return None

    max_dd = goals.get("max_dd")
    if max_dd and float(max_dd) > 0 and stats["max_dd"] > float(max_dd):
        return None

    # Skóre = vážená kombinace metrik (vždy kladné i pro špatné výsledky)
    wr_score  = max(stats["winrate"] / 100, 0)
    pf_score  = min(max(stats["profit_factor"] / 3, 0), 1.0)
    rr_score  = min(max(stats["avg_rr"] / 3, 0), 1.0)
    pnl_norm  = stats["total_pnl"] / max(abs(stats["total_pnl"]), 1)
    pnl_score = min(max((pnl_norm + 1) / 2, 0), 1.0)
    dd_max    = max(stats["max_dd"], 1)
    dd_score  = 1.0 - min(dd_max / 10000, 1.0)
    sh_score  = min(max((stats.get("sharpe_ratio", 0) + 3) / 6, 0), 1.0)

    score = (
        wr_score  * 0.25 +
        pf_score  * 0.20 +
        rr_score  * 0.20 +
        pnl_score * 0.15 +
        dd_score  * 0.10 +
        sh_score  * 0.10
    )
    return round(score * 100, 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  HLAVNÍ OPTIMALIZÁTOR
# ═══════════════════════════════════════════════════════════════════════════════

def run_optimization(
    bot_code:      str,
    csv_content:   str,
    insample_from: str,
    insample_to:   str,
    oos_from:      str,
    oos_to:        str,
    base_params:   dict,
    goals:         dict,
    timeframe:     int = 3,
    max_combos:    int = 500,
    parquet_bytes: bytes = None,
) -> dict:
    """
    Walk-Forward Optimization.

    Vrátí:
    {
      "success": bool,
      "error":   str | None,
      "opt_params": [...],       # detekované parametry
      "total_combos": int,       # celkový počet kombinací
      "tested_combos": int,      # kolik bylo otestováno
      "top_results": [...],      # top 10 z in-sample
      "best_params": {...},      # nejlepší parametry
      "insample_stats": {...},   # stats nejlepší kombinace na IS
      "oos_stats": {...},        # stats na OOS
      "oos_equity": [...],
      "verdict": "PASS"|"FAIL"|"WARN",
      "verdict_msg": str,
      "elapsed_sec": float,
    }
    """
    t0 = time.time()

    try:
        # 1. Načti a resampluj data (Parquet má prioritu)
        if parquet_bytes:
            df_full = load_parquet(parquet_bytes)
            if df_full is None or df_full.empty:
                return {"success": False, "error": "Nepodařilo se načíst Parquet soubor"}
        else:
            df_full = load_csv(csv_content)
            if df_full is None or df_full.empty:
                return {"success": False, "error": "Nepodařilo se načíst CSV"}

        if timeframe > 1:
            df_full = resample_df(df_full, timeframe)

        # 2. Detekuj optimalizovatelné parametry
        opt_params = detect_opt_params(bot_code)
        if not opt_params:
            return {"success": False,
                    "error": ("Žádné optimalizovatelné parametry nenalezeny.\n"
                              "Označ parametry v kódu bota komentářem # opt: min-max\n"
                              "Příklad: EMA_PERIOD = 9  # opt: 5-21")}

        total_combos = estimate_combinations(opt_params)

        # 3. Pokud příliš mnoho kombinací — zredukuj step
        if total_combos > max_combos:
            opt_params = _reduce_params(opt_params, max_combos)
            total_combos = estimate_combinations(opt_params)

        # 4. Grid search na IN-SAMPLE
        keys   = [p["key"]   for p in opt_params]
        values = [p["values"] for p in opt_params]
        raw_keys = [p.get("raw_key", p["key"]) for p in opt_params]
        is_dict  = [p.get("is_dict_param", False) for p in opt_params]
        # Zachovej metadata pro správné předávání dict parametrů
        _opt_meta = {p["key"]: p for p in opt_params}

        results = []
        tested  = 0

        # Pre-generuj signály jednou pro celý IS dataset (rychlost!)
        style_info = detect_bot_style(_sanitize_code(bot_code))
        prepared_code = _sanitize_code(bot_code)
        # Filtruj IS dataset
        import pandas as pd
        df_is = df_full.copy()
        if insample_from:
            ts = pd.Timestamp(insample_from)
            if df_is.index.tz is not None:
                ts = ts.tz_localize("UTC")
            df_is = df_is[df_is.index >= ts]
        if insample_to:
            ts = pd.Timestamp(insample_to)
            if df_is.index.tz is not None:
                ts = ts.tz_localize("UTC")
            df_is = df_is[df_is.index <= ts]

        for combo in itertools.product(*values):
            params = dict(base_params)
            for opt_p, v in zip(opt_params, combo):
                k     = opt_p["key"]
                raw_k = opt_p.get("raw_key", k)
                # Předej všechny varianty klíče pro max kompatibilitu
                params[k]          = v   # VELKÁ
                params[raw_k]      = v   # původní (malá pro dict)
                params[k.lower()]  = v   # vždy malá
                params[k.upper()]  = v   # vždy velká

            try:
                # Simuluj podle stylu strategie
                if style_info["style"] == "run_backtest":
                    trades, err = _simulate_run_backtest_style(prepared_code, df_is, params)
                else:
                    signals = _generate_signals_vectorized(df_is, style_info, prepared_code, params)
                    trades  = _simulate_vectorized(df_is, signals, style_info, params)

                if trades and len(trades) >= (goals.get("min_trades") or 3):
                    fast_stats = _compute_stats_fast(trades)
                    sc = score_result(fast_stats, goals)
                    if sc is not None:
                        results.append({
                            "params": dict(zip(keys, combo)),
                            "stats":  fast_stats,
                            "score":  sc,
                        })
            except Exception:
                pass

            tested += 1

        if not results:
            return {
                "success": True,
                "opt_params": opt_params,
                "total_combos": total_combos,
                "tested_combos": tested,
                "top_results": [],
                "best_params": None,
                "insample_stats": None,
                "oos_stats": None,
                "oos_equity": [],
                "verdict": "FAIL",
                "verdict_msg": "Žádná kombinace parametrů nesplnila zadané cíle na in-sample datech.",
                "elapsed_sec": round(time.time() - t0, 1),
            }

        # 5. Seřaď a vyber top 10
        results.sort(key=lambda x: x["score"], reverse=True)
        top10 = results[:10]
        best  = top10[0]

        # 6. Verifikace na OUT-OF-SAMPLE
        oos_params = dict(base_params)
        oos_params.update(best["params"])

        oos_result = run_backtest(
            bot_code, csv_content,
            oos_from, oos_to,
            oos_params, timeframe
        )

        oos_stats  = oos_result["stats"]  if oos_result["success"] else {}
        oos_equity = oos_result["equity"] if oos_result["success"] else []

        # 7. Verdict
        verdict, verdict_msg = _evaluate_verdict(best["stats"], oos_stats, goals)

        return {
            "success":        True,
            "error":          None,
            "opt_params":     opt_params,
            "total_combos":   total_combos,
            "tested_combos":  tested,
            "top_results":    top10,
            "best_params":    best["params"],
            "insample_stats": best["stats"],
            "oos_stats":      oos_stats,
            "oos_equity":     oos_equity,
            "verdict":        verdict,
            "verdict_msg":    verdict_msg,
            "elapsed_sec":    round(time.time() - t0, 1),
        }

    except Exception as e:
        import traceback
        return {"success": False, "error": f"Chyba: {e}\n{traceback.format_exc()}"}


def _reduce_params(params: list, max_combos: int) -> list:
    """Zredukuje počet hodnot aby bylo max_combos kombinací."""
    import math
    n = len(params)
    target_per_param = int(max_combos ** (1/n))
    new_params = []
    for p in params:
        vals = p["values"]
        if len(vals) > target_per_param:
            step = max(1, len(vals) // target_per_param)
            vals = vals[::step]
        new_params.append({**p, "values": vals})
    return new_params


def _evaluate_verdict(is_stats: dict, oos_stats: dict, goals: dict) -> tuple:
    """Porovná IS a OOS výsledky a vydá verdikt."""
    if not oos_stats or not oos_stats.get("total_trades"):
        return "WARN", "OOS data neobsahují dostatek tradů pro spolehlivé hodnocení."

    is_wr  = is_stats.get("winrate", 0)
    oos_wr = oos_stats.get("winrate", 0)
    is_pnl  = is_stats.get("total_pnl", 0)
    oos_pnl = oos_stats.get("total_pnl", 0)
    oos_dd  = oos_stats.get("max_dd", 0)

    # Degradace výkonu IS → OOS
    wr_drop  = is_wr - oos_wr
    pnl_drop = (is_pnl - oos_pnl) / max(abs(is_pnl), 1) * 100

    # Zkontroluj cíle na OOS
    goals_met = True
    if goals.get("min_winrate") and oos_wr < goals["min_winrate"]: goals_met = False
    if goals.get("max_dd") and oos_dd > goals["max_dd"]:           goals_met = False
    if goals.get("min_rr") and oos_stats.get("avg_rr",0) < goals["min_rr"]: goals_met = False

    if goals_met and wr_drop < 10 and pnl_drop < 30:
        return "PASS", (
            f"✅ Strategie je robustní! OOS winrate {oos_wr}% (pokles jen {wr_drop:.1f}%). "
            f"Parametry jsou pravděpodobně použitelné pro live trading."
        )
    elif goals_met and wr_drop < 20:
        return "WARN", (
            f"⚠️ Strategie prošla s výhradami. OOS winrate {oos_wr}% (pokles {wr_drop:.1f}%). "
            f"Doporučujeme opatrný live trading s menší kontraktáží."
        )
    else:
        return "FAIL", (
            f"❌ Strategie selhala na OOS datech. IS winrate {is_wr}% → OOS {oos_wr}% "
            f"(pokles {wr_drop:.1f}%). Pravděpodobný overfitting na in-sample data."
        )
