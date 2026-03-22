"""
database.py – SQLite vrstva pro Trading Hub
Vytvoří trading_hub.db automaticky při prvním spuštění.
"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data" / "trading_hub.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bots (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT DEFAULT '',
                instrument  TEXT DEFAULT '',
                code        TEXT DEFAULT '',
                params      TEXT DEFAULT '{}',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id      TEXT NOT NULL,
                bot_name    TEXT NOT NULL,
                instrument  TEXT NOT NULL,
                direction   TEXT NOT NULL,
                contracts   INTEGER NOT NULL DEFAULT 1,
                entry       REAL,
                exit_price  REAL,
                sl          REAL,
                tp          REAL,
                pnl         REAL DEFAULT 0,
                entry_time  TEXT,
                exit_time   TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS optimization_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id           TEXT NOT NULL,
                bot_name         TEXT NOT NULL,
                insample_from    TEXT,
                insample_to      TEXT,
                oos_from         TEXT,
                oos_to           TEXT,
                best_params      TEXT,
                insample_stats   TEXT,
                oos_stats        TEXT,
                oos_equity       TEXT,
                top_results      TEXT,
                verdict          TEXT,
                verdict_msg      TEXT,
                tested_combos    INTEGER,
                elapsed_sec      REAL,
                created_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS backtest_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id           TEXT NOT NULL,
                bot_name         TEXT NOT NULL,
                period_from      TEXT,
                period_to        TEXT,
                starting_balance REAL DEFAULT 50000,
                total_pnl        REAL,
                winrate          REAL,
                avg_rr           REAL,
                max_dd           REAL,
                total_trades     INTEGER,
                trades_json      TEXT,
                equity_json      TEXT,
                stats_json       TEXT,
                created_at       TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS data_files (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                csv_id           TEXT NOT NULL UNIQUE,
                original_name    TEXT NOT NULL,
                rows             INTEGER DEFAULT 0,
                format           TEXT DEFAULT 'parquet',
                created_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_bot_id    ON trades(bot_id);
            CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time DESC);
            CREATE INDEX IF NOT EXISTS idx_bt_bot_id         ON backtest_results(bot_id);
            CREATE INDEX IF NOT EXISTS idx_opt_bot_id        ON optimization_results(bot_id);
        """)
    print("OK Databaze inicializovana")


# ═══ BOTS ═════════════════════════════════════════════════════════════════════

def get_all_bots():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM bots ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_bot(bot_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        return dict(row) if row else None


def create_bot(bot_id: str, name: str, description: str, instrument: str, code: str, params: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bots (id,name,description,instrument,code,params) VALUES (?,?,?,?,?,?)",
            (bot_id, name, description, instrument, code, json.dumps(params))
        )


def update_bot(bot_id: str, name: str, description: str, instrument: str, code: str, params: dict):
    with get_conn() as conn:
        conn.execute(
            """UPDATE bots SET name=?,description=?,instrument=?,code=?,params=?,
               updated_at=datetime('now') WHERE id=?""",
            (name, description, instrument, code, json.dumps(params), bot_id)
        )


def delete_bot(bot_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        conn.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))


# ═══ TRADES ═══════════════════════════════════════════════════════════════════

def log_trade(bot_id: str, bot_name: str, instrument: str, direction: str,
              contracts: int, entry: float, exit_price: float,
              sl: float, tp: float, pnl: float,
              entry_time: str, exit_time: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (bot_id,bot_name,instrument,direction,contracts,entry,exit_price,
                sl,tp,pnl,entry_time,exit_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bot_id, bot_name, instrument, direction, contracts,
             entry, exit_price, sl, tp, pnl, entry_time, exit_time)
        )


def get_trades(bot_id=None, period=None, limit=500):
    """
    period: 'week' | 'month' | None (= vše)
    """
    wheres, args = [], []

    if bot_id and bot_id != "all":
        wheres.append("bot_id=?")
        args.append(bot_id)

    if period == "week":
        wheres.append("entry_time >= datetime('now','-7 days')")
    elif period == "month":
        wheres.append("entry_time >= datetime('now','-30 days')")

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    query = f"SELECT * FROM trades {where_sql} ORDER BY entry_time DESC LIMIT ?"
    args.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]


# ═══ STATISTIKY ═══════════════════════════════════════════════════════════════

