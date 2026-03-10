# core/kite_client.py
# QUANTRA BEAST v3.2 — Kite Connect Client
# Auth, WebSocket, REST LTP/Quotes, Order placement

import requests, time, json, datetime, threading, hashlib, struct, ssl, os

KITE_API_KEY    = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")

KITE_INDEX_TOKENS = {
    "NIFTY 50":          256265,
    "NIFTY BANK":        260105,
    "INDIA VIX":         264969,
    "NIFTY FIN SERVICE": 257801,
    "SENSEX":            265,
}
TOKEN_TO_NAME = {v: k for k, v in KITE_INDEX_TOKENS.items()}

LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65}
MARGINS   = {"NIFTY": 6500, "BANKNIFTY": 11000, "FINNIFTY": 6000}


def _kh(kite_state: dict) -> dict:
    return {
        "X-Kite-Version": "3",
        "Authorization":  f"token {KITE_API_KEY}:{kite_state['access_token']}",
    }

# ═══════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════

def kite_login_url() -> str | None:
    if not KITE_API_KEY: return None
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={KITE_API_KEY}"

def kite_exchange_token(request_token: str, kite_state: dict,
                         on_success_callback=None) -> tuple[bool, str]:
    try:
        checksum = hashlib.sha256(
            f"{KITE_API_KEY}{request_token}{KITE_API_SECRET}".encode()
        ).hexdigest()
        r = requests.post(
            "https://api.kite.trade/session/token",
            data={"api_key": KITE_API_KEY,
                  "request_token": request_token,
                  "checksum": checksum},
            headers={"X-Kite-Version": "3"},
            timeout=15,
        )
        d = r.json()
        if d.get("status") == "success":
            data = d.get("data", {})
            kite_state["access_token"]    = data.get("access_token", "")
            kite_state["connected"]       = True
            kite_state["last_token_time"] = datetime.datetime.now().isoformat()
            kite_state["error"]           = None
            kite_state["profile"] = {
                "user_name":  data.get("user_name", ""),
                "user_id":    data.get("user_id", ""),
                "email":      data.get("email", ""),
                "broker":     data.get("broker", "ZERODHA"),
                "login_time": data.get("login_time", ""),
            }
            print(f"[KITE] ✅ Logged in as {kite_state['profile'].get('user_name')}")
            if on_success_callback:
                on_success_callback()
            return True, kite_state["access_token"]
        err = d.get("message", str(d))
        kite_state["error"] = err
        return False, err
    except Exception as e:
        kite_state["error"] = str(e)
        return False, str(e)

def kite_invalidate_token(kite_state: dict):
    if not kite_state.get("access_token"): return
    try:
        requests.delete(
            "https://api.kite.trade/session/token",
            data={"api_key": KITE_API_KEY,
                  "access_token": kite_state["access_token"]},
            headers=_kh(kite_state),
            timeout=10,
        )
    except:
        pass
    kite_state["access_token"] = ""
    kite_state["connected"]    = False
    kite_state["ws_connected"] = False
    kite_state["error"]        = None


# ═══════════════════════════════════════════════════════════
#  WEBSOCKET
# ═══════════════════════════════════════════════════════════

def _parse_kite_binary(data: bytes, kite_state: dict):
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
            kite_state["ltp_cache"][token] = entry
            sym = TOKEN_TO_NAME.get(token)
            if sym:
                kite_state["ltp_sym"][f"NSE:{sym}"] = entry
                kite_state["last_tick_ts"] = time.time()
    except Exception as e:
        print(f"[KITE WS PARSE] {e}")

def start_kite_ws(kite_state: dict):
    if not kite_state.get("access_token") or not KITE_API_KEY: return
    try:
        import websocket as ws_lib
        WS_URL = (f"wss://ws.kite.trade?api_key={KITE_API_KEY}"
                  f"&access_token={kite_state['access_token']}")
        ALL_TOKENS = list(KITE_INDEX_TOKENS.values())

        def _subscribe(ws):
            ws.send(json.dumps({"a": "subscribe", "v": ALL_TOKENS}))
            ws.send(json.dumps({"a": "mode", "v": ["ltp", ALL_TOKENS]}))
            kite_state["subscribed"].update(ALL_TOKENS)
            print(f"[KITE WS] Subscribed {len(ALL_TOKENS)} index tokens")

        def on_open(ws):
            kite_state["ws_connected"] = True
            kite_state["ws_obj"]       = ws
            print("[KITE WS] ✅ Connected")
            _subscribe(ws)

        def on_message(ws, message):
            if isinstance(message, bytes):
                _parse_kite_binary(message, kite_state)
            else:
                try:
                    d = json.loads(message)
                    if d.get("type") == "order":
                        print(f"[KITE ORDER UPDATE] {d.get('data', {})}")
                except:
                    pass

        def on_close(ws, *args):
            kite_state["ws_connected"] = False
            kite_state["ws_obj"]       = None
            print("[KITE WS] Disconnected — retrying in 15s")
            time.sleep(15)
            if kite_state.get("access_token"):
                threading.Thread(
                    target=start_kite_ws, args=(kite_state,), daemon=True
                ).start()

        def on_error(ws, error):
            print(f"[KITE WS] Error: {error}")

        ws_lib.WebSocketApp(
            WS_URL,
            on_open=on_open, on_message=on_message,
            on_close=on_close, on_error=on_error,
        ).run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

    except Exception as e:
        print(f"[KITE WS] Start error: {e}")

