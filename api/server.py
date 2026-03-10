"""
QUANTRA BEAST v4.0 — Modular Production Server
Zerodha Kite Connect — PRIMARY & ONLY broker
8 signal engines + capital management + DB

Architecture:
  api/server.py          ← this file (thin Flask router)
  core/kite_client.py    ← Kite auth / WS / REST
  core/data_fetcher.py   ← NSE + Kite data pipeline
  core/cache.py          ← in-memory TTL cache
  engines/               ← 8 individual signal engines
  signals/fusion_engine.py ← weighted signal combiner
  signals/strike_selector.py ← strike + expiry selection
  capital/risk_manager.py ← position sizing + risk rules
  monitoring/trade_monitor.py ← open trade P&L + SL logic
  database/db.py         ← SQLite trade history
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import time, datetime, threading, os, uuid, json

app  = Flask(__name__)
CORS(app)

# ── Env config ───────────────────────────────────────────
DB_PATH  = os.environ.get("DB_PATH", "/tmp/quantra.db")
OWN_URL  = os.environ.get("RENDER_EXTERNAL_URL", "https://quantra-beast.onrender.com")

# ── Module imports ───────────────────────────────────────
from core.cache         import _cget, _cset
from core.kite_client   import (
    KITE_API_KEY, KITE_INDEX_TOKENS, TOKEN_TO_NAME,
    LOT_SIZES, MARGINS,
    kite_login_url, kite_exchange_token, kite_invalidate_token,
    kite_quotes, kite_ltp_rest, get_index_ltp, fetch_live_ltp,
    kite_place_order, kite_modify_order, kite_cancel_order,
    kite_get_orders, kite_get_positions, kite_get_margins,
    build_order_params, start_kite_ws, kite_ws_subscribe,
    _option_sym,
)
from core.data_fetcher  import (
    fetch_indices, fetch_chain, fetch_fii_dii, fetch_market_status,
)
from signals.fusion_engine  import generate_signal as fusion_generate
from signals.strike_selector import select_strike
from capital.risk_manager   import RiskManager
from monitoring.trade_monitor import TradeMonitor
from database.db import (
    init_db, save_signal, save_trade, close_trade, get_trade_history,
    get_performance_summary,
)

# ── Kite state (single shared dict) ─────────────────────
kite = {
    "access_token":    os.environ.get("KITE_ACCESS_TOKEN", ""),
    "connected":       False,
    "ws_connected":    False,
    "last_token_time": None,
    "ltp_cache":       {},
    "ltp_sym":         {},
    "subscribed":      set(),
    "ws_obj":          None,
    "error":           None,
    "profile":         {},
    "last_tick_ts":    None,
}

# ── Global app state ─────────────────────────────────────
risk_mgr:       RiskManager | None = None
active_monitor: dict | None        = None   # None = not in trade
last_signal:    dict | None        = None
last_auto_signal_time              = None
_ltp_poll_running                  = False

WEIGHTS = {
    "trend": 0.16, "options": 0.18, "gamma": 0.11, "volatility": 0.09,
    "regime": 0.07, "sentiment": 0.14, "flow": 0.15, "news": 0.10,
}

# ── Init DB ──────────────────────────────────────────────
init_db()

# ════════════════════════════════════════════════════════════════════════════════
#  BACKGROUND THREADS
# ════════════════════════════════════════════════════════════════════════════════

def _start_kite_ws_thread():
    threading.Thread(target=start_kite_ws, args=(kite,), daemon=True).start()

def start_ltp_polling():
    global _ltp_poll_running
    if _ltp_poll_running: return
    _ltp_poll_running = True
    INDEX_SYMS = ["NSE:NIFTY 50", "NSE:NIFTY BANK",
                  "NSE:INDIA VIX", "NSE:NIFTY FIN SERVICE"]
    def poll():
        while True:
            try:
                if kite["access_token"]:
                    kite_ltp_rest(INDEX_SYMS, kite)
                    if active_monitor:
                        pos  = active_monitor.get("trade", {})
                        live = fetch_live_ltp(
                            pos.get("symbol", "NIFTY"),
                            pos.get("ce_strike"),
                            pos.get("pe_strike"),
                            pos.get("expiry", ""),
                            kite,
                        )
                        if live["ce_ltp"]   > 0: active_monitor["live_ce_ltp"]  = live["ce_ltp"]
                        if live["pe_ltp"]   > 0: active_monitor["live_pe_ltp"]  = live["pe_ltp"]
                        if live["spot_ltp"] > 0: active_monitor["live_spot"]    = live["spot_ltp"]
            except Exception as e:
                print(f"[LTP POLL] {e}")
            time.sleep(5)
    threading.Thread(target=poll, daemon=True).start()

def auto_signal_loop():
    global last_auto_signal_time
    while True:
        try:
            now  = datetime.datetime.now()
            mins = now.hour * 60 + now.minute
            if (now.weekday() < 5 and 555 <= mins <= 930
                    and kite["connected"] and risk_mgr and not active_monitor):
                if not last_auto_signal_time or (time.time() - last_auto_signal_time) >= 180:
                    print(f"[AUTO] Signal at {now.strftime('%H:%M')}")
                    try:
                        cfg = risk_mgr._last_cfg or {
                            "symbol":    "NIFTY",
                            "capital":   risk_mgr.total,
                            "risk_pct":  risk_mgr.risk_pct,
                            "rr_ratio":  risk_mgr.rr,
                            "max_trades": risk_mgr.max_trades,
                        }
                        _do_generate_signal(cfg)
                        last_auto_signal_time = time.time()
                    except Exception as e:
                        print(f"[AUTO SIGNAL] {e}")
        except Exception as e:
            print(f"[AUTO LOOP] {e}")
        time.sleep(30)

threading.Thread(target=auto_signal_loop, daemon=True).start()

# ════════════════════════════════════════════════════════════════════════════════
#  SIGNAL GENERATION
# ════════════════════════════════════════════════════════════════════════════════

def _fetch_news_score(symbol: str) -> tuple[float, list]:
    """Lightweight news fetch — returns (score, headlines)."""
    try:
        from core.data_fetcher import nse_get
        data = nse_get("https://www.nseindia.com/api/market-news")
        if data:
            headlines = [item.get("description", "") for item in
                         (data if isinstance(data, list)
                          else data.get("data", []))[:5]]
            # Simple sentiment: count positive/negative keywords
            pos_kw = ["rally", "surge", "gain", "bull", "positive", "up", "high", "record"]
            neg_kw = ["fall", "crash", "drop", "bear", "negative", "down", "low", "sell"]
            score = 0
            for h in headlines:
                hl = h.lower()
                score += sum(10 for w in pos_kw if w in hl)
                score -= sum(10 for w in neg_kw if w in hl)
            return max(-50, min(50, score)), headlines
    except:
        pass
    return 0, []

def _do_generate_signal(cfg: dict) -> dict:
    global last_signal
    symbol = cfg.get("symbol", "NIFTY")

    indices     = fetch_indices(kite)
    option_data = fetch_chain(symbol, kite)
    fii_dii     = fetch_fii_dii()
    news_score, headlines = _fetch_news_score(symbol)

    if not option_data.get("chain"):
        print(f"[SIGNAL] Warning: empty chain for {symbol}, proceeding with degraded data")

    snapshot = {
        "symbol":      symbol,
        "indices":     indices,
        "option_data": option_data,
        "fii_dii":     fii_dii,
    }

    signal = fusion_generate(snapshot, weights=WEIGHTS, news_score=news_score)
    signal["headlines"] = headlines

    # Strike selection
    if signal["action"] != "WAIT" and option_data.get("chain"):
        vix = indices.get("INDIA VIX", {}).get("last", 15)
        strikes = select_strike(signal, option_data, signal["action"], vix=vix)
        signal["ce_strike"] = strikes.get("ce_strike")
        signal["pe_strike"] = strikes.get("pe_strike")

    # Capital check
    if risk_mgr:
        can = risk_mgr.can_trade()
        if not can["allowed"] and signal["action"] != "WAIT":
            signal["action"]     = "WAIT"
            signal["signal_cls"] = "wait"
            signal["signal_sub"] = f"Capital block: {can['reason']}"

    last_signal = signal
    try:
        save_signal(signal)
    except Exception as e:
        print(f"[DB SAVE SIGNAL] {e}")

    print(f"[SIGNAL] {symbol} → {signal['action']} | score={signal['composite']} | conf={signal['confidence']}")
    return signal


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES — KITE AUTH
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "ts": time.time(), "version": "4.0-modular"})

@app.route("/kite/status")
def kite_status():
    return jsonify({
        "connected":         kite["connected"],
        "ws_connected":      kite["ws_connected"],
        "profile":           kite["profile"],
        "subscribed_tokens": len(kite["subscribed"]),
        "subscribed":        len(kite["subscribed"]),
        "last_token_time":   kite["last_token_time"],
        "last_tick":         kite["last_tick_ts"],
        "error":             kite["error"],
    })

@app.route("/kite/login-url")
def kite_login():
    url = kite_login_url()
    if not url:
        return jsonify({"error": "KITE_API_KEY not set"}), 400
    return jsonify({"url": url})

@app.route("/kite/callback")
def kite_callback():
    rt = request.args.get("request_token")
    if not rt:
        return "<h2>❌ No request_token</h2>", 400

    def on_success():
        _start_kite_ws_thread()
        start_ltp_polling()

    ok, val = kite_exchange_token(rt, kite, on_success_callback=on_success)
    if ok:
        return f"<h2>✅ Connected as {kite['profile'].get('user_name','')}</h2>"
    return f"<h2>❌ Login failed: {val}</h2>", 400

@app.route("/kite/set-token", methods=["POST"])
def kite_set_token():
    data = request.get_json(force=True) or {}
    token = data.get("access_token", "")
    if not token:
        return jsonify({"error": "access_token required"}), 400
    kite["access_token"]    = token
    kite["connected"]       = True
    kite["last_token_time"] = datetime.datetime.now().isoformat()
    kite["error"]           = None
    _start_kite_ws_thread()
    start_ltp_polling()
    return jsonify({"status": "ok", "token_set": True})

@app.route("/kite/logout", methods=["POST"])
def kite_logout():
    kite_invalidate_token(kite)
    return jsonify({"status": "logged_out"})


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES — KITE DATA
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/kite/ltp")
def kite_ltp_route():
    symbols = request.args.getlist("symbols")

    # No-param mode: return full index snapshot for 2s RT polling
    if not symbols:
        idx_map = {
            "NIFTY 50":          "NSE:NIFTY 50",
            "NIFTY BANK":        "NSE:NIFTY BANK",
            "INDIA VIX":         "NSE:INDIA VIX",
            "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
        }
        snap = {}
        for name, sym in idx_map.items():
            c = kite["ltp_sym"].get(sym)
            if c: snap[sym] = c["ltp"]

        # FII/DII from cache
        fii = _cget("fii", ttl=300) or {}

        # Option meta from last chain
        opt_meta = {}
        for sym in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
            c = _cget(f"oc_{sym}", ttl=60)
            if c:
                opt_meta[sym] = {
                    "atm":      c.get("atm_strike"),
                    "pcr":      c.get("pcr"),
                    "max_pain": c.get("max_pain"),
                    "expiry":   c.get("expiry"),
                }
        return jsonify({
            "indices":     snap,
            "fii_dii":     fii,
            "option_meta": opt_meta,
            "ws_active":   kite["ws_connected"],
            "last_tick":   kite["last_tick_ts"],
            "ts":          time.time(),
        })

    # Specific symbols requested
    if not kite["access_token"]:
        return jsonify({"error": "Kite not connected"}), 401
    prices = kite_ltp_rest(symbols, kite)
    return jsonify(prices)

@app.route("/kite/quote")
def kite_quote():
    symbols = request.args.getlist("symbols")
    if not symbols:
        return jsonify({"error": "symbols required"}), 400
    if not kite["access_token"]:
        return jsonify({"error": "Kite not connected"}), 401
    return jsonify(kite_quotes(symbols, kite))

@app.route("/kite/orders")
def get_orders():
    return jsonify(kite_get_orders(kite))

@app.route("/kite/positions")
def get_positions():
    return jsonify(kite_get_positions(kite))

@app.route("/kite/margins")
def get_margins():
    return jsonify(kite_get_margins(kite))

@app.route("/kite/place-order", methods=["POST"])
def place_order():
    data = request.get_json(force=True) or {}
    if not kite["access_token"]:
        return jsonify({"error": "Kite not connected"}), 401
    return jsonify(kite_place_order(data, kite))

@app.route("/kite/modify-order/<order_id>", methods=["PUT"])
def modify_order(order_id):
    data = request.get_json(force=True) or {}
    return jsonify(kite_modify_order(order_id, data, kite))

@app.route("/kite/cancel-order/<order_id>", methods=["DELETE"])
def cancel_order(order_id):
    variety = request.args.get("variety", "regular")
    return jsonify(kite_cancel_order(order_id, kite, variety=variety))


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES — SIGNAL
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/generate-signal", methods=["POST"])
def generate_signal_route():
    global risk_mgr
    data = request.get_json(force=True) or {}

    # Init / update risk manager
    if not risk_mgr or data:
        risk_mgr = RiskManager(data)

    try:
        signal = _do_generate_signal(data)
        return jsonify(signal)
    except Exception as e:
        print(f"[GENERATE SIGNAL] {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/latest-signal")
def latest_signal():
    if not last_signal:
        return jsonify({"error": "No signal yet"}), 404
    return jsonify(last_signal)


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES — TRADE
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/trade/enter", methods=["POST"])
def trade_enter():
    global active_monitor, risk_mgr
    data = request.get_json(force=True) or {}

    if active_monitor:
        return jsonify({"error": "Already in trade — exit first"}), 400
    if not risk_mgr:
        return jsonify({"error": "No capital config — call /generate-signal first"}), 400

    can = risk_mgr.can_trade()
    if not can["allowed"]:
        return jsonify({"error": can["reason"]}), 400

    # Use signal from request or last_signal
    trade_data = data.get("trade") or last_signal
    if not trade_data:
        return jsonify({"error": "No trade data"}), 400

    # Position sizing
    option_data = _cget(f"oc_{trade_data.get('symbol', 'NIFTY')}", ttl=60) or {}
    position    = risk_mgr.calculate_position(trade_data, option_data)

    # Merge strikes from request if provided
    for f in ["ce_strike", "pe_strike", "ce_price", "pe_price", "lots", "expiry"]:
        if data.get(f): position[f] = data[f]

    position["signal_id"]      = trade_data.get("signal_id", str(uuid.uuid4())[:8])
    position["capital_before"] = risk_mgr.available_capital
    risk_mgr.in_trade_capital  = position["capital_used"]

    # Place Kite orders if requested
    orders_placed = []
    if data.get("place_orders") and kite["access_token"]:
        for params in build_order_params(position):
            result = kite_place_order(dict(params), kite)
            orders_placed.append(result)

    # Save to DB and start monitoring
    trade_id = save_trade(position)
    monitor  = TradeMonitor(position)
    active_monitor = {
        "trade":      position,
        "trade_id":   trade_id,
        "monitor":    monitor,
        "start_time": time.time(),
        "live_ce_ltp": position.get("ce_price", 0),
        "live_pe_ltp": position.get("pe_price", 0),
        "live_spot":   position.get("spot", 0),
    }

    return jsonify({
        "status":        "entered",
        "trade":         position,
        "trade_id":      trade_id,
        "orders_placed": orders_placed,
    })

@app.route("/trade/monitor")
def trade_monitor_route():
    if not active_monitor:
        return jsonify({"in_trade": False})

    trade_data   = active_monitor["trade"]
    chain        = _cget(f"oc_{trade_data.get('symbol', 'NIFTY')}", ttl=60)
    current_chain = (chain or {}).get("chain", [])

    live_ltp = {
        "ce_ltp":   active_monitor.get("live_ce_ltp", 0),
        "pe_ltp":   active_monitor.get("live_pe_ltp", 0),
        "spot_ltp": active_monitor.get("live_spot", 0),
    }

    score = last_signal.get("composite", 0) if last_signal else 0
    result = active_monitor["monitor"].check(current_chain, score, live_ltp=live_ltp)

    return jsonify({
        "in_trade": True,
        "trade":    trade_data,
        "monitor":  result,
    })

@app.route("/trade/exit", methods=["POST"])
def trade_exit():
    global active_monitor
    if not active_monitor:
        return jsonify({"error": "No active trade"}), 400

    data       = request.get_json(force=True) or {}
    trade_data = active_monitor["trade"]
    live_ltp   = {
        "ce_ltp":   active_monitor.get("live_ce_ltp", 0),
        "pe_ltp":   active_monitor.get("live_pe_ltp", 0),
        "spot_ltp": active_monitor.get("live_spot", 0),
    }
    chain        = _cget(f"oc_{trade_data.get('symbol', 'NIFTY')}", ttl=60)
    current_chain = (chain or {}).get("chain", [])
    score = last_signal.get("composite", 0) if last_signal else 0

    monitor_result = active_monitor["monitor"].check(
        current_chain, score, live_ltp=live_ltp
    )

    pnl       = data.get("pnl", monitor_result["pnl"])
    exit_type = data.get("exit_type", monitor_result["status"])

    exit_data = {
        "ce_exit_price": live_ltp["ce_ltp"] or data.get("ce_exit_price", 0),
        "pe_exit_price": live_ltp["pe_ltp"] or data.get("pe_exit_price", 0),
        "pnl":           pnl,
        "exit_type":     exit_type,
        "capital_after": (risk_mgr.available_capital + pnl) if risk_mgr else 0,
        "notes":         data.get("notes", ""),
    }

    close_trade(active_monitor["trade_id"], exit_data)
    if risk_mgr:
        risk_mgr.record_trade_result(pnl)

    result = {
        "status":    "exited",
        "pnl":       pnl,
        "exit_type": exit_type,
        "capital":   risk_mgr.get_status() if risk_mgr else {},
    }
    active_monitor = None
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES — CAPITAL
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/capital")
def capital_status():
    if not risk_mgr:
        return jsonify({"error": "No capital config yet"}), 404
    return jsonify(risk_mgr.get_status())

@app.route("/capital/reset", methods=["POST"])
def capital_reset():
    global risk_mgr
    data     = request.get_json(force=True) or {}
    risk_mgr = RiskManager(data)
    return jsonify({"status": "reset", "capital": risk_mgr.get_status()})


# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES — HISTORY & MARKET DATA
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/history")
def history():
    limit = int(request.args.get("limit", 50))
    return jsonify({
        "trades":  get_trade_history(limit),
        "summary": get_performance_summary(),
    })

@app.route("/market-status")
def market_status_route():
    return jsonify(fetch_market_status())

@app.route("/gift-nifty")
def gift_nifty():
    c = _cget("gift_nifty", ttl=60)
    if c: return jsonify(c)
    try:
        from core.data_fetcher import nse_get
        data = nse_get("https://www.nseindia.com/api/gift-nifty")
        if data:
            _cset("gift_nifty", data)
            return jsonify(data)
    except Exception as e:
        print(f"[GIFT NIFTY] {e}")
    return jsonify({"error": "unavailable"})

@app.route("/global-macro")
def global_macro():
    c = _cget("macro", ttl=120)
    if c: return jsonify(c)
    try:
        from core.data_fetcher import nse_get
        data = nse_get("https://www.nseindia.com/api/market-data-pre-open?key=NIFTY")
        if data:
            _cset("macro", data)
            return jsonify(data)
    except Exception as e:
        print(f"[MACRO] {e}")
    return jsonify({"error": "unavailable"})

@app.route("/market-news")
def market_news():
    c = _cget("news", ttl=180)
    if c: return jsonify(c)
    try:
        from core.data_fetcher import nse_get
        data = nse_get("https://www.nseindia.com/api/market-news") or []
        result = {"headlines": data[:10] if isinstance(data, list) else
                  data.get("data", [])[:10], "ts": time.time()}
        _cset("news", result)
        return jsonify(result)
    except Exception as e:
        print(f"[NEWS] {e}")
    return jsonify({"headlines": [], "error": "unavailable"})


# ════════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Resume WS if token already in env
    if kite["access_token"]:
        kite["connected"] = True
        _start_kite_ws_thread()
        start_ltp_polling()
        print("[SERVER] Resumed Kite session from env token")

    port = int(os.environ.get("PORT", 5000))
    print(f"[SERVER] QUANTRA BEAST v4.0 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
