"""
QUANTRA BEAST v3.0 — Production Server
Zerodha Kite Connect — PRIMARY & ONLY broker
Live WebSocket streaming + REST LTP + Order Placement
8 signal engines + capital management + DB
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, json, time, datetime, threading, os, uuid, sqlite3, hashlib, struct, ssl

app  = Flask(__name__)
CORS(app)

# ─── ENV CONFIG ───────────────────────────────────────────────────────────────
SCRAPER_KEY = os.environ.get("SCRAPER_API_KEY", "620fb119d0569d21a4303effdd303228")
DB_PATH     = os.environ.get("DB_PATH", "/tmp/quantra.db")
OWN_URL     = os.environ.get("RENDER_EXTERNAL_URL", "https://quantra-beast.onrender.com")

# ─── KITE CREDENTIALS ─────────────────────────────────────────────────────────
KITE_API_KEY    = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")
KITE_REDIRECT   = os.environ.get("KITE_REDIRECT", f"{OWN_URL}/kite/callback")

# ─── KITE STATE ───────────────────────────────────────────────────────────────
kite = {
    "access_token":    os.environ.get("KITE_ACCESS_TOKEN", ""),
    "connected":       False,
    "ws_connected":    False,
    "last_token_time": None,
    "ltp_cache":       {},      # instrument_token (int) -> {ltp, ts}
    "ltp_sym":         {},      # "NSE:NIFTY 50"  -> {ltp, ts}
    "subscribed":      set(),
    "ws_obj":          None,
    "error":           None,
    "profile":         {},
}

# ─── KITE INSTRUMENT TOKENS (fixed by Zerodha) ───────────────────────────────
KITE_INDEX_TOKENS = {
    "NIFTY 50":          256265,
    "NIFTY BANK":        260105,
    "INDIA VIX":         264969,
    "NIFTY FIN SERVICE": 257801,
    "SENSEX":            265,
}
TOKEN_TO_NAME = {v: k for k, v in KITE_INDEX_TOKENS.items()}

LOT_SIZES  = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65}
MARGINS    = {"NIFTY": 6500, "BANKNIFTY": 11000, "FINNIFTY": 6000}
SL_PCT     = 0.40

# ─── SIMPLE CACHE ─────────────────────────────────────────────────────────────
_cache = {}
def _cget(k, ttl=60):
    e = _cache.get(k)
    if not e or time.time() - e["ts"] > ttl: return None
    return e["d"]
def _cset(k, d): _cache[k] = {"ts": time.time(), "d": d}

# ─── NSE SCRAPER (OI data only) ───────────────────────────────────────────────
def nse_get(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get("http://api.scraperapi.com", params={
                "api_key": SCRAPER_KEY, "url": url,
                "country_code": "in", "render": "false", "keep_headers": "true"
            }, timeout=30, headers={"Accept": "application/json"})
            t = r.text.strip()
            if t.startswith("<") or "<!doctype" in t.lower():
                time.sleep(2*(i+1)); continue
            return r.json()
        except Exception as e:
            print(f"[NSE] Error {i+1}: {e}"); time.sleep(2)
    return None

# ═══════════════════════════════════════════════════════════════════════════════
#  KITE AUTH
# ═══════════════════════════════════════════════════════════════════════════════

def kite_login_url():
    if not KITE_API_KEY: return None
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={KITE_API_KEY}"

def kite_exchange_token(request_token):
    try:
        checksum = hashlib.sha256(
            f"{KITE_API_KEY}{request_token}{KITE_API_SECRET}".encode()
        ).hexdigest()
        print(f"[KITE] Exchanging request_token...")
        r = requests.post(
            "https://api.kite.trade/session/token",
            data={"api_key": KITE_API_KEY, "request_token": request_token, "checksum": checksum},
            headers={"X-Kite-Version": "3"},
            timeout=15
        )
        print(f"[KITE] Token response: {r.status_code} {r.text[:300]}")
        d = r.json()
        if d.get("status") == "success":
            data = d.get("data", {})
            kite["access_token"]    = data.get("access_token", "")
            kite["connected"]       = True
            kite["last_token_time"] = datetime.datetime.now().isoformat()
            kite["error"]           = None
            kite["profile"]         = {
                "user_name":  data.get("user_name", ""),
                "user_id":    data.get("user_id", ""),
                "email":      data.get("email", ""),
                "broker":     data.get("broker", "ZERODHA"),
                "login_time": data.get("login_time", ""),
            }
            print(f"[KITE] Logged in as {kite['profile'].get('user_name')}")
            threading.Thread(target=start_kite_ws, daemon=True).start()
            start_ltp_polling()
            return True, kite["access_token"]
        err = d.get("message", str(d))
        kite["error"] = err
        return False, err
    except Exception as e:
        kite["error"] = str(e); return False, str(e)

def kite_invalidate_token():
    if not kite["access_token"]: return
    try:
        requests.delete("https://api.kite.trade/session/token",
            data={"api_key": KITE_API_KEY, "access_token": kite["access_token"]},
            headers=_kh(), timeout=10)
    except: pass
    kite["access_token"] = ""; kite["connected"] = False
    kite["ws_connected"] = False; kite["error"] = None

def _kh():
    return {"X-Kite-Version": "3",
            "Authorization":  f"token {KITE_API_KEY}:{kite['access_token']}"}

# ═══════════════════════════════════════════════════════════════════════════════
#  KITE WEBSOCKET  (KiteTicker binary protocol)
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_kite_binary(data: bytes):
    try:
        if len(data) < 2: return
        num = struct.unpack(">H", data[:2])[0]
        offset = 2
        for _ in range(num):
            if offset + 2 > len(data): break
            pkt_len = struct.unpack(">H", data[offset:offset+2])[0]
            offset += 2
            pkt = data[offset:offset+pkt_len]; offset += pkt_len
            if len(pkt) < 8: continue
            token = struct.unpack(">I", pkt[0:4])[0]
            ltp   = struct.unpack(">I", pkt[4:8])[0] / 100.0
            entry = {"ltp": ltp, "ts": time.time()}
            kite["ltp_cache"][token] = entry
            sym = TOKEN_TO_NAME.get(token)
            if sym: kite["ltp_sym"][f"NSE:{sym}"] = entry
    except Exception as e:
        print(f"[KITE WS PARSE] {e}")

def start_kite_ws():
    if not kite["access_token"] or not KITE_API_KEY: return
    try:
        import websocket as ws_lib
        WS_URL = (f"wss://ws.kite.trade?api_key={KITE_API_KEY}"
                  f"&access_token={kite['access_token']}")
        ALL_TOKENS = list(KITE_INDEX_TOKENS.values())

        def _subscribe(ws):
            ws.send(json.dumps({"a": "subscribe", "v": ALL_TOKENS}))
            ws.send(json.dumps({"a": "mode",      "v": ["ltp", ALL_TOKENS]}))
            kite["subscribed"].update(ALL_TOKENS)
            print(f"[KITE WS] Subscribed {len(ALL_TOKENS)} index tokens")

        def on_open(ws):
            kite["ws_connected"] = True; kite["ws_obj"] = ws
            print("[KITE WS] Connected"); _subscribe(ws)

        def on_message(ws, message):
            if isinstance(message, bytes): _parse_kite_binary(message)
            else:
                try:
                    d = json.loads(message)
                    if d.get("type") == "order":
                        print(f"[KITE ORDER UPDATE] {d.get('data',{})}")
                except: pass

        def on_close(ws, *args):
            kite["ws_connected"] = False; kite["ws_obj"] = None
            print("[KITE WS] Disconnected — retrying in 15s")
            time.sleep(15)
            if kite["access_token"]:
                threading.Thread(target=start_kite_ws, daemon=True).start()

        def on_error(ws, error): print(f"[KITE WS] Error: {error}")

        ws_lib.WebSocketApp(WS_URL,
            on_open=on_open, on_message=on_message,
            on_close=on_close, on_error=on_error
        ).run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

    except Exception as e:
        print(f"[KITE WS] Start error: {e}")

def kite_ws_subscribe(tokens: list):
    new = [t for t in tokens if t not in kite["subscribed"]]
    if not new: return
    try:
        ws = kite.get("ws_obj")
        if ws:
            ws.send(json.dumps({"a": "subscribe", "v": new}))
            ws.send(json.dumps({"a": "mode",      "v": ["full", new]}))
            kite["subscribed"].update(new)
    except Exception as e:
        print(f"[KITE WS SUBSCRIBE] {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  KITE REST  — Quotes / LTP
# ═══════════════════════════════════════════════════════════════════════════════

def kite_quotes(exchange_symbols: list) -> dict:
    if not kite["access_token"]: return {}
    try:
        qs = "&".join(f"i={s}" for s in exchange_symbols)
        r  = requests.get(f"https://api.kite.trade/quote?{qs}", headers=_kh(), timeout=10)
        d  = r.json(); result = {}
        if d.get("status") == "success":
            for key, val in d.get("data", {}).items():
                ohlc = val.get("ohlc", {}); ltp = val.get("last_price", 0)
                result[key] = {"ltp": ltp, "open": ohlc.get("open",0), "high": ohlc.get("high",0),
                               "low": ohlc.get("low",0), "close": ohlc.get("close",0),
                               "volume": val.get("volume",0), "oi": val.get("oi",0), "ts": time.time()}
                tok = val.get("instrument_token")
                if tok: kite["ltp_cache"][tok] = {"ltp": ltp, "ts": time.time()}
                kite["ltp_sym"][key] = {"ltp": ltp, "ts": time.time()}
        return result
    except Exception as e:
        print(f"[KITE QUOTES] {e}"); return {}

def kite_ltp_rest(exchange_symbols: list) -> dict:
    if not kite["access_token"]: return {}
    try:
        qs = "&".join(f"i={s}" for s in exchange_symbols)
        r  = requests.get(f"https://api.kite.trade/quote/ltp?{qs}", headers=_kh(), timeout=8)
        d  = r.json(); result = {}
        if d.get("status") == "success":
            for key, val in d.get("data", {}).items():
                ltp = val.get("last_price", 0)
                result[key] = ltp
                kite["ltp_sym"][key] = {"ltp": ltp, "ts": time.time()}
                tok = val.get("instrument_token")
                if tok: kite["ltp_cache"][tok] = {"ltp": ltp, "ts": time.time()}
        return result
    except Exception as e:
        print(f"[KITE LTP] {e}"); return {}

def _option_sym(symbol, strike, otype, expiry_str):
    try:
        exp = datetime.datetime.strptime(expiry_str, "%d-%b-%Y")
        return f"NFO:{symbol}{exp.strftime('%y%b').upper()}{int(strike)}{otype}"
    except: return None

def get_index_ltp(name: str) -> float:
    key = f"NSE:{name}"
    c = kite["ltp_sym"].get(key)
    if c and time.time() - c["ts"] < 5: return c["ltp"]
    tok = KITE_INDEX_TOKENS.get(name)
    if tok:
        c = kite["ltp_cache"].get(tok)
        if c and time.time() - c["ts"] < 5: return c["ltp"]
    if kite["access_token"]:
        r = kite_ltp_rest([key])
        return r.get(key, 0)
    return 0

def get_option_ltp(symbol, strike, otype, expiry_str) -> float:
    if not kite["access_token"]: return 0
    es = _option_sym(symbol, strike, otype, expiry_str)
    if not es: return 0
    r = kite_ltp_rest([es])
    return r.get(es, 0)

def fetch_live_ltp(symbol, strike_ce, strike_pe, expiry):
    idx_map = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE"}
    spot   = get_index_ltp(idx_map.get(symbol, "NIFTY 50"))
    ce_ltp = get_option_ltp(symbol, strike_ce, "CE", expiry) if strike_ce else 0
    pe_ltp = get_option_ltp(symbol, strike_pe, "PE", expiry) if strike_pe else 0
    src    = "kite_ws" if kite["ws_connected"] else "kite_rest"
    return {"spot_ltp": spot, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp, "source": src}

# ═══════════════════════════════════════════════════════════════════════════════
#  KITE ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def kite_place_order(params: dict) -> dict:
    if not kite["access_token"]: return {"error": "Kite not connected"}
    try:
        variety = params.pop("variety", "regular")
        r = requests.post(f"https://api.kite.trade/orders/{variety}",
                          data=params, headers=_kh(), timeout=15)
        d = r.json()
        if d.get("status") == "success":
            oid = d.get("data", {}).get("order_id", "")
            print(f"[KITE ORDER] Placed {oid} {params.get('tradingsymbol')} {params.get('transaction_type')}")
            return {"order_id": oid, "status": "success"}
        err = d.get("message", str(d))
        print(f"[KITE ORDER] Failed: {err}")
        return {"error": err}
    except Exception as e:
        return {"error": str(e)}

def kite_modify_order(order_id: str, params: dict) -> dict:
    if not kite["access_token"]: return {"error": "Not connected"}
    try:
        variety = params.pop("variety", "regular")
        r = requests.put(f"https://api.kite.trade/orders/{variety}/{order_id}",
                         data=params, headers=_kh(), timeout=10)
        d = r.json()
        return {"order_id": order_id, "status": "success"} if d.get("status")=="success" else {"error": d.get("message")}
    except Exception as e: return {"error": str(e)}

def kite_cancel_order(order_id: str, variety="regular") -> dict:
    if not kite["access_token"]: return {"error": "Not connected"}
    try:
        r = requests.delete(f"https://api.kite.trade/orders/{variety}/{order_id}",
                            headers=_kh(), timeout=10)
        d = r.json()
        return {"status": "cancelled"} if d.get("status")=="success" else {"error": d.get("message")}
    except Exception as e: return {"error": str(e)}

def kite_get_orders() -> list:
    if not kite["access_token"]: return []
    try:
        r = requests.get("https://api.kite.trade/orders", headers=_kh(), timeout=10)
        d = r.json()
        return d.get("data", []) if d.get("status")=="success" else []
    except: return []

def kite_get_positions() -> dict:
    if not kite["access_token"]: return {}
    try:
        r = requests.get("https://api.kite.trade/portfolio/positions", headers=_kh(), timeout=10)
        d = r.json()
        return d.get("data", {}) if d.get("status")=="success" else {}
    except: return {}

def kite_get_margins() -> dict:
    if not kite["access_token"]: return {}
    try:
        r = requests.get("https://api.kite.trade/user/margins", headers=_kh(), timeout=10)
        d = r.json()
        return d.get("data", {}) if d.get("status")=="success" else {}
    except: return {}

def build_order_params(pos: dict) -> list:
    """Build Kite order dicts from position — returns list of order param dicts"""
    action = pos.get("action", "BUY CE")
    sym    = pos.get("symbol", "NIFTY")
    exp    = pos.get("expiry", "")
    lots   = pos.get("lots", 1)
    qty    = lots * LOT_SIZES.get(sym, 75)
    orders = []
    if "CE" in action and pos.get("ce_strike"):
        es = _option_sym(sym, pos["ce_strike"], "CE", exp)
        if es:
            orders.append({"tradingsymbol": es.replace("NFO:", ""), "exchange": "NFO",
                           "transaction_type": "BUY", "order_type": "MARKET",
                           "quantity": qty, "product": "MIS", "validity": "DAY",
                           "variety": "regular", "tag": "QB_CE"})
    if "PE" in action and pos.get("pe_strike"):
        es = _option_sym(sym, pos["pe_strike"], "PE", exp)
        if es:
            orders.append({"tradingsymbol": es.replace("NFO:", ""), "exchange": "NFO",
                           "transaction_type": "BUY", "order_type": "MARKET",
                           "quantity": qty, "product": "MIS", "validity": "DAY",
                           "variety": "regular", "tag": "QB_PE"})
    return orders

# ═══════════════════════════════════════════════════════════════════════════════
#  LTP POLLING THREAD  (REST poll every 5s — supplements WS)
# ═══════════════════════════════════════════════════════════════════════════════

_ltp_poll_running = False
def start_ltp_polling():
    global _ltp_poll_running
    if _ltp_poll_running: return
    _ltp_poll_running = True
    INDEX_SYMS = ["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX", "NSE:NIFTY FIN SERVICE"]
    def poll():
        while True:
            try:
                if kite["access_token"]:
                    kite_ltp_rest(INDEX_SYMS)
                    if active_monitor:
                        pos = active_monitor.get("trade", {})
                        live = fetch_live_ltp(pos.get("symbol","NIFTY"),
                                              pos.get("ce_strike"), pos.get("pe_strike"),
                                              pos.get("expiry",""))
                        if live["ce_ltp"]   > 0: active_monitor["live_ce_ltp"]  = live["ce_ltp"]
                        if live["pe_ltp"]   > 0: active_monitor["live_pe_ltp"]  = live["pe_ltp"]
                        if live["spot_ltp"] > 0: active_monitor["live_spot"]    = live["spot_ltp"]
            except Exception as e:
                print(f"[LTP POLL] {e}")
            time.sleep(5)
    threading.Thread(target=poll, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTO SIGNAL THREAD
# ═══════════════════════════════════════════════════════════════════════════════

last_auto_signal_time = None

def auto_signal_loop():
    global last_auto_signal_time
    while True:
        try:
            now  = datetime.datetime.now()
            mins = now.hour * 60 + now.minute
            if now.weekday() < 5 and 555 <= mins <= 930 and kite["connected"] and risk_mgr and not inTrade():
                if not last_auto_signal_time or (time.time() - last_auto_signal_time) >= 180:
                    print(f"[AUTO] Signal at {now.strftime('%H:%M')}")
                    try:
                        _do_generate_signal(risk_mgr._last_cfg or {
                            "symbol": "NIFTY", "capital": risk_mgr.total,
                            "risk_pct": risk_mgr.risk_pct, "rr_ratio": risk_mgr.rr,
                            "max_trades": risk_mgr.max_trades})
                        last_auto_signal_time = time.time()
                    except Exception as e: print(f"[AUTO SIGNAL] {e}")
        except Exception as e: print(f"[AUTO LOOP] {e}")
        time.sleep(30)

def inTrade(): return active_monitor is not None

# ═══════════════════════════════════════════════════════════════════════════════
#  FETCH INDICES
# ═══════════════════════════════════════════════════════════════════════════════

INDEX_EXCHANGE_SYMS = {
    "NIFTY 50":          "NSE:NIFTY 50",
    "NIFTY BANK":        "NSE:NIFTY BANK",
    "INDIA VIX":         "NSE:INDIA VIX",
    "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
}

def fetch_indices():
    c = _cget("idx")
    if c: return c
    if kite["access_token"]:
        try:
            q = kite_quotes(list(INDEX_EXCHANGE_SYMS.values()))
            result = {}
            for name, ks in INDEX_EXCHANGE_SYMS.items():
                if ks in q:
                    v = q[ks]; prev = v.get("close", v["ltp"])
                    chg = round(v["ltp"]-prev, 2); pchg = round(chg/prev*100,2) if prev else 0
                    result[name] = {"last": v["ltp"], "open": v["open"], "high": v["high"],
                                    "low": v["low"], "previousClose": prev,
                                    "change": chg, "pChange": pchg, "source": "kite"}
            if result: _cset("idx", result); return result
        except Exception as e: print(f"[INDICES] {e}")
    # ScraperAPI fallback
    d = nse_get("https://www.nseindia.com/api/allIndices")
    if not d: return {"error": "failed"}
    result = {}
    for item in d.get("data", []):
        n = item.get("indexSymbol","")
        if n in {"NIFTY 50","NIFTY BANK","INDIA VIX","NIFTY FIN SERVICE"}:
            result[n] = {"last": float(item.get("last",0)), "open": float(item.get("open",0)),
                         "high": float(item.get("high",0)), "low": float(item.get("low",0)),
                         "previousClose": float(item.get("previousClose",0)),
                         "change": float(item.get("variation",0)),
                         "pChange": float(item.get("percentChange",0)), "source": "scraperapi"}
    _cset("idx", result); return result

# ─── FETCH OPTION CHAIN ───────────────────────────────────────────────────────
def fetch_chain(symbol="NIFTY"):
    k = f"oc_{symbol}"; c = _cget(k)
    if c: return c
    raw = nse_get(f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}")
    if not raw: return {"error":"failed","chain":[],"spot":0,"pcr":1,"atm_strike":0}
    rec = raw.get("records",{}); spot = float(rec.get("underlyingValue",0))
    exp = rec.get("expiryDates",[]); nearest = exp[0] if exp else None
    step = 100 if symbol=="BANKNIFTY" else 50; atm = round(spot/step)*step
    chain = []; tco = tcp = 0
    for item in rec.get("data",[]):
        if item.get("expiryDate") != nearest: continue
        s = item.get("strikePrice",0); ce = item.get("CE",{}); pe = item.get("PE",{})
        coi = float(ce.get("openInterest",0)); poi = float(pe.get("openInterest",0))
        tco += coi; tcp += poi
        chain.append({"strike":int(s),"distance":int(s-atm),
            "ce_oi":int(coi),"ce_oi_change":int(ce.get("changeinOpenInterest",0)),
            "ce_ltp":float(ce.get("lastPrice",0)),"ce_iv":float(ce.get("impliedVolatility",0)),
            "ce_volume":int(ce.get("totalTradedVolume",0)),
            "pe_oi":int(poi),"pe_oi_change":int(pe.get("changeinOpenInterest",0)),
            "pe_ltp":float(pe.get("lastPrice",0)),"pe_iv":float(pe.get("impliedVolatility",0)),
            "pe_volume":int(pe.get("totalTradedVolume",0))})
    chain.sort(key=lambda x: x["strike"])

    # Enrich ATM ±5 strikes with live Kite LTP
    if kite["access_token"] and nearest:
        try:
            nearby = [row for row in chain if abs(row["strike"]-atm) <= 5*step]
            syms = []
            for row in nearby:
                cs = _option_sym(symbol, row["strike"], "CE", nearest)
                ps = _option_sym(symbol, row["strike"], "PE", nearest)
                if cs: syms.append(cs)
                if ps: syms.append(ps)
            if syms:
                ltps = kite_ltp_rest(syms)
                for row in nearby:
                    cs = _option_sym(symbol, row["strike"], "CE", nearest)
                    ps = _option_sym(symbol, row["strike"], "PE", nearest)
                    if cs and ltps.get(cs,0) > 0: row["ce_ltp"] = ltps[cs]; row["ce_src"] = "kite"
                    if ps and ltps.get(ps,0) > 0: row["pe_ltp"] = ltps[ps]; row["pe_src"] = "kite"
        except Exception as e: print(f"[CHAIN ENRICH] {e}")

    live_spot = get_index_ltp("NIFTY 50" if symbol=="NIFTY" else
                               "NIFTY BANK" if symbol=="BANKNIFTY" else "NIFTY FIN SERVICE")
    if live_spot > 0: spot = live_spot; atm = round(spot/step)*step

    pcr = round(tcp/tco,2) if tco>0 else 1.0
    res = sorted([c for c in chain if c["strike"]>=atm],  key=lambda x: x["ce_oi"], reverse=True)[:3]
    sup = sorted([c for c in chain if c["strike"]<=atm],  key=lambda x: x["pe_oi"], reverse=True)[:3]
    result = {"spot":spot,"atm_strike":atm,"expiry":nearest,"pcr":pcr,
              "max_pain":_calc_max_pain(chain),"chain":chain,
              "total_ce_oi":int(tco),"total_pe_oi":int(tcp),
              "resistance_strikes":res,"support_strikes":sup,
              "iv_skew":_calc_iv_skew(chain,atm),
              "data_source":"nse_oi+kite_ltp" if kite["access_token"] else "scraperapi"}
    _cset(k, result); return result

def _calc_max_pain(chain):
    best=0; mp=float("inf")
    for t in chain:
        p = sum(max(0,c["strike"]-t["strike"])*c["ce_oi"]+max(0,t["strike"]-c["strike"])*c["pe_oi"] for c in chain)
        if p<mp: mp=p; best=t["strike"]
    return best

def _calc_iv_skew(chain, atm):
    a = next((c for c in chain if c["strike"]==atm), None)
    return round(a.get("pe_iv",0)-a.get("ce_iv",0),2) if a else 0

def fetch_fii():
    c = _cget("fii")
    if c: return c
    d = nse_get("https://www.nseindia.com/api/fiidiiTradeReact")
    r = {"fii_net":0,"dii_net":0}
    if d and isinstance(d,list):
        for row in d:
            cat = str(row.get("category","")).upper()
            try:
                net = float(str(row.get("netPurchasesSales","0")).replace(",",""))
                if "FII" in cat or "FPI" in cat: r["fii_net"] = net
                elif "DII" in cat: r["dii_net"] = net
            except: pass
    _cset("fii",r); return r

# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL ENGINES (8 total)
# ═══════════════════════════════════════════════════════════════════════════════

def engine_trend(idx,oc):
    n=idx.get("NIFTY 50",{}); sc=0; rs=[]
    spot=oc.get("spot",0); o=n.get("open",0); h=n.get("high",0); l=n.get("low",0)
    p=n.get("previousClose",0); chg=n.get("pChange",0)
    if chg>0.5:    sc+=25; rs.append(f"NIFTY up {chg:.2f}% — bullish momentum")
    elif chg<-0.5: sc-=25; rs.append(f"NIFTY down {chg:.2f}% — bearish momentum")
    if p>0 and (h-l)>0:
        pr=(spot-l)/(h-l)
        if pr>0.7:   sc+=15; rs.append("Spot in upper 30% of day's range")
        elif pr<0.3: sc-=15; rs.append("Spot in lower 30% of day's range")
    if o>0:
        gap=(spot-p)/p*100
        if gap>0.3:    sc+=10; rs.append(f"Gap up {gap:.2f}%")
        elif gap<-0.3: sc-=10; rs.append(f"Gap down {gap:.2f}%")
        if spot>o:   sc+=10; rs.append("Spot above day open — bullish")
        elif spot<o: sc-=10; rs.append("Spot below day open — bearish")
    sc=max(-100,min(100,sc))
    return {"name":"Trend","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"chg":chg,"spot":spot}}

def engine_options(oc):
    pcr=oc.get("pcr",1.0); mp=oc.get("max_pain",0); spot=oc.get("spot",0)
    skew=oc.get("iv_skew",0); sc=0; rs=[]
    if pcr>1.5:    sc+=30; rs.append(f"PCR {pcr:.2f} — heavy put writing, bullish")
    elif pcr>1.2:  sc+=15; rs.append(f"PCR {pcr:.2f} — moderate put writing")
    elif pcr<0.7:  sc-=30; rs.append(f"PCR {pcr:.2f} — heavy call writing, bearish")
    elif pcr<0.9:  sc-=15; rs.append(f"PCR {pcr:.2f} — moderate call writing")
    if mp and spot:
        dist=((spot-mp)/mp)*100
        if dist>1.5:    sc+=20; rs.append(f"Spot {dist:.1f}% above max pain {mp}")
        elif dist>0.5:  sc+=10; rs.append(f"Spot above max pain {mp}")
        elif dist<-1.5: sc-=20; rs.append(f"Spot {abs(dist):.1f}% below max pain {mp}")
        elif dist<-0.5: sc-=10; rs.append(f"Spot below max pain {mp}")
    chain=oc.get("chain",[]); atm=oc.get("atm_strike",0)
    ad=next((c for c in chain if c["strike"]==atm),None)
    if ad:
        co=ad.get("ce_oi_change",0); po=ad.get("pe_oi_change",0)
        if co<0 and po>0: sc+=20; rs.append("CE OI unwinding + PE OI building — bullish")
        elif co>0 and po<0: sc-=20; rs.append("CE OI building + PE OI unwinding — bearish")
    if skew>3:    sc-=10; rs.append(f"IV skew +{skew:.1f} — fear elevated")
    elif skew<-3: sc+=10; rs.append(f"IV skew {skew:.1f} — CE IV higher")
    sc=max(-100,min(100,sc))
    return {"name":"Options","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"pcr":pcr,"max_pain":mp,"skew":skew}}

def engine_gamma(oc):
    chain=oc.get("chain",[]); atm=oc.get("atm_strike",0); sc=0; rs=[]
    if not chain: return {"name":"Gamma","score":0,"signal":"NEUTRAL","confidence":0,"reasons":["No data"],"data":{}}
    step=100 if atm>=40000 else 50
    near=[c for c in chain if abs(c["strike"]-atm)<=3*step]
    coi=sum(c.get("ce_oi",0) for c in near); poi=sum(c.get("pe_oi",0) for c in near); tot=coi+poi
    if tot>0:
        cp=coi/tot*100
        if cp>60:   sc-=20; rs.append(f"Gamma zone CE heavy ({cp:.0f}%) — resistance")
        elif cp<40: sc+=20; rs.append(f"Gamma zone PE heavy ({100-cp:.0f}%) — support")
    above=[c for c in chain if atm<c["strike"]<=atm+3*step]
    below=[c for c in chain if atm-3*step<=c["strike"]<atm]
    if above and below:
        ac=max(above,key=lambda x:x.get("ce_oi",0)); bp=max(below,key=lambda x:x.get("pe_oi",0))
        if ac.get("ce_oi",0)>bp.get("pe_oi",0)*1.5:   sc-=15; rs.append(f"CE wall at {ac['strike']}")
        elif bp.get("pe_oi",0)>ac.get("ce_oi",0)*1.5: sc+=15; rs.append(f"PE wall at {bp['strike']}")
    sc=max(-100,min(100,sc))
    return {"name":"Gamma","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"ce_oi":coi,"pe_oi":poi}}

def engine_volatility(idx,oc):
    vix=idx.get("INDIA VIX",{}).get("last",15); vc=idx.get("INDIA VIX",{}).get("pChange",0)
    sc=0; rs=[]
    if vix<12:   sc+=20; rs.append(f"VIX {vix:.1f} — very low fear")
    elif vix<15: sc+=10; rs.append(f"VIX {vix:.1f} — low volatility")
    elif vix>25: sc-=25; rs.append(f"VIX {vix:.1f} — high fear")
    elif vix>20: sc-=15; rs.append(f"VIX {vix:.1f} — elevated fear")
    if vc<-5:   sc+=15; rs.append(f"VIX falling {vc:.1f}%")
    elif vc>10: sc-=15; rs.append(f"VIX rising {vc:.1f}%")
    sc=max(-100,min(100,sc))
    return {"name":"Volatility","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"vix":vix,"vix_chg":vc}}

def engine_regime(idx,oc):
    n=idx.get("NIFTY 50",{}); vix=idx.get("INDIA VIX",{}).get("last",15)
    chg=abs(n.get("pChange",0)); sc=0; rs=[]; mult=1.0
    if vix<15 and chg<1.5:   sc=30;  rs.append("Trending regime — directional trades ideal"); mult=1.2
    elif vix>20 or chg>2.5:  sc=-20; rs.append("High vol regime — tight SL needed"); mult=0.7
    elif chg<0.5:             sc=-10; rs.append("Sideways regime — options decay"); mult=0.8
    else:                     sc=10;  rs.append("Normal regime"); mult=1.0
    return {"name":"Regime","score":round(sc,1),"signal":"BULL" if sc>0 else "BEAR" if sc<0 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"mult":mult}}

def engine_sentiment(fii):
    fn=fii.get("fii_net",0); dn=fii.get("dii_net",0); sc=0; rs=[]
    if fn>2000:    sc+=40; rs.append(f"FII strong buyers +₹{fn:.0f}Cr")
    elif fn>500:   sc+=20; rs.append(f"FII buyers +₹{fn:.0f}Cr")
    elif fn>0:     sc+=10; rs.append(f"FII mild buyers +₹{fn:.0f}Cr")
    elif fn<-2000: sc-=40; rs.append(f"FII strong sellers ₹{fn:.0f}Cr")
    elif fn<-500:  sc-=20; rs.append(f"FII sellers ₹{fn:.0f}Cr")
    elif fn<0:     sc-=10; rs.append(f"FII mild sellers ₹{fn:.0f}Cr")
    if dn>0 and fn>0:   sc+=10; rs.append("Both FII+DII buying — strong support")
    elif dn<0 and fn<0: sc-=10; rs.append("Both FII+DII selling — dual pressure")
    sc=max(-100,min(100,sc))
    return {"name":"Sentiment","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"fii_net":fn,"dii_net":dn}}

def engine_flow(oc):
    chain=oc.get("chain",[]); atm=oc.get("atm_strike",0); sc=0; rs=[]
    step=100 if atm>=40000 else 50
    near=[c for c in chain if abs(c["strike"]-atm)<=5*step]
    ca=sum(c.get("ce_oi_change",0) for c in near if c.get("ce_oi_change",0)>0)
    pa=sum(c.get("pe_oi_change",0) for c in near if c.get("pe_oi_change",0)>0)
    cu=abs(sum(c.get("ce_oi_change",0) for c in near if c.get("ce_oi_change",0)<0))
    pu=abs(sum(c.get("pe_oi_change",0) for c in near if c.get("pe_oi_change",0)<0))
    if pa>ca*1.5:   sc+=25; rs.append("PE OI building faster — bulls in control")
    elif ca>pa*1.5: sc-=25; rs.append("CE OI building faster — bears in control")
    if pu>pa*1.2 and cu<pu:  sc-=20; rs.append("PE OI unwinding — caution")
    elif cu>ca*1.2 and pu<cu: sc+=20; rs.append("CE OI unwinding — bullish signal")
    ocv=sum(c.get("ce_volume",0) for c in near if c["strike"]>atm+step)
    opv=sum(c.get("pe_volume",0) for c in near if c["strike"]<atm-step)
    if ocv+opv>0:
        cp=ocv/(ocv+opv)*100
        if cp>65:   sc+=20; rs.append(f"High OTM CE buying ({cp:.0f}%)")
        elif cp<35: sc-=20; rs.append(f"High OTM PE buying ({100-cp:.0f}%)")
    sc=max(-100,min(100,sc))
    return {"name":"Flow","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"ce_add":ca,"pe_add":pa}}

NEWS_SIGNALS = {
    "rate cut":80,"repo cut":80,"stimulus":70,"gdp beat":65,"inflation ease":60,
    "cpi lower":60,"surplus":55,"buyback":55,"dividend":50,"upgrade":55,
    "record high":50,"profit surge":55,"earnings beat":55,"fdi":60,
    "foreign inflow":65,"fii buy":70,"dii buy":55,"dovish":55,"easing":50,"rate pause":35,
    "rate hike":-80,"hawkish":-65,"recession":-80,"inflation surge":-65,"cpi high":-60,
    "deficit":-55,"downgrade":-60,"war":-75,"conflict":-65,"sanctions":-60,"tax hike":-55,
    "loss":-50,"profit fall":-50,"earnings miss":-55,"layoffs":-40,"bankruptcy":-70,
    "default":-75,"crisis":-70,"sell off":-65,"fii sell":-70,"outflow":-60,"slowdown":-55,
}

def analyze_news_impact(news_list):
    impactful=[]; total=0
    for item in news_list:
        txt=(item.get("title","")+" "+item.get("company","")).lower()
        sc=0; kws=[]
        for kw,v in NEWS_SIGNALS.items():
            if kw in txt: sc+=v; kws.append(kw)
        sc=max(-100,min(100,sc))
        if abs(sc)>=20:
            impactful.append({**item,"impact_score":round(sc,1),
                "impact_level":"HIGH" if abs(sc)>=60 else "MEDIUM" if abs(sc)>=30 else "LOW",
                "direction":"BULL" if sc>0 else "BEAR","keywords":kws[:3]})
            total+=sc
    impactful.sort(key=lambda x:abs(x["impact_score"]),reverse=True)
    ns=round(total/max(1,len(impactful)),1) if impactful else 0
    return {"impactful_news":impactful[:10],"news_sentiment_score":ns,
            "bull_count":len([i for i in impactful if i["direction"]=="BULL"]),
            "bear_count":len([i for i in impactful if i["direction"]=="BEAR"]),
            "total_news_analyzed":len(news_list)}

def engine_news(na):
    ns=na.get("news_sentiment_score",0); bc=na.get("bull_count",0); rc=na.get("bear_count",0)
    sc=max(-100,min(100,ns)); rs=[]
    if bc>rc and abs(sc)>20:   rs.append(f"{bc} bullish news items")
    elif rc>bc and abs(sc)>20: rs.append(f"{rc} bearish news items")
    else:                       rs.append(f"News neutral — {bc} bull, {rc} bear")
    imp=na.get("impactful_news",[])
    if imp: rs.append(f"Top: {imp[0].get('title','')[:60]}...")
    return {"name":"News","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL",
            "confidence":min(95,abs(sc)),"reasons":rs,"data":{"sentiment":ns}}

WEIGHTS={"trend":0.16,"options":0.18,"gamma":0.11,"volatility":0.09,
         "regime":0.07,"sentiment":0.14,"flow":0.15,"news":0.10}

def generate_signal(snapshot):
    idx=snapshot["indices"]; oc=snapshot["option_data"]; fii=snapshot["fii_dii"]
    na=snapshot.get("news_analysis",{"news_sentiment_score":0,"bull_count":0,"bear_count":0,"impactful_news":[]})
    e={"trend":engine_trend(idx,oc),"options":engine_options(oc),"gamma":engine_gamma(oc),
       "volatility":engine_volatility(idx,oc),"regime":engine_regime(idx,oc),
       "sentiment":engine_sentiment(fii),"flow":engine_flow(oc),"news":engine_news(na)}
    composite=sum(e[k]["score"]*WEIGHTS.get(k,0) for k in e)
    mult=e["regime"]["data"].get("mult",1.0)
    confidence=round(min(97,max(35,abs(composite)*mult)),1)
    bull=sum(1 for v in e.values() if v["signal"]=="BULL")
    bear=sum(1 for v in e.values() if v["signal"]=="BEAR")
    conf=bull if composite>0 else bear
    if abs(composite)<25 or conf<3:    action,cls="WAIT","wait";   sub=f"Low confluence ({conf}/8)"
    elif composite>50:                  action,cls="BUY CE","bull"; sub=f"STRONG BULL — {conf}/8 aligned"
    elif composite>25:                  action,cls="BUY CE","bull"; sub=f"BULL — {conf}/8 engines"
    elif composite<-50:                 action,cls="BUY PE","bear"; sub=f"STRONG BEAR — {conf}/8 aligned"
    elif composite<-25:                 action,cls="BUY PE","bear"; sub=f"BEAR — {conf}/8 engines"
    else:                               action,cls="WAIT","wait";   sub="Borderline — wait"
    reasons=[e[k]["reasons"][0] for k in ["options","sentiment","flow","trend","gamma","volatility","regime","news"] if e[k]["reasons"]]
    spot=oc.get("spot",0); mp=oc.get("max_pain",spot); atm=oc.get("atm_strike",0)
    return {"signal_id":str(uuid.uuid4())[:8],"timestamp":datetime.datetime.now().isoformat(),
            "symbol":snapshot.get("symbol","NIFTY"),
            "action":action,"signal_cls":cls,"signal_sub":sub,
            "composite":round(composite,2),"confidence":confidence,
            "bull_count":bull,"bear_count":bear,"confluence":conf,
            "engines":{k:{"score":v["score"],"signal":v["signal"],"confidence":v["confidence"]} for k,v in e.items()},
            "top_reasons":reasons[:8],
            "market_data":{"spot":spot,"atm":atm,"max_pain":mp,"pcr":oc.get("pcr",0),
                           "vix":idx.get("INDIA VIX",{}).get("last",0),
                           "fii_net":fii.get("fii_net",0),"expiry":oc.get("expiry",""),
                           "resistances":[c["strike"] for c in oc.get("resistance_strikes",[])],
                           "supports":[c["strike"] for c in oc.get("support_strikes",[])]},
            "chain":oc.get("chain",[])}

# ─── CAPITAL MANAGER ──────────────────────────────────────────────────────────
class CapMgr:
    def __init__(self,cfg):
        self.total=cfg.get("total_capital",50000); self.risk_pct=cfg.get("risk_per_trade",2.0)
        self.rr=cfg.get("rr_ratio",2.0); self.max_trades=cfg.get("max_trades_day",3)
        self.avail=self.total; self.in_trade=0.0; self.peak=self.total
        self.ses_trades=self.ses_pnl=self.ses_wins=self.ses_loss=self.consec=0
        self.date=datetime.date.today(); self._last_cfg=cfg
    def _chk_day(self):
        if datetime.date.today()!=self.date:
            self.ses_trades=self.ses_pnl=self.ses_wins=self.ses_loss=self.consec=0
            self.date=datetime.date.today()
    def can_trade(self):
        self._chk_day()
        if self.ses_trades>=self.max_trades: return {"allowed":False,"reason":f"Max {self.max_trades} trades"}
        dlim=self.total*0.03
        if self.ses_pnl<=-dlim: return {"allowed":False,"reason":f"Daily loss ₹{dlim:.0f} hit"}
        dd=(self.peak-self.avail)/self.peak*100 if self.peak>0 else 0
        if dd>=10: return {"allowed":False,"reason":f"Drawdown {dd:.1f}% — paused"}
        return {"allowed":True,"reason":"OK"}
    def calc_position(self,sig,oc):
        sym=sig.get("symbol","NIFTY"); action=sig.get("action","BUY CE")
        spot=sig.get("market_data",{}).get("spot",0)
        atm=sig.get("market_data",{}).get("atm",round(spot/50)*50)
        chain=sig.get("chain",[]); exp=sig.get("market_data",{}).get("expiry","")
        ls=LOT_SIZES.get(sym,75); mg=MARGINS.get(sym,6500)
        cp=pp=0; live_src="nse_chain"
        if kite["access_token"] and exp:
            try:
                live=fetch_live_ltp(sym, atm if "CE" in action else None,
                                        atm if "PE" in action else None, exp)
                if live["ce_ltp"]>0: cp=live["ce_ltp"]; live_src="kite_live"
                if live["pe_ltp"]>0: pp=live["pe_ltp"]; live_src="kite_live"
            except: pass
        if cp==0 or pp==0:
            for c in chain:
                if c["strike"]==atm:
                    if cp==0: cp=c.get("ce_ltp",0)
                    if pp==0: pp=c.get("pe_ltp",0); break
        if cp==0: cp=round(spot*0.0055,1)
        if pp==0: pp=round(spot*0.0050,1)
        rm=0.5 if self.consec>=2 else 1.0
        risk=self.avail*(self.risk_pct/100)*rm
        pr=cp if action=="BUY CE" else pp if action=="BUY PE" else (cp+pp)/2
        pls=pr*SL_PCT*ls; lots=max(1,int(risk/pls)) if pls>0 else 1
        mult=2 if "+" in action else 1
        return {"action":action,"symbol":sym,"expiry":exp,
                "ce_strike":atm if "CE" in action else None,
                "pe_strike":atm if "PE" in action else None,
                "ce_price":round(cp,1),"pe_price":round(pp,1),
                "ce_sl":round(cp*(1-SL_PCT),1),"ce_target":round(cp*(1+self.rr*SL_PCT),1),
                "pe_sl":round(pp*(1-SL_PCT),1),"pe_target":round(pp*(1+self.rr*SL_PCT),1),
                "lots":lots,"lot_size":ls,"capital_used":int(lots*mg*mult),
                "max_loss":int(lots*pr*ls*SL_PCT*mult),
                "target_pnl":int(lots*pr*ls*SL_PCT*mult*self.rr),
                "risk_reduced":rm<1.0,"spot":spot,"atm":atm,"ltp_source":live_src}
    def record(self,pnl):
        self.ses_trades+=1; self.ses_pnl+=pnl; self.avail+=pnl; self.in_trade=0
        if pnl>0: self.ses_wins+=1; self.consec=0
        else:     self.ses_loss+=1; self.consec+=1
        if self.avail>self.peak: self.peak=self.avail
    def status(self):
        wr=round(self.ses_wins/self.ses_trades*100,1) if self.ses_trades>0 else 0
        dlp=round(abs(min(0,self.ses_pnl))/(self.total*0.03)*100,1) if self.ses_pnl<0 else 0
        dd=round((self.peak-self.avail)/self.peak*100,2) if self.peak>0 else 0
        return {"total_capital":self.total,"available_capital":round(self.avail,2),
                "in_trade_capital":round(self.in_trade,2),"session_pnl":round(self.ses_pnl,2),
                "session_trades":self.ses_trades,"session_wins":self.ses_wins,"session_losses":self.ses_loss,
                "win_rate":wr,"consecutive_loss":self.consec,"drawdown_pct":dd,
                "daily_loss_used_pct":dlp,"trades_left_today":max(0,self.max_trades-self.ses_trades),
                "max_trades_day":self.max_trades}

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    c=sqlite3.connect(DB_PATH); cur=c.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, symbol TEXT, action TEXT,
        ce_strike INTEGER, pe_strike INTEGER, lots INTEGER,
        ce_entry REAL, pe_entry REAL, ce_exit REAL, pe_exit REAL,
        capital_used INTEGER, max_loss INTEGER, target_pnl INTEGER, actual_pnl REAL,
        exit_type TEXT, entry_time TEXT, exit_time TEXT,
        capital_before REAL, capital_after REAL, ltp_source TEXT,
        kite_ce_order_id TEXT, kite_pe_order_id TEXT);
    CREATE TABLE IF NOT EXISTS kite_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER,
        order_id TEXT, tradingsymbol TEXT, transaction_type TEXT,
        quantity INTEGER, order_type TEXT, status TEXT,
        placed_at TEXT, response TEXT);
    """); c.commit(); c.close()

