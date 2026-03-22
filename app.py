# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Trading Hub – app.py
Spusť: python app.py
Otevři: http://localhost:5000
"""

import json
import os
import re
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, render_template, Response

# Windows UTF-8 fix
import sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import database as db
from backtest import run_backtest
from optimizer import run_optimization, detect_opt_params, estimate_combinations

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload

# ── Process registry ───────────────────────────────────────────────────────
_processes: dict = {}
_log_buffers: dict = {}
_lock = threading.Lock()
MAX_LOG = 500


def bot_status(bot_id):
    with _lock:
        proc = _processes.get(bot_id)
    if proc is None: return "stopped"
    return "running" if proc.poll() is None else "stopped"


def append_log(bot_id, line):
    buf = _log_buffers.setdefault(bot_id, [])
    ts = datetime.now().strftime("%H:%M:%S")
    buf.append(f"[{ts}] {line.rstrip()}")
    if len(buf) > MAX_LOG:
        buf.pop(0)


def scan_placeholders(code):
    """
    Najde proměnné označené DOPLNIT - funguje pro všechny vzory:
      USERNAME = 'DOPLNIT'
      os.getenv('TOPSTEPX_USERNAME', 'DOPLNIT')
      ACCOUNT_ID = int(os.getenv('TOPSTEPX_ACCOUNT_ID', 'DOPLNIT'))
    Na každém řádku vybere NEJDELŠÍ proměnnou = nejspecifičtější název.
    """
    SKIP = {'DOPLNIT', 'TRUE', 'FALSE', 'NONE', 'UTF', 'LIVE', 'DEMO',
            'INT', 'STR', 'FLOAT', 'BOOL', 'OS', 'ENV', 'GET'}
    fields, seen = [], set()
    for line in code.split("\n"):
        if 'DOPLNIT' not in line:
            continue
        candidates = re.findall(r'([A-Z_][A-Z0-9_]{2,})', line)
        candidates = [c for c in candidates if c not in SKIP]
        if not candidates:
            continue
        # Vyber nejdelší = nejspecifičtější název (TOPSTEPX_USERNAME > USERNAME)
        key = max(candidates, key=len)
        if key not in seen:
            seen.add(key)
            label = key.replace('_', ' ').title()
            fields.append({"key": key, "label": label})
    return fields


def inject_params(code, params):
    """
    Nahradí DOPLNIT v kódu — funguje pro všechny vzory na řádku.
    Prochází řádek po řádku a nahrazuje 'DOPLNIT' pokud řádek
    obsahuje název dané proměnné.
    """
    lines = code.split('\n')
    result = []
    for line in lines:
        if 'DOPLNIT' not in line:
            result.append(line)
            continue
        modified = line
        for key, value in params.items():
            if key in line:
                modified = re.sub(r'["\']DOPLNIT["\']', f'"{value}"', modified)
                break
        result.append(modified)
    return '\n'.join(result)


def reinject_params(code, old_params, new_params):
    """
    Nahradí stávající hodnoty params v kódu novými hodnotami.
    Používá se při editaci, kdy kód již nemá 'DOPLNIT' (bylo nahrazeno).
    Strategie:
    1. Pokud známe starou hodnotu z old_params → nahraď ji
    2. Pokud ne → hledej vzor KEY = "cokoli" a nahraď hodnotu
    """
    lines = code.split('\n')
    result = []
    for line in lines:
        modified = line
        for key, new_val in new_params.items():
            if key not in line:
                continue
            old_val = old_params.get(key)
            if old_val is not None:
                modified = re.sub(r'"' + re.escape(str(old_val)) + '"', f'"{new_val}"', modified)
                modified = re.sub(r"'" + re.escape(str(old_val)) + "'", f'"{new_val}"', modified)
            else:
                # Fallback: nahraď hodnotu v přiřazení KEY = "cokoli"
                modified = re.sub(
                    re.escape(key) + r'\s*=\s*["\'][^"\']*["\']',
                    f'{key} = "{new_val}"', modified
                )
            break
        result.append(modified)
    return '\n'.join(result)


def stream_process(bot_id, proc):
    log_dir  = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{bot_id}.log"
    with open(log_file, "a", encoding="utf-8", errors="replace") as lf:
        for line in proc.stdout:
            append_log(bot_id, line)
            lf.write(line if line.endswith("\n") else line + "\n")
            lf.flush()
    append_log(bot_id, "── Proces ukončen ──")


# ═══ UI ═══════════════════════════════════════════════════════════════════

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Soubor je příliš velký. Maximum je 500 MB."}), 413


@app.route("/favicon.ico")
def favicon():
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="8" fill="#3b82f6"/>
  <text x="16" y="23" font-size="20" text-anchor="middle" fill="white">&#x26A1;</text>
</svg>'''
    return Response(svg, mimetype="image/svg+xml")


@app.route("/")
def index():
    return render_template("index.html")


# ═══ BOTS ═════════════════════════════════════════════════════════════════

@app.route("/api/bots")
def api_get_bots():
    bots = db.get_all_bots()
    for b in bots:
        b["status"] = bot_status(b["id"])
        b["params"] = json.loads(b.get("params") or "{}")
    return jsonify(bots)


@app.route("/api/bots/<bot_id>")
def api_get_bot(bot_id):
    b = db.get_bot(bot_id)
    if not b: return jsonify({"error": "Nenalezeno"}), 404
    b["status"] = bot_status(bot_id)
    b["params"] = json.loads(b.get("params") or "{}")
    b["placeholders"] = scan_placeholders(b.get("code", ""))
    return jsonify(b)


@app.route("/api/bots", methods=["POST"])
def api_create_bot():
    data = request.json
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Název bota je povinný"}), 400
    code = data.get("code", "")
    params = data.get("params", {})
    bot_id = uuid.uuid4().hex[:12]
    db.create_bot(bot_id, name,
                  data.get("description", ""), data.get("instrument", ""),
                  inject_params(code, params), params)
    return jsonify({"id": bot_id}), 201


@app.route("/api/bots/<bot_id>", methods=["PUT"])
def api_update_bot(bot_id):
    b = db.get_bot(bot_id)
    if not b: return jsonify({"error": "Nenalezeno"}), 404
    data = request.json
    code = data.get("code", b["code"])
    params = data.get("params", {})
    old_params = json.loads(b.get("params") or "{}")
    if 'DOPLNIT' in code:
        injected = inject_params(code, params)
    else:
        injected = reinject_params(code, old_params, params)
    db.update_bot(bot_id, data.get("name", b["name"]),
                  data.get("description", b["description"]),
                  data.get("instrument", b["instrument"]),
                  injected, params)
    if bot_status(bot_id) == "running":
        append_log(bot_id, "⚙️  Parametry aktualizovány")
    return jsonify({"status": "ok"})


@app.route("/api/bots/<bot_id>", methods=["DELETE"])
def api_delete_bot(bot_id):
    _stop_bot(bot_id)
    db.delete_bot(bot_id)
    _log_buffers.pop(bot_id, None)
    return jsonify({"status": "deleted"})


@app.route("/api/bots/scan", methods=["POST"])
def api_scan():
    code = request.json.get("code", "")
    return jsonify({"fields": scan_placeholders(code)})


# ═══ START / STOP ══════════════════════════════════════════════════════════

@app.route("/api/bots/<bot_id>/start", methods=["POST"])
def api_start(bot_id):
    if bot_status(bot_id) == "running":
        return jsonify({"status": "already_running"})
    b = db.get_bot(bot_id)
    if not b: return jsonify({"error": "Bot nenalezen"}), 404
    code = b.get("code", "").strip()
    if not code: return jsonify({"error": "Bot nemá kód"}), 400

    runtime_dir = Path(__file__).parent / "bots_runtime"
    runtime_dir.mkdir(exist_ok=True)
    script = runtime_dir / f"{bot_id}.py"
    script.write_text(code)

    proc = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=str(Path(__file__).parent),
        env={**os.environ, "BOT_ID": bot_id, "BOT_NAME": b["name"],
             "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    with _lock:
        _processes[bot_id] = proc
    # Načti existující logy ze souboru (přežijí restart aplikace)
    log_file = Path(__file__).parent / "logs" / f"{bot_id}.log"
    if log_file.exists():
        try:
            existing = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            _log_buffers[bot_id] = existing[-200:] if len(existing) > 200 else existing
        except Exception:
            _log_buffers[bot_id] = []
    else:
        _log_buffers[bot_id] = []
    append_log(bot_id, f"🚀 '{b['name']}' spuštěn (PID {proc.pid})")
    threading.Thread(target=stream_process, args=(bot_id, proc), daemon=True).start()
    return jsonify({"status": "started", "pid": proc.pid})


@app.route("/api/bots/<bot_id>/stop", methods=["POST"])
def api_stop(bot_id):
    _stop_bot(bot_id)
    return jsonify({"status": "stopped"})


def _stop_bot(bot_id):
    with _lock:
        proc = _processes.pop(bot_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        append_log(bot_id, "🛑 Bot zastaven")


# ═══ LOGS ══════════════════════════════════════════════════════════════════

@app.route("/api/bots/<bot_id>/logs")
def api_logs(bot_id):
    return jsonify({"lines": _log_buffers.get(bot_id, [])})


@app.route("/api/bots/<bot_id>/logs/stream")
def api_logs_stream(bot_id):
    def gen():
        last = 0
        while True:
            lines = _log_buffers.get(bot_id, [])
            if len(lines) > last:
                for line in lines[last:]:
                    yield f"data: {json.dumps(line)}\n\n"
                last = len(lines)
            time.sleep(0.4)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ═══ TRADES ════════════════════════════════════════════════════════════════

@app.route("/api/trades")
def api_trades():
    return jsonify(db.get_trades(
        bot_id=request.args.get("bot_id"),
        period=request.args.get("period")
    ))


# ═══ STATS ═════════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats(period=request.args.get("period")))


# ═══ BACKTEST ══════════════════════════════════════════════════════════════

@app.route("/api/csv/upload", methods=["POST"])
def api_csv_upload():
    """
    Přijme CSV nebo Parquet soubor přes multipart upload.
    Vrátí csv_id pro použití v backtestu a optimalizaci.
    """
    if "file" in request.files:
        f = request.files["file"]
        orig_name = f.filename or "data"
        filename = orig_name.lower()
        if filename.endswith(".parquet"):
            csv_id   = uuid.uuid4().hex[:12]
            csv_dir  = Path(__file__).parent / "data" / "csv_cache"
            csv_dir.mkdir(exist_ok=True)
            path = csv_dir / f"{csv_id}.parquet"
            f.save(str(path))
            try:
                import pandas as pd
                rows = len(pd.read_parquet(str(path), columns=["close"] if True else []))
            except Exception:
                rows = 0
            db.save_data_file(csv_id, orig_name, rows, "parquet")
            return jsonify({"csv_id": csv_id, "rows": rows, "format": "parquet", "name": orig_name})
        elif filename.endswith(".csv"):
            csv_id   = uuid.uuid4().hex[:12]
            csv_dir  = Path(__file__).parent / "data" / "csv_cache"
            csv_dir.mkdir(exist_ok=True)
            csv_path = csv_dir / f"{csv_id}.csv"
            f.save(str(csv_path))
            try:
                import pandas as pd
                from backtest import load_csv
                df = load_csv(csv_path.read_text(encoding="utf-8", errors="replace"))
                if df is not None and not df.empty:
                    pq_path = csv_dir / f"{csv_id}.parquet"
                    df.to_parquet(str(pq_path), index=True)
                    csv_path.unlink()
                    db.save_data_file(csv_id, orig_name, len(df), "parquet")
                    return jsonify({"csv_id": csv_id, "rows": len(df), "format": "parquet", "name": orig_name})
            except Exception:
                pass
            rows = sum(1 for _ in open(csv_path, encoding="utf-8", errors="replace")) - 1
            db.save_data_file(csv_id, orig_name, rows, "csv")
            return jsonify({"csv_id": csv_id, "rows": rows, "format": "csv", "name": orig_name})
        else:
            return jsonify({"error": "Soubor musí mít příponu .csv nebo .parquet"}), 400

    # Fallback: JSON upload (pro menší CSV soubory)
    csv_data = request.json.get("csv", "") if request.is_json else ""
    if not csv_data:
        return jsonify({"error": "Prázdné CSV nebo chybí soubor"}), 400
    csv_id   = uuid.uuid4().hex[:12]
    csv_dir  = Path(__file__).parent / "data" / "csv_cache"
    csv_dir.mkdir(exist_ok=True)
    try:
        import pandas as pd
        from backtest import load_csv
        df = load_csv(csv_data)
        if df is not None and not df.empty:
            pq_path = csv_dir / f"{csv_id}.parquet"
            df.to_parquet(str(pq_path), index=True)
            db.save_data_file(csv_id, "upload.csv", len(df), "parquet")
            return jsonify({"csv_id": csv_id, "rows": len(df), "format": "parquet", "name": "upload.csv"})
    except Exception:
        pass
    csv_path = csv_dir / f"{csv_id}.csv"
    csv_path.write_text(csv_data, encoding="utf-8")
    rows = csv_data.count("\n")
    db.save_data_file(csv_id, "upload.csv", rows, "csv")
    return jsonify({"csv_id": csv_id, "rows": rows, "format": "csv", "name": "upload.csv"})


@app.route("/api/data/files")
def api_data_files():
    return jsonify(db.get_data_files())


@app.route("/api/data/files/<int:file_id>", methods=["DELETE"])
def api_delete_data_file(file_id):
    csv_id = db.delete_data_file(file_id)
    if csv_id:
        base = Path(__file__).parent / "data" / "csv_cache"
        for ext in (".parquet", ".csv"):
            p = base / f"{csv_id}{ext}"
            if p.exists():
                p.unlink()
    return jsonify({"ok": True})


def _load_data_by_id(csv_id: str):
    """Načte data ze serveru podle ID (CSV nebo Parquet). Brání path traversal.
    Vrací (typ, obsah) kde typ je 'csv' nebo 'parquet' a obsah je str/bytes."""
    if not re.fullmatch(r'[0-9a-f]{12}', csv_id):
        return None, None
    base = Path(__file__).parent / "data" / "csv_cache"
    parquet_path = base / f"{csv_id}.parquet"
    csv_path     = base / f"{csv_id}.csv"
    if parquet_path.exists():
        return "parquet", parquet_path.read_bytes()
    if csv_path.exists():
        return "csv", csv_path.read_text(encoding="utf-8")
    return None, None


def _load_csv_by_id(csv_id: str) -> str | None:
    """Zpětná kompatibilita — vrací CSV obsah nebo None."""
    fmt, content = _load_data_by_id(csv_id)
    if fmt == "csv":
        return content
    return None


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    data = request.json
    b = db.get_bot(data.get("bot_id"))
    if not b: return jsonify({"error": "Bot nenalezen"}), 404
    params = json.loads(b.get("params") or "{}")
    timeframe     = max(1, int(data.get("timeframe", 1)))
    start_balance = float(data.get("start_balance", 50000))
    params["start_balance"] = start_balance
    if "commission" in data: params["commission"] = float(data["commission"])
    if "slippage"   in data: params["slippage"]   = float(data["slippage"])
    csv_content = data.get("csv", "")
    csv_bytes   = None
    if not csv_content and data.get("csv_id"):
        fmt, content = _load_data_by_id(data["csv_id"])
        if fmt == "parquet":
            csv_bytes = content
        else:
            csv_content = content or ""
    result = run_backtest(b["code"], csv_content,
                          data.get("period_from",""), data.get("period_to",""),
                          params, timeframe_minutes=timeframe,
                          parquet_bytes=csv_bytes)
    if result["success"]:
        s = result["stats"]
        db.save_backtest(b["id"], b["name"], data.get("period_from",""), data.get("period_to",""),
                         s["total_pnl"], s["winrate"], s["avg_rr"], s["max_dd"],
                         s["total_trades"], result["trades"], result["equity"],
                         starting_balance=start_balance, stats_dict=s)
    return jsonify(result)


@app.route("/api/backtest/history")
def api_backtest_history():
    results = db.get_backtest_results(request.args.get("bot_id"))
    for r in results:
        r.pop("trades_json", None); r.pop("equity_json", None)
    return jsonify(results)


@app.route("/api/backtest/<int:result_id>")
def api_backtest_detail(result_id):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM backtest_results WHERE id=?", (result_id,)).fetchone()
    if not row: return jsonify({"error": "Nenalezeno"}), 404
    r = dict(row)
    import json as _json
    r["trades"] = _json.loads(r.pop("trades_json", "[]"))
    r["equity"] = _json.loads(r.pop("equity_json", "[]"))
    r["stats"]  = _json.loads(r.pop("stats_json",  "{}"))
    return jsonify(r)


@app.route("/api/backtest/<int:result_id>", methods=["DELETE"])
def api_backtest_delete(result_id):
    db.delete_backtest(result_id)
    return jsonify({"status": "deleted"})


@app.route("/api/backtest/scan_params", methods=["POST"])
def api_scan_opt_params():
    """Detekuje optimalizovatelné parametry v kódu bota."""
    bot_id = request.json.get("bot_id")
    b = db.get_bot(bot_id)
    if not b: return jsonify({"error": "Bot nenalezen"}), 404
    params = detect_opt_params(b["code"] or "")
    combos = estimate_combinations(params)
    return jsonify({"params": params, "total_combos": combos})


@app.route("/api/backtest/optimize", methods=["POST"])
def api_optimize():
    """Spustí Walk-Forward Optimization."""
    data    = request.json
    bot_id  = data.get("bot_id")
    b = db.get_bot(bot_id)
    if not b: return jsonify({"error": "Bot nenalezen"}), 404

    import json as _json
    base_params = _json.loads(b.get("params") or "{}")
    base_params["start_balance"] = float(data.get("start_balance", 50000))

    goals = {
        "min_winrate": float(data["min_winrate"]) if data.get("min_winrate") else None,
        "min_rr":      float(data["min_rr"])      if data.get("min_rr")      else None,
        "max_dd":      float(data["max_dd"])       if data.get("max_dd")      else None,
        "min_trades":  int(data["min_trades"])     if data.get("min_trades")  else 10,
    }

    csv_content = data.get("csv", "")
    csv_bytes   = None
    if not csv_content and data.get("csv_id"):
        fmt, content = _load_data_by_id(data["csv_id"])
        if fmt == "parquet":
            csv_bytes = content
        else:
            csv_content = content or ""
    result = run_optimization(
        bot_code      = b["code"] or "",
        csv_content   = csv_content,
        insample_from = data.get("insample_from", ""),
        insample_to   = data.get("insample_to", ""),
        oos_from      = data.get("oos_from", ""),
        oos_to        = data.get("oos_to", ""),
        base_params   = base_params,
        goals         = goals,
        timeframe     = max(1, int(data.get("timeframe", 3))),
        max_combos    = int(data.get("max_combos", 500)),
        parquet_bytes = csv_bytes,
    )
    if result.get("success"):
        db.save_optimization(
            bot_id        = bot_id,
            bot_name      = b["name"],
            insample_from = data.get("insample_from",""),
            insample_to   = data.get("insample_to",""),
            oos_from      = data.get("oos_from",""),
            oos_to        = data.get("oos_to",""),
            best_params   = result.get("best_params"),
            insample_stats= result.get("insample_stats"),
            oos_stats     = result.get("oos_stats"),
            oos_equity    = result.get("oos_equity"),
            top_results   = result.get("top_results"),
            verdict       = result.get("verdict",""),
            verdict_msg   = result.get("verdict_msg",""),
            tested_combos = result.get("tested_combos",0),
            elapsed_sec   = result.get("elapsed_sec",0),
        )
    return jsonify(result)


@app.route("/api/optimization/history")
def api_opt_history():
    results = db.get_optimization_results(request.args.get("bot_id"))
    return jsonify(results)


@app.route("/api/optimization/<int:result_id>")
def api_opt_detail(result_id):
    r = db.get_optimization_detail(result_id)
    if not r: return jsonify({"error": "Nenalezeno"}), 404
    return jsonify(r)


@app.route("/api/optimization/<int:result_id>", methods=["DELETE"])
def api_opt_delete(result_id):
    db.delete_optimization(result_id)
    return jsonify({"status": "deleted"})


@app.route("/api/optimization/<int:result_id>/apply", methods=["POST"])
def api_opt_apply(result_id):
    """Aplikuje nejlepší parametry z optimalizace na bota."""
    r = db.get_optimization_detail(result_id)
    if not r: return jsonify({"error": "Nenalezeno"}), 404
    b = db.get_bot(r["bot_id"])
    if not b: return jsonify({"error": "Bot nenalezen"}), 404
    best = r.get("best_params", {})
    if not best: return jsonify({"error": "Žádné parametry"}), 400
    old_params = json.loads(b.get("params") or "{}")
    code = b["code"]
    if 'DOPLNIT' in code:
        injected = inject_params(code, best)
    else:
        injected = reinject_params(code, old_params, best)
    db.update_bot(r["bot_id"], b["name"], b["description"], b["instrument"], injected, best)
    return jsonify({"status": "ok", "params": best})


# ═══ START ═════════════════════════════════════════════════════════════════

def cleanup_csv_cache():
    """Smaže CSV/Parquet soubory starší než 24 hodin, pokud nejsou uložené v DB."""
    cache_dir = Path(__file__).parent / "data" / "csv_cache"
    if not cache_dir.exists():
        return
    saved_ids = {f["csv_id"] for f in db.get_data_files()}
    now = time.time()
    deleted = 0
    for pattern in ("*.csv", "*.parquet"):
        for f in cache_dir.glob(pattern):
            csv_id = f.stem
            if csv_id in saved_ids:
                continue  # uložený soubor — nemaž
            if now - f.stat().st_mtime > 86400:
                f.unlink()
                deleted += 1
    if deleted:
        print(f"Cache: smazano {deleted} starych souboru")


if __name__ == "__main__":
    db.init_db()
    cleanup_csv_cache()
    print("\n" + "═"*50)
    print("  ⚡  TRADING HUB")
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else int(os.environ.get("PORT", 5000))
    print(f"  🌐  http://localhost:{port}")
    print("  🛑  Zastav: Ctrl+C")
    print("═"*50 + "\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