def kite_ws_subscribe(tokens: list, kite_state: dict):
    new = [t for t in tokens if t not in kite_state.get("subscribed", set())]
    if not new: return
    try:
        ws = kite_state.get("ws_obj")
        if ws:
            ws.send(json.dumps({"a": "subscribe", "v": new}))
            ws.send(json.dumps({"a": "mode", "v": ["full", new]}))
            kite_state["subscribed"].update(new)
    except Exception as e:
        print(f"[KITE WS SUBSCRIBE] {e}")


# ═══════════════════════════════════════════════════════════
#  REST — QUOTES & LTP
# ═══════════════════════════════════════════════════════════

def kite_quotes(exchange_symbols: list, kite_state: dict) -> dict:
    if not kite_state.get("access_token"): return {}
    try:
        qs = "&".join(f"i={s}" for s in exchange_symbols)
        r  = requests.get(f"https://api.kite.trade/quote?{qs}",
                          headers=_kh(kite_state), timeout=10)
        d  = r.json()
        result = {}
        if d.get("status") == "success":
            for key, val in d.get("data", {}).items():
                ltp  = val.get("last_price", 0)
                prev = val.get("ohlc", {}).get("close", ltp)
                chg  = round(ltp - prev, 2)
                pchg = round((chg / prev * 100) if prev else 0, 2)
                ohlc = val.get("ohlc", {})
                result[key] = {
                    "last": ltp, "ltp": ltp,
                    "open": ohlc.get("open", 0),
                    "high": ohlc.get("high", 0), "low": ohlc.get("low", 0),
                    "close": prev, "volume": val.get("volume", 0),
                    "oi": val.get("oi", 0), "ts": time.time(),
                    "change": chg, "pChange": pchg,
                }
                tok = val.get("instrument_token")
                if tok:
                    kite_state["ltp_cache"][tok] = {"ltp": ltp, "ts": time.time()}
                kite_state["ltp_sym"][key] = {"ltp": ltp, "ts": time.time()}
        return result
    except Exception as e:
        print(f"[KITE QUOTES] {e}"); return {}

def kite_ltp_rest(exchange_symbols: list, kite_state: dict) -> dict:
    if not kite_state.get("access_token"): return {}
    try:
        qs = "&".join(f"i={s}" for s in exchange_symbols)
        r  = requests.get(f"https://api.kite.trade/quote/ltp?{qs}",
                          headers=_kh(kite_state), timeout=8)
        d  = r.json()
        result = {}
        if d.get("status") == "success":
            for key, val in d.get("data", {}).items():
                ltp = val.get("last_price", 0)
                result[key] = ltp
                kite_state["ltp_sym"][key] = {"ltp": ltp, "ts": time.time()}
                tok = val.get("instrument_token")
                if tok:
                    kite_state["ltp_cache"][tok] = {"ltp": ltp, "ts": time.time()}
        return result
    except Exception as e:
        print(f"[KITE LTP] {e}"); return {}

def get_index_ltp(name: str, kite_state: dict) -> float:
    key = f"NSE:{name}"
    c = kite_state["ltp_sym"].get(key)
    if c and time.time() - c["ts"] < 5: return c["ltp"]
    tok = KITE_INDEX_TOKENS.get(name)
    if tok:
        c = kite_state["ltp_cache"].get(tok)
        if c and time.time() - c["ts"] < 5: return c["ltp"]
    if kite_state.get("access_token"):
        r = kite_ltp_rest([key], kite_state)
        return r.get(key, 0)
    return 0

def get_option_ltp(symbol: str, strike, otype: str,
                   expiry_str: str, kite_state: dict) -> float:
    if not kite_state.get("access_token"): return 0
    es = _option_sym(symbol, strike, otype, expiry_str)
    if not es: return 0
    r = kite_ltp_rest([es], kite_state)
    return r.get(es, 0)

def fetch_live_ltp(symbol: str, strike_ce, strike_pe,
                   expiry: str, kite_state: dict) -> dict:
    idx_map = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK",
               "FINNIFTY": "NIFTY FIN SERVICE"}
    spot   = get_index_ltp(idx_map.get(symbol, "NIFTY 50"), kite_state)
    ce_ltp = get_option_ltp(symbol, strike_ce, "CE", expiry, kite_state) if strike_ce else 0
    pe_ltp = get_option_ltp(symbol, strike_pe, "PE", expiry, kite_state) if strike_pe else 0
    src    = "kite_ws" if kite_state.get("ws_connected") else "kite_rest"
    return {"spot_ltp": spot, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp, "source": src}


# ═══════════════════════════════════════════════════════════
#  ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════

