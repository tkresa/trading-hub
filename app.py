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
# DB funkce pro optimalizace importujeme přímo z db modulu

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB max upload


def _cleanup_csv_cache():
    """Smaže CSV cache soubory starší než 24 hodin."""
    import time
    csv_dir = Path(__file__).parent / "data" / "csv_cache"
    if not csv_dir.exists():
        return
    now = time.time()
    deleted = 0
    for f in csv_dir.glob("*.csv"):
        try:
            if now - f.stat().st_mtime > 86400:  # 24 hodin
                f.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        print(f"[CSV Cache] Smazáno {deleted} starých souborů")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload

# ── Process registry ───────────────────────────────────────────────────────
_processes: dict = {}
_log_buffers: dict = {}
MAX_LOG = 500


def bot_status(bot_id):
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
            # Nahraď DOPLNIT na tomto řádku pokud řádek obsahuje název proměnné
            if key in line:
                modified = re.sub(r'["\']DOPLNIT["\']', f'"{value}"', modified)
                break
        result.append(modified)
    return '\n'.join(result)


def reinject_params(code, old_params, new_params):
    """
    Nahradí stávající hodnoty params v kódu novými hodnotami.
    Používá se při editaci, kdy kód již nemá 'DOPLNIT' (bylo nahrazeno).
    """
    lines = code.split('\n')
    result = []
    for line in lines:
        modified = line
        for key, new_val in new_params.items():
            old_val = old_params.get(key)
            if key in line and old_val is not None:
                modified = re.sub(r'"' + re.escape(str(old_val)) + '"', f'"{new_val}"', modified)
                modified = re.sub(r"'" + re.escape(str(old_val)) + "'", f'"{new_val}"', modified)
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
    return jsonify({"error": "Soubor je příliš velký. Maximum je 500MB."}), 413


@app.route("/favicon.ico")
def favicon():
    """Jednoduchý favicon aby prohlížeč nehazoval 404."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="8" fill="#3b82f6"/>
  <text x="16" y="23" font-size="20" text-anchor="middle" fill="white">&#x26A1;</text>
</svg>'''
    from flask import Response
    return Response(svg, mimetype="image/svg+xml")


@app.route("/")
def index():
    return render_template("index.html")


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Soubor je příliš velký. Maximum je 500 MB."}), 413


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
    code = data.get("code", "")
    params = data.get("params", {})
    bot_id = uuid.uuid4().hex[:10]
    db.create_bot(bot_id, data.get("name","Nový bot"),
                  data.get("description",""), data.get("instrument",""),
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
    return jsonify({"status": "deleted"})


@app.route("/api/bots/scan", methods=["POST"])
def api_scan():
    code = request.json.get("code", "")
    return jsonify({"fields": scan_placeholders(code)})


# ═══ START / STOP ══════════════════════════════════════════════════════════

# ── Progress tracking ─────────────────────────────────────────────────────
_progress: dict = {}  # task_id → {status, pct, msg}

@app.route("/api/progress/<task_id>")
def get_progress(task_id):
    return jsonify(_progress.get(task_id, {"status": "unknown", "pct": 0, "msg": ""}))

def set_progress(task_id: str, pct: int, msg: str, status: str = "running"):
    _progress[task_id] = {"status": status, "pct": pct, "msg": msg}


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
    Přijme CSV soubor přes multipart upload (bez načítání do paměti prohlížeče).
    Vrátí csv_id pro použití v backtestu a optimalizaci.
    """
    import uuid
    # Multipart upload (preferovaný - nespotřebuje paměť prohlížeče)
    if "file" in request.files:
        f = request.files["file"]
        csv_id  = uuid.uuid4().hex[:12]
        csv_dir = Path(__file__).parent / "data" / "csv_cache"
        csv_dir.mkdir(exist_ok=True)
        csv_path = csv_dir / f"{csv_id}.csv"
        f.save(str(csv_path))
        # Spočítej řádky efektivně
        rows = sum(1 for _ in open(csv_path, encoding="utf-8", errors="replace")) - 1
        return jsonify({"csv_id": csv_id, "rows": rows})

    # Fallback: JSON upload (pro menší soubory)
    csv_data = request.json.get("csv", "") if request.is_json else ""
    if not csv_data:
        return jsonify({"error": "Prázdné CSV nebo chybí soubor"}), 400
    csv_id  = uuid.uuid4().hex[:12]
    csv_dir = Path(__file__).parent / "data" / "csv_cache"
    csv_dir.mkdir(exist_ok=True)
    csv_path = csv_dir / f"{csv_id}.csv"
    csv_path.write_text(csv_data, encoding="utf-8")
    rows = csv_data.count("\n")
    return jsonify({"csv_id": csv_id, "rows": rows})