def get_stats(period=None):
    """Vrátí statistiky per-bot + celkovou equity křivku."""
    trades = get_trades(period=period, limit=10000)
    if not trades:
        return {"bots": [], "equity": [], "totals": {}}

    # Per-bot agregace
    bot_map = {}
    for t in trades:
        bid = t["bot_id"]
        if bid not in bot_map:
            bot_map[bid] = {
                "bot_id": bid, "bot_name": t["bot_name"],
                "instrument": t["instrument"],
                "trades": [], "pnl_list": []
            }
        bot_map[bid]["trades"].append(t)
        bot_map[bid]["pnl_list"].append(t["pnl"] or 0)

    bot_stats = []
    for bid, data in bot_map.items():
        pnls = data["pnl_list"]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_trades = len(pnls)
        winrate = round(len(wins) / total_trades * 100, 1) if total_trades else 0
        total_pnl = round(sum(pnls), 2)
        avg_win  = round(sum(wins) / len(wins), 2) if wins else 0
        avg_loss = round(abs(sum(losses) / len(losses)), 2) if losses else 0
        avg_rr   = round(avg_win / avg_loss, 2) if avg_loss else 0

        # Průměrná délka obchodu v minutách
        durations = []
        for t in data["trades"]:
            try:
                e = datetime.fromisoformat(t["entry_time"])
                x = datetime.fromisoformat(t["exit_time"])
                durations.append((x - e).total_seconds() / 60)
            except Exception:
                pass
        avg_duration = round(sum(durations) / len(durations), 0) if durations else 0

        bot_stats.append({
            "bot_id":       bid,
            "bot_name":     data["bot_name"],
            "instrument":   data["instrument"],
            "total_trades": total_trades,
            "winrate":      winrate,
            "avg_rr":       avg_rr,
            "total_pnl":    total_pnl,
            "avg_duration": avg_duration,
        })

    # Celková equity křivka (chronologicky)
    all_trades_sorted = sorted(trades, key=lambda t: t.get("exit_time") or "")
    equity, running = [], 0
    for t in all_trades_sorted:
        running += t["pnl"] or 0
        equity.append({
            "time":   t.get("exit_time", "")[:16],
            "value":  round(running, 2),
            "bot":    t["bot_name"],
            "pnl":    t["pnl"],
        })

    total_pnl = round(sum(t["pnl"] or 0 for t in trades), 2)
    total_wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
    overall_wr = round(total_wins / len(trades) * 100, 1) if trades else 0

    return {
        "bots":   bot_stats,
        "equity": equity,
        "totals": {
            "total_trades": len(trades),
            "total_pnl":    total_pnl,
            "winrate":      overall_wr,
        }
    }


# ═══ BACKTEST ═════════════════════════════════════════════════════════════════

def save_optimization(bot_id, bot_name, insample_from, insample_to,
                      oos_from, oos_to, best_params, insample_stats,
                      oos_stats, oos_equity, top_results, verdict,
                      verdict_msg, tested_combos, elapsed_sec):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO optimization_results
            (bot_id,bot_name,insample_from,insample_to,oos_from,oos_to,
             best_params,insample_stats,oos_stats,oos_equity,top_results,
             verdict,verdict_msg,tested_combos,elapsed_sec)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bot_id, bot_name, insample_from, insample_to, oos_from, oos_to,
             json.dumps(best_params or {}), json.dumps(insample_stats or {}),
             json.dumps(oos_stats or {}), json.dumps(oos_equity or []),
             json.dumps(top_results or []),
             verdict, verdict_msg, tested_combos, elapsed_sec)
        )


def get_optimization_results(bot_id=None, limit=20):
    try:
        where = "WHERE bot_id=?" if bot_id else ""
        args  = [bot_id] if bot_id else []
        args.append(limit)
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id,bot_id,bot_name,insample_from,insample_to,oos_from,oos_to,"
                f"verdict,verdict_msg,tested_combos,elapsed_sec,created_at "
                f"FROM optimization_results {where} ORDER BY created_at DESC LIMIT ?",
                args
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_optimization_detail(result_id: int):
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM optimization_results WHERE id=?", (result_id,)
            ).fetchone()
        if not row: return None
        r = dict(row)
        for k in ["best_params","insample_stats","oos_stats","oos_equity","top_results"]:
            try: r[k] = json.loads(r.get(k) or "{}")
            except: pass
        return r
    except Exception:
        return None


def delete_optimization(result_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM optimization_results WHERE id=?", (result_id,))


def save_backtest(bot_id, bot_name, period_from, period_to,
                  total_pnl, winrate, avg_rr, max_dd, total_trades,
                  trades_list, equity_list, starting_balance=50000, stats_dict=None):
    with get_conn() as conn:
        # Přidej sloupce pokud chybí (migration)
        try:
            conn.execute("ALTER TABLE backtest_results ADD COLUMN starting_balance REAL DEFAULT 50000")
        except Exception: pass
        try:
            conn.execute("ALTER TABLE backtest_results ADD COLUMN stats_json TEXT")
        except Exception: pass
        conn.execute(
            """INSERT INTO backtest_results
               (bot_id,bot_name,period_from,period_to,starting_balance,
                total_pnl,winrate,avg_rr,max_dd,total_trades,
                trades_json,equity_json,stats_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bot_id, bot_name, period_from, period_to, starting_balance,
             total_pnl, winrate, avg_rr, max_dd, total_trades,
             json.dumps(trades_list), json.dumps(equity_list),
             json.dumps(stats_dict or {}))
        )


def delete_backtest(result_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM backtest_results WHERE id=?", (result_id,))


def save_data_file(csv_id, original_name, rows=0, fmt="parquet"):
    with get_conn() as conn:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS data_files (id INTEGER PRIMARY KEY AUTOINCREMENT, csv_id TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL, rows INTEGER DEFAULT 0, format TEXT DEFAULT 'parquet', created_at TEXT DEFAULT (datetime('now')))")
        except Exception:
            pass
        conn.execute(
            "INSERT OR REPLACE INTO data_files (csv_id, original_name, rows, format) VALUES (?,?,?,?)",
            (csv_id, original_name, rows, fmt)
        )


def get_data_files():
    with get_conn() as conn:
        try:
            rows = conn.execute("SELECT * FROM data_files ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def delete_data_file(file_id):
    with get_conn() as conn:
        row = conn.execute("SELECT csv_id FROM data_files WHERE id=?", (file_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM data_files WHERE id=?", (file_id,))
            return row["csv_id"]
        return None


def get_backtest_results(bot_id=None, limit=10):
    where = "WHERE bot_id=?" if bot_id else ""
    args = [bot_id] if bot_id else []
    args.append(limit)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM backtest_results {where} ORDER BY created_at DESC LIMIT ?",
            args
        ).fetchall()
        return [dict(r) for r in rows]