def _option_sym(symbol: str, strike, otype: str, expiry_str: str) -> str | None:
    try:
        exp = datetime.datetime.strptime(expiry_str, "%d-%b-%Y")
        return f"NFO:{symbol}{exp.strftime('%y%b').upper()}{int(strike)}{otype}"
    except:
        return None

def kite_place_order(params: dict, kite_state: dict) -> dict:
    if not kite_state.get("access_token"): return {"error": "Kite not connected"}
    try:
        variety = params.pop("variety", "regular")
        r = requests.post(
            f"https://api.kite.trade/orders/{variety}",
            data=params, headers=_kh(kite_state), timeout=15,
        )
        d = r.json()
        if d.get("status") == "success":
            oid = d.get("data", {}).get("order_id", "")
            print(f"[KITE ORDER] ✅ Placed {oid}")
            return {"order_id": oid, "status": "success"}
        err = d.get("message", str(d))
        print(f"[KITE ORDER] ❌ Failed: {err}")
        return {"error": err}
    except Exception as e:
        return {"error": str(e)}

def kite_modify_order(order_id: str, params: dict, kite_state: dict) -> dict:
    if not kite_state.get("access_token"): return {"error": "Not connected"}
    try:
        variety = params.pop("variety", "regular")
        r = requests.put(
            f"https://api.kite.trade/orders/{variety}/{order_id}",
            data=params, headers=_kh(kite_state), timeout=10,
        )
        d = r.json()
        return ({"order_id": order_id, "status": "success"}
                if d.get("status") == "success"
                else {"error": d.get("message")})
    except Exception as e:
        return {"error": str(e)}

def kite_cancel_order(order_id: str, kite_state: dict,
                      variety: str = "regular") -> dict:
    if not kite_state.get("access_token"): return {"error": "Not connected"}
    try:
        r = requests.delete(
            f"https://api.kite.trade/orders/{variety}/{order_id}",
            headers=_kh(kite_state), timeout=10,
        )
        d = r.json()
        return ({"status": "cancelled"}
                if d.get("status") == "success"
                else {"error": d.get("message")})
    except Exception as e:
        return {"error": str(e)}

def kite_get_orders(kite_state: dict) -> list:
    if not kite_state.get("access_token"): return []
    try:
        r = requests.get("https://api.kite.trade/orders",
                         headers=_kh(kite_state), timeout=10)
        d = r.json()
        if d.get("status") != "success":
            return []
        orders = d.get("data", [])
        enriched = []
        for o in orders:
            avg    = float(o.get("average_price") or 0)
            ltp    = float(o.get("last_price") or 0)
            filled = int(o.get("filled_quantity") or 0)
            side   = (o.get("transaction_type") or "BUY").upper()
            pnl    = None
            if avg > 0 and ltp > 0 and filled > 0:
                pnl = round((ltp - avg) * filled * (1 if side == "BUY" else -1), 2)
            o["computed_pnl"]    = pnl
            o["filled_quantity"] = filled
            enriched.append(o)
        enriched.sort(key=lambda x: x.get("order_timestamp", ""), reverse=True)
        return enriched
    except Exception as e:
        print(f"[ORDERS] {e}")
        return []

def kite_get_positions(kite_state: dict) -> dict:
    if not kite_state.get("access_token"): return {}
    try:
        r = requests.get("https://api.kite.trade/portfolio/positions",
                         headers=_kh(kite_state), timeout=10)
        d = r.json()
        return d.get("data", {}) if d.get("status") == "success" else {}
    except:
        return {}

def kite_get_margins(kite_state: dict) -> dict:
    if not kite_state.get("access_token"): return {}
    try:
        r = requests.get("https://api.kite.trade/user/margins",
                         headers=_kh(kite_state), timeout=10)
        d = r.json()
        return d.get("data", {}) if d.get("status") == "success" else {}
    except:
        return {}

def build_order_params(pos: dict) -> list:
    action = pos.get("action", "BUY CE")
    sym    = pos.get("symbol", "NIFTY")
    exp    = pos.get("expiry", "")
    lots   = pos.get("lots", 1)
    qty    = lots * LOT_SIZES.get(sym, 75)
    orders = []
    if "CE" in action and pos.get("ce_strike"):
        es = _option_sym(sym, pos["ce_strike"], "CE", exp)
        if es:
            orders.append({
                "tradingsymbol": es.replace("NFO:", ""), "exchange": "NFO",
                "transaction_type": "BUY", "order_type": "MARKET",
                "quantity": qty, "product": "MIS", "validity": "DAY",
                "variety": "regular", "tag": "QB_CE",
            })
    if "PE" in action and pos.get("pe_strike"):
        es = _option_sym(sym, pos["pe_strike"], "PE", exp)
        if es:
            orders.append({
                "tradingsymbol": es.replace("NFO:", ""), "exchange": "NFO",
                "transaction_type": "BUY", "order_type": "MARKET",
                "quantity": qty, "product": "MIS", "validity": "DAY",
                "variety": "regular", "tag": "QB_PE",
            })
    return orders