def _load_csv_by_id(csv_id: str) -> str | None:
    """Načte CSV ze serveru podle ID."""
    csv_path = Path(__file__).parent / "data" / "csv_cache" / f"{csv_id}.csv"
    if csv_path.exists():
        return csv_path.read_text(encoding="utf-8")
    return None


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    data = request.json
    b = db.get_bot(data.get("bot_id"))
    if not b: return jsonify({"error": "Bot nenalezen"}), 404
    params = json.loads(b.get("params") or "{}")
    timeframe     = int(data.get("timeframe", 1))
    start_balance = float(data.get("start_balance", 50000))
    task_id       = data.get("task_id", "bt_default")
    set_progress(task_id, 5, "Načítám CSV a resamplinguji data...")
    params["start_balance"] = start_balance
    # Podporuj csv_id (ze serveru) i přímé csv (zpětná kompatibilita)
    csv_content = data.get("csv", "")
    if not csv_content and data.get("csv_id"):
        csv_content = _load_csv_by_id(data["csv_id"]) or ""
    import uuid
    op_id = uuid.uuid4().hex[:8]
    _set_progress(op_id, 10, "Načítám data...")
    result = run_backtest(b["code"], csv_content,
                          data.get("period_from",""), data.get("period_to",""),
                          params, timeframe_minutes=timeframe)
    _set_progress(op_id, 100, "Hotovo")
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


# Progress tracking pro dlouhé operace
_progress_store: dict = {}


def _set_progress(op_id: str, pct: int, msg: str):
    _progress_store[op_id] = {"pct": pct, "msg": msg}


@app.route("/api/progress/<op_id>")
def api_progress(op_id):
    """SSE stream pro progress bar."""
    import time
    def gen():
        last = None
        for _ in range(600):  # max 10 min
            p = _progress_store.get(op_id, {"pct": 0, "msg": "Čekám..."})
            if p != last:
                yield f"data: {json.dumps(p)}\n\n"
                last = p
            if p.get("pct", 0) >= 100:
                break
            time.sleep(1)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


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
    if not csv_content and data.get("csv_id"):
        csv_content = _load_csv_by_id(data["csv_id"]) or ""
    result = run_optimization(
        bot_code      = b["code"] or "",
        csv_content   = csv_content,
        insample_from = data.get("insample_from", ""),
        insample_to   = data.get("insample_to", ""),
        oos_from      = data.get("oos_from", ""),
        oos_to        = data.get("oos_to", ""),
        base_params   = base_params,
        goals         = goals,
        timeframe     = int(data.get("timeframe", 3)),
        max_combos    = int(data.get("max_combos", 500)),
    )
    # Ulož výsledek do DB
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


# ═══ START ═════════════════════════════════════════════════════════════════

def cleanup_csv_cache():
    """Smaže CSV soubory starší než 24 hodin."""
    import time
    cache_dir = Path(__file__).parent / "data" / "csv_cache"
    if not cache_dir.exists():
        return
    now = time.time()
    deleted = 0
    for f in cache_dir.glob("*.csv"):
        if now - f.stat().st_mtime > 86400:  # 24h
            f.unlink()
            deleted += 1
    if deleted:
        print(f"🧹 CSV cache: smazáno {deleted} starých souborů")


if __name__ == "__main__":
    db.init_db()
    cleanup_csv_cache()
    print("\n" + "═"*50)
    print("  ⚡  TRADING HUB")
    print("  🌐  http://localhost:5000")
    print("  🛑  Zastav: Ctrl+C")
    print("═"*50 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
