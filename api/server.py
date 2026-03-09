"""
QUANTRA BEAST v2 — Main API Server
All stages wired together:
  Data Pipeline → 7 Engines → Signal Fusion → Capital Mgmt → DB → Dashboard
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import datetime, threading, time, os, sys

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data_fetcher import fetch_full_snapshot, fetch_market_status
from signals.fusion_engine import generate_signal
from capital.risk_manager import RiskManager
from monitoring.trade_monitor import TradeMonitor
from database.db import init_db, save_signal, save_trade, close_trade, get_trade_history, get_performance_summary

app = Flask(__name__)
CORS(app)

# ── Global state
risk_manager    = None
active_monitor  = None
active_trade_id = None
last_signal     = None
last_snapshot   = None

# ── Init DB on startup
init_db()

# ─────────────────────────────────────────
#  SELF PING — keeps Render awake
# ─────────────────────────────────────────
def self_ping():
    time.sleep(30)
    while True:
        try:
            own_url = os.environ.get("RENDER_EXTERNAL_URL","http://localhost:5000")
            import requests as req
            req.get(f"{own_url}/ping", timeout=10)
            print(f"[PING] {datetime.datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[PING] Error: {e}")
        time.sleep(600)

threading.Thread(target=self_ping, daemon=True).start()

# ─────────────────────────────────────────
#  HELPER
# ─────────────────────────────────────────
def get_or_create_risk_manager(config: dict) -> RiskManager:
    global risk_manager
    if risk_manager is None:
        risk_manager = RiskManager(config)
    return risk_manager

# ─────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────

@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "time": str(datetime.datetime.now()), "version": "2.0"})

@app.route("/market-status")
def market_status():
    return jsonify(fetch_market_status())

# ── Full signal generation (main endpoint)
@app.route("/generate-signal", methods=["POST"])
def generate_signal_route():
    global last_signal, last_snapshot

    body   = request.get_json(force=True) or {}
    symbol = body.get("symbol", "NIFTY").upper()
    config = {
        "total_capital":  body.get("capital", 50000),
        "risk_per_trade": body.get("risk_pct", 2.0),
        "rr_ratio":       body.get("rr_ratio", 2.0),
        "max_trades_day": body.get("max_trades", 3),
    }

    rm = get_or_create_risk_manager(config)

    # Check if trading is allowed
    can_trade = rm.can_trade()

    # Fetch data
    snapshot = fetch_full_snapshot(symbol)
    last_snapshot = snapshot

    if snapshot.get("errors"):
        return jsonify({"error": "Data fetch failed", "details": snapshot["errors"]}), 503

    # Generate signal
    signal = generate_signal(snapshot)
    last_signal = signal

    # Calculate position size
    position = None
    if signal["action"] != "WAIT" and can_trade["allowed"]:
        position = rm.calculate_position(signal, snapshot.get("option_data", {}))

    # Save signal to DB
    try:
        save_signal(signal)
    except Exception as e:
        print(f"[DB] Signal save error: {e}")

    return jsonify({
        "signal":       signal,
        "position":     position,
        "capital":      rm.get_status(),
        "can_trade":    can_trade,
        "market":       snapshot.get("market_status", {}),
        "indices":      snapshot.get("indices", {}),
        "fii_dii":      snapshot.get("fii_dii", {}),
        "chain":        snapshot.get("option_data", {}).get("chain", []),
        "option_meta":  {k:v for k,v in snapshot.get("option_data",{}).items() if k != "chain"},
    })

# ── Trade taken (user confirmed entry)
@app.route("/trade/enter", methods=["POST"])
def trade_enter():
    global active_monitor, active_trade_id

    body     = request.get_json(force=True) or {}
    position = body.get("position", {})
    signal   = body.get("signal", last_signal or {})

    if not position:
        return jsonify({"error": "No position data"}), 400

    rm = risk_manager
    if rm is None:
        return jsonify({"error": "Risk manager not initialized"}), 400

    # Record in DB
    position["signal_id"]     = signal.get("signal_id","")
    position["capital_before"] = rm.available_capital
    rm.in_trade_capital = position.get("capital_used", 0)

    try:
        trade_id = save_trade(position)
        active_trade_id = trade_id
    except Exception as e:
        print(f"[DB] Trade save error: {e}")
        trade_id = 0

    # Start monitor
    active_monitor = TradeMonitor(position)

    return jsonify({
        "trade_id": trade_id,
        "message":  "Trade recorded. Monitor started.",
        "position": position,
    })

# ── Monitor check (call every 60s)
@app.route("/trade/monitor")
def trade_monitor_route():
    if active_monitor is None:
        return jsonify({"status": "NO_ACTIVE_TRADE"})

    # Get latest chain
    symbol   = active_monitor.trade.get("symbol","NIFTY")
    snapshot = fetch_full_snapshot(symbol)
    chain    = snapshot.get("option_data",{}).get("chain",[])
    sig_score= last_signal.get("composite",0) if last_signal else 0

    status = active_monitor.check(chain, sig_score)
    return jsonify(status)

# ── Trade exit
@app.route("/trade/exit", methods=["POST"])
def trade_exit():
    global active_monitor, active_trade_id

    body      = request.get_json(force=True) or {}
    exit_type = body.get("exit_type","MANUAL")
    pnl       = float(body.get("pnl", 0))

    rm = risk_manager
    if rm is None:
        return jsonify({"error": "No risk manager"}), 400

    rm.record_trade_result(pnl)

    # Save exit to DB
    if active_trade_id:
        try:
            close_trade(active_trade_id, {
                "ce_exit_price": body.get("ce_exit_price"),
                "pe_exit_price": body.get("pe_exit_price"),
                "pnl":           pnl,
                "exit_type":     exit_type,
                "capital_after": rm.available_capital,
                "notes":         body.get("notes",""),
            })
        except Exception as e:
            print(f"[DB] Exit save error: {e}")

    active_monitor  = None
    active_trade_id = None

    return jsonify({
        "message":  f"Trade closed. P&L: ₹{pnl:.0f}",
        "capital":  rm.get_status(),
        "next_can_trade": rm.can_trade(),
    })

# ── Capital status
@app.route("/capital")
def capital_status():
    if risk_manager is None:
        return jsonify({"error": "Not initialized"})
    return jsonify(risk_manager.get_status())

# ── Reset capital (new session)
@app.route("/capital/reset", methods=["POST"])
def capital_reset():
    global risk_manager
    body = request.get_json(force=True) or {}
    risk_manager = RiskManager({
        "total_capital":  body.get("capital", 50000),
        "risk_per_trade": body.get("risk_pct", 2.0),
        "rr_ratio":       body.get("rr_ratio", 2.0),
        "max_trades_day": body.get("max_trades", 3),
    })
    return jsonify({"message": "Capital reset", "capital": risk_manager.get_status()})

# ── Trade history
@app.route("/history")
def history():
    try:
        trades = get_trade_history(50)
        perf   = get_performance_summary()
        return jsonify({"trades": trades, "performance": perf})
    except Exception as e:
        return jsonify({"error": str(e), "trades": [], "performance": {}})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