def save_trade_db(t):
    c=sqlite3.connect(DB_PATH); cur=c.cursor()
    cur.execute("""INSERT INTO trades(signal_id,symbol,action,ce_strike,pe_strike,lots,
        ce_entry,pe_entry,capital_used,max_loss,target_pnl,entry_time,capital_before,ltp_source,
        kite_ce_order_id,kite_pe_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t.get("signal_id",""),t.get("symbol"),t.get("action"),
         t.get("ce_strike"),t.get("pe_strike"),t.get("lots"),
         t.get("ce_price"),t.get("pe_price"),t.get("capital_used"),
         t.get("max_loss"),t.get("target_pnl"),datetime.datetime.now().isoformat(),
         t.get("capital_before",0),t.get("ltp_source","nse_chain"),
         t.get("kite_ce_order_id",""),t.get("kite_pe_order_id","")))
    tid=cur.lastrowid; c.commit(); c.close(); return tid

def close_trade_db(tid,ex):
    c=sqlite3.connect(DB_PATH); cur=c.cursor()
    cur.execute("""UPDATE trades SET ce_exit=?,pe_exit=?,actual_pnl=?,exit_type=?,
        exit_time=?,capital_after=? WHERE id=?""",
        (ex.get("ce_exit"),ex.get("pe_exit"),ex.get("pnl"),ex.get("exit_type"),
         datetime.datetime.now().isoformat(),ex.get("capital_after"),tid))
    c.commit(); c.close()

def get_history():
    try:
        c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; cur=c.cursor()
        rows=cur.execute("SELECT * FROM trades ORDER BY entry_time DESC LIMIT 50").fetchall()
        perf=cur.execute("""SELECT COUNT(*) t,SUM(CASE WHEN actual_pnl>0 THEN 1 ELSE 0 END) w,
            SUM(actual_pnl) p FROM trades WHERE actual_pnl IS NOT NULL""").fetchone()
        c.close(); trades=[dict(r) for r in rows]; pd=dict(perf) if perf else {}
        if pd.get("t",0)>0: pd["win_rate"]=round(pd["w"]/pd["t"]*100,1)
        return trades,pd
    except: return [],{}

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────────
risk_mgr=None; active_monitor=None; active_tid=None
last_signal=None; last_pos=None; last_generated_signal=None

init_db()

def self_ping():
    time.sleep(30)
    while True:
        try: requests.get(f"{OWN_URL}/ping",timeout=10)
        except: pass
        time.sleep(600)

threading.Thread(target=self_ping,daemon=True).start()
threading.Thread(target=auto_signal_loop,daemon=True).start()

if kite["access_token"]:
    kite["connected"]=True; start_ltp_polling()
    threading.Thread(target=start_kite_ws,daemon=True).start()
    print("[KITE] Pre-set token — connecting WS")

def _do_generate_signal(cfg):
    global last_generated_signal,last_pos,last_signal
    sym=cfg.get("symbol","NIFTY")
    if risk_mgr is None: return None,None,None,None
    idx=fetch_indices(); oc=fetch_chain(sym); fii=fetch_fii()
    if "error" in oc: return None,None,None,{"error":f"Data fetch failed: {oc['error']}"}
    na={"news_sentiment_score":0,"bull_count":0,"bear_count":0,"impactful_news":[]}
    try:
        nd=nse_get("https://www.nseindia.com/api/corporate-announcements?index=equities")
        if nd and isinstance(nd,list):
            na=analyze_news_impact([{"title":x.get("subject",""),"company":x.get("symbol",""),
                                     "time":x.get("an_dt","")} for x in nd[:30]])
    except: pass
    snap={"symbol":sym,"indices":idx,"option_data":oc,"fii_dii":fii,"news_analysis":na}
    sig=generate_signal(snap); last_signal=sig; last_generated_signal=sig
    ct=risk_mgr.can_trade(); pos=None
    if sig["action"]!="WAIT" and ct["allowed"]:
        pos=risk_mgr.calc_position(sig,oc); last_pos=pos
    return sig,pos,ct,None

# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def root():
    return jsonify({"status":"alive","version":"3.0","service":"QUANTRA BEAST",
                    "broker":"Zerodha Kite","kite_connected":kite["connected"],
                    "kite_ws":kite["ws_connected"],"time":str(datetime.datetime.now())})

@app.route("/ping")
def ping():
    return jsonify({"status":"alive","kite":kite["connected"],"kite_ws":kite["ws_connected"],
                    "time":str(datetime.datetime.now())})

# ── Kite Auth ─────────────────────────────────────────────────────────────────
@app.route("/kite/login-url")
def kite_login_route():
    if not KITE_API_KEY: return jsonify({"error":"KITE_API_KEY not set"}),400
    return jsonify({"login_url":kite_login_url(),
                    "instructions":"Open URL → login with Zerodha → auto-redirects back",
                    "redirect_uri":KITE_REDIRECT})

@app.route("/kite/callback")
def kite_callback():
    rt=request.args.get("request_token"); status=request.args.get("status","")
    if status!="success" or not rt:
        err=request.args.get("message","Login failed or cancelled")
        return f"""<html><body style='background:#020b08;color:#ff3355;font-family:monospace;
            display:flex;align-items:center;justify-content:center;height:100vh;text-align:center'>
            <div><div style='font-size:36px'>❌</div>
            <div style='font-size:18px;margin:10px'>Kite login failed</div>
            <div style='font-size:13px;color:#ff6677'>{err}</div></div></body></html>""",400
    success,result=kite_exchange_token(rt)
    if success:
        name=kite["profile"].get("user_name","")
        return f"""<html><body style='background:#020b08;color:#00ff88;
            font-family:Share Tech Mono,monospace;display:flex;align-items:center;
            justify-content:center;height:100vh;text-align:center'>
            <div><div style='font-size:48px;margin-bottom:20px'>✅</div>
            <div style='font-size:28px;letter-spacing:3px;margin-bottom:8px'>KITE CONNECTED</div>
            <div style='color:#00cc66;font-size:15px;margin-bottom:6px'>Welcome, {name}</div>
            <div style='color:#5a8a7a;font-size:12px'>Live WebSocket streaming active</div>
            <div style='color:#5a8a7a;font-size:11px;margin-top:10px'>Return to QUANTRA BEAST</div>
            </div></body></html>"""
    return f"""<html><body style='background:#020b08;color:#ff3355;font-family:monospace;
        display:flex;align-items:center;justify-content:center;height:100vh;text-align:center;padding:20px'>
        <div><div style='font-size:36px'>❌</div>
        <div style='font-size:18px;margin:10px'>Auth Failed</div>
        <div style='font-size:12px;color:#ff6677;max-width:500px;word-break:break-all'>{result}</div>
        <div style='font-size:11px;color:#884444;margin-top:15px'>
        API Key: {KITE_API_KEY[:8] if KITE_API_KEY else "NOT SET"}... | Redirect: {KITE_REDIRECT}
        </div></div></body></html>""",400

@app.route("/kite/set-token",methods=["POST"])
def kite_set_token():
    b=request.get_json(force=True) or {}; token=b.get("access_token","")
    if not token: return jsonify({"error":"No access_token"}),400
    kite["access_token"]=token; kite["connected"]=True
    kite["last_token_time"]=datetime.datetime.now().isoformat(); kite["error"]=None
    start_ltp_polling()
    threading.Thread(target=start_kite_ws,daemon=True).start()
    return jsonify({"message":"Kite token set","connected":True})

@app.route("/kite/logout",methods=["POST"])
def kite_logout():
    kite_invalidate_token()
    return jsonify({"message":"Logged out","connected":False})

@app.route("/kite/status")
def kite_status():
    return jsonify({"connected":kite["connected"],"ws_connected":kite["ws_connected"],
                    "last_token_time":kite["last_token_time"],
                    "tokens_cached":len(kite["ltp_cache"]),
                    "subscribed":len(kite["subscribed"]),
                    "error":kite["error"],"has_token":bool(kite["access_token"]),
                    "profile":kite["profile"],"login_url":kite_login_url() if KITE_API_KEY else None})

@app.route("/kite/ltp")
def kite_ltp_route():
    syms=[s.strip() for s in request.args.get("symbols","").split(",") if s.strip()]
    if not syms: return jsonify({"error":"Provide ?symbols=NSE:NIFTY 50,..."})
    return jsonify({"ltp":kite_ltp_rest(syms),"ws_connected":kite["ws_connected"]})

@app.route("/kite/quote")
def kite_quote_route():
    syms=[s.strip() for s in request.args.get("symbols","").split(",") if s.strip()]
    if not syms: return jsonify({"error":"Provide ?symbols=NSE:NIFTY 50"})
    return jsonify({"quote":kite_quotes(syms)})

# ── Kite Order Routes ─────────────────────────────────────────────────────────
@app.route("/kite/orders")
def kite_orders_route():
    return jsonify({"orders":kite_get_orders()})

@app.route("/kite/positions")
def kite_positions_route():
    return jsonify({"positions":kite_get_positions()})

@app.route("/kite/margins")
def kite_margins_route():
    return jsonify({"margins":kite_get_margins()})

@app.route("/kite/place-order",methods=["POST"])
def kite_place_order_route():
    if not kite["access_token"]: return jsonify({"error":"Kite not connected"}),400
    b=request.get_json(force=True) or {}
    result=kite_place_order(b)
    return jsonify(result),(200 if "order_id" in result else 400)

@app.route("/kite/modify-order/<order_id>",methods=["PUT"])
def kite_modify_route(order_id):
    b=request.get_json(force=True) or {}
    return jsonify(kite_modify_order(order_id,b))

@app.route("/kite/cancel-order/<order_id>",methods=["DELETE"])
def kite_cancel_route(order_id):
    variety=request.args.get("variety","regular")
    return jsonify(kite_cancel_order(order_id,variety))

# ── Signal + Trade Routes ─────────────────────────────────────────────────────
@app.route("/generate-signal",methods=["POST"])
def gen_signal():
    global risk_mgr
    b=request.get_json(force=True) or {}; sym=b.get("symbol","NIFTY").upper()
    cfg={"total_capital":b.get("capital",50000),"risk_per_trade":b.get("risk_pct",2.0),
         "rr_ratio":b.get("rr_ratio",2.0),"max_trades_day":b.get("max_trades",3),"symbol":sym}
    if risk_mgr is None: risk_mgr=CapMgr(cfg)
    else: risk_mgr._last_cfg=cfg
    sig,pos,ct,err=_do_generate_signal(cfg)
    if err: return jsonify(err),503
    idx=fetch_indices(); oc=fetch_chain(sym); fii=fetch_fii()
    na={"news_sentiment_score":0,"bull_count":0,"bear_count":0,"impactful_news":[]}
    try:
        nd=nse_get("https://www.nseindia.com/api/corporate-announcements?index=equities")
        if nd and isinstance(nd,list):
            na=analyze_news_impact([{"title":x.get("subject",""),"company":x.get("symbol",""),
                                     "time":x.get("an_dt","")} for x in nd[:30]])
    except: pass
    order_preview=build_order_params(pos) if pos else []
    return jsonify({"signal":sig,"position":pos,"capital":risk_mgr.status(),"can_trade":ct,
                    "indices":idx,"fii_dii":fii,"chain":oc.get("chain",[]),
                    "option_meta":{k:v for k,v in oc.items() if k!="chain"},
                    "news_analysis":na,"order_preview":order_preview,
                    "kite_connected":kite["connected"],"kite_ws":kite["ws_connected"],
                    "data_source":oc.get("data_source","scraperapi")})

@app.route("/latest-signal")
def latest_signal():
    if not last_generated_signal: return jsonify({"signal":None,"message":"No signal yet"})
    return jsonify({"signal":last_generated_signal,"position":last_pos,
                    "capital":risk_mgr.status() if risk_mgr else None,
                    "kite_connected":kite["connected"],"auto_generated":True,
                    "ts":datetime.datetime.now().isoformat()})

@app.route("/trade/enter",methods=["POST"])
def trade_enter():
    global active_monitor,active_tid
    b=request.get_json(force=True) or {}
    pos=b.get("position",{}); sig=b.get("signal",last_signal or {})
    place_orders=b.get("place_orders",False)
    if not pos: return jsonify({"error":"No position"}),400
    if risk_mgr is None: return jsonify({"error":"Not initialized"}),400
    pos["signal_id"]=sig.get("signal_id",""); pos["capital_before"]=risk_mgr.avail
    risk_mgr.in_trade=pos.get("capital_used",0)
    kite_ce_oid=kite_pe_oid=""; order_results=[]
    if place_orders and kite["access_token"]:
        for o in build_order_params(pos):
            res=kite_place_order(dict(o)); order_results.append(res)
            if "order_id" in res:
                if "CE" in o.get("tag",""): kite_ce_oid=res["order_id"]
                elif "PE" in o.get("tag",""): kite_pe_oid=res["order_id"]
    pos["kite_ce_order_id"]=kite_ce_oid; pos["kite_pe_order_id"]=kite_pe_oid
    tid=save_trade_db(pos); active_tid=tid
    active_monitor={"trade":pos,"entry":datetime.datetime.now(),
                    "trailing":False,"partial":False,
                    "live_ce_ltp":0,"live_pe_ltp":0,"live_spot":0}
    start_ltp_polling()
    return jsonify({"trade_id":tid,"message":"Trade recorded",
                    "ltp_source":pos.get("ltp_source","nse_chain"),
                    "kite_orders_placed":place_orders,"kite_order_results":order_results})

@app.route("/trade/monitor")
def trade_monitor():
    if not active_monitor: return jsonify({"status":"NO_ACTIVE_TRADE"})
    pos=active_monitor["trade"]; action=pos.get("action","BUY CE")
    sym=pos.get("symbol","NIFTY"); ls=LOT_SIZES.get(sym,75); lots=pos.get("lots",1)
    ce_e=pos.get("ce_price",0); pe_e=pos.get("pe_price",0)
    ce_sl=pos.get("ce_sl",0); pe_sl=pos.get("pe_sl",0)
    ce_tgt=pos.get("ce_target",0); pe_tgt=pos.get("pe_target",0)
    max_loss=pos.get("max_loss",0); tpnl=pos.get("target_pnl",0)
    ce_s=active_monitor.get("live_ce_ltp",0); pe_s=active_monitor.get("live_pe_ltp",0)
    ltp_src="kite_ws" if kite["ws_connected"] else "kite_rest" if kite["connected"] else "nse_chain"
    if ce_s==0 or pe_s==0:
        oc=fetch_chain(sym); chain=oc.get("chain",[])
        cs=pos.get("ce_strike"); ps=pos.get("pe_strike")
        for c in chain:
            if cs and c["strike"]==cs and ce_s==0: ce_s=c.get("ce_ltp",ce_e)
            if ps and c["strike"]==ps and pe_s==0: pe_s=c.get("pe_ltp",pe_e)
        ltp_src="nse_chain"
    if ce_s==0: ce_s=ce_e
    if pe_s==0: pe_s=pe_e
    if action=="BUY CE":   pnl=(ce_s-ce_e)*lots*ls
    elif action=="BUY PE": pnl=(pe_s-pe_e)*lots*ls
    else:                  pnl=((ce_s-ce_e)+(pe_s-pe_e))*lots*ls
    elapsed=int((datetime.datetime.now()-active_monitor["entry"]).total_seconds()//60)
    pct=round(pnl/max_loss*100,1) if max_loss else 0
    status="HOLD"; reason=""; urgency="normal"
    if (action=="BUY CE" and ce_s>0 and ce_s<=ce_sl) or (action=="BUY PE" and pe_s>0 and pe_s<=pe_sl):
        status="EXIT_SL"; reason=f"SL HIT — P&L ₹{pnl:.0f}"; urgency="critical"
    elif (action=="BUY CE" and ce_s>=ce_tgt) or (action=="BUY PE" and pe_s>=pe_tgt) or (action=="BUY CE + PE" and pnl>=tpnl):
        status="EXIT_TARGET"; reason=f"TARGET HIT ₹{pnl:.0f}"; urgency="success"
    elif pnl>tpnl*0.6 and not active_monitor["trailing"]:
        active_monitor["trailing"]=True; pos["ce_sl"]=ce_e; pos["pe_sl"]=pe_e
        status="TRAIL_SL"; reason=f"SL trailed to entry — ₹{pnl:.0f} protected"; urgency="info"
    elif pnl>tpnl*0.5 and not active_monitor["partial"] and lots>=2:
        active_monitor["partial"]=True; status="BOOK_PARTIAL"
        reason=f"Book {lots//2} lots — ₹{pnl:.0f} profit"; urgency="action"
    elif elapsed>180 and abs(pct)<20:
        status="TIME_DECAY"; reason=f"Stagnant {elapsed}min — time decay"; urgency="warning"
    return jsonify({"status":status,"urgency":urgency,"exit_reason":reason,
                    "pnl":round(pnl,2),"pnl_pct":pct,"elapsed_min":elapsed,
                    "ce_current":ce_s,"pe_current":pe_s,
                    "ce_sl":pos.get("ce_sl",ce_sl),"pe_sl":pos.get("pe_sl",pe_sl),
                    "ce_target":ce_tgt,"pe_target":pe_tgt,
                    "ltp_source":ltp_src,"live_spot":active_monitor.get("live_spot",0)})

@app.route("/trade/exit",methods=["POST"])
def trade_exit():
    global active_monitor,active_tid
    b=request.get_json(force=True) or {}
    pnl=float(b.get("pnl",0)); etype=b.get("exit_type","MANUAL")
    if risk_mgr is None: return jsonify({"error":"Not initialized"}),400
    risk_mgr.record(pnl)
    if active_tid: close_trade_db(active_tid,{"pnl":pnl,"exit_type":etype,"capital_after":risk_mgr.avail})
    active_monitor=None; active_tid=None
    return jsonify({"message":f"Closed ₹{pnl:.0f}","capital":risk_mgr.status(),
                    "next_can_trade":risk_mgr.can_trade()})

@app.route("/capital")
def capital():
    if not risk_mgr: return jsonify({"error":"Not initialized"})
    return jsonify(risk_mgr.status())

@app.route("/capital/reset",methods=["POST"])
def cap_reset():
    global risk_mgr
    b=request.get_json(force=True) or {}
    risk_mgr=CapMgr({"total_capital":b.get("capital",50000),"risk_per_trade":b.get("risk_pct",2.0),
                      "rr_ratio":b.get("rr_ratio",2.0),"max_trades_day":b.get("max_trades",3)})
    return jsonify({"message":"Reset","capital":risk_mgr.status()})

@app.route("/history")
def history():
    trades,perf=get_history()
    return jsonify({"trades":trades,"performance":perf})

@app.route("/gift-nifty")
def gift_nifty():
    c=_cget("gift")
    if c: return jsonify(c)
    ltp=get_index_ltp("NIFTY 50")
    gift=({"last":ltp,"change":0,"pChange":0,"market":"GIFT NIFTY",
           "note":"Live via Kite WS" if kite["ws_connected"] else "Via Kite REST","source":"kite"}
          if ltp>0 else {})
    if not gift:
        idx=_cget("idx") or {}; n=idx.get("NIFTY 50",{})
        gift={"last":n.get("last",0),"change":n.get("change",0),"pChange":n.get("pChange",0),
              "market":"GIFT NIFTY (approx)","source":"nse"}
    _cset("gift",gift); return jsonify(gift)

@app.route("/market-news")
def market_news():
    c=_cget("news",ttl=120)
    if c: return jsonify(c)
    news=[]
    try:
        d=nse_get("https://www.nseindia.com/api/corporate-announcements?index=equities")
        if d and isinstance(d,list):
            for item in d[:15]:
                news.append({"title":item.get("subject","") or item.get("desc",""),
                              "company":item.get("symbol",""),
                              "time":item.get("an_dt","") or item.get("exchdisstime",""),
                              "type":"CORP"})
    except: pass
    if not news: news=[{"title":"Market data loading...","company":"","time":"","type":"INFO"}]
    result={"news":news,"count":len(news),"fetched_at":datetime.datetime.now().isoformat()}
    _cset("news",result); return jsonify(result)

@app.route("/market-status")
def mkt_status():
    now=datetime.datetime.now(); wd=now.weekday()
    mo=now.replace(hour=9,minute=15,second=0,microsecond=0)
    mc=now.replace(hour=15,minute=30,second=0,microsecond=0)
    if wd>=5:    s,sess="CLOSED","Weekend"
    elif now<now.replace(hour=9,minute=0,second=0,microsecond=0): s,sess="CLOSED","Pre-Market"
    elif now<mo: s,sess="PRE-OPEN","Pre-Open 9:00–9:15"
    elif now<=mc: s,sess="OPEN",f"Live | {int((mc-now).total_seconds()//60)}m to close"
    else:         s,sess="CLOSED","After Market"
    return jsonify({"status":s,"session":sess,"is_open":s=="OPEN",
                    "time":now.strftime("%H:%M:%S"),"date":now.strftime("%d %b %Y")})

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=5000,debug=False)
