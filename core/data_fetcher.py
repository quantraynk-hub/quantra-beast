# core/data_fetcher.py
# QUANTRA BEAST v3.2 — Data Pipeline
# Kite Connect PRIMARY + NSE fallback
# All fetch logic extracted from api/server.py v3.2

import requests, time, datetime, os, struct, json

# ── Cache (shared with server) ───────────────────────────
from core.cache import _cget, _cset

# ── NSE Session ──────────────────────────────────────────
_nse_session = None
_nse_session_ts = 0

def _get_nse_session():
    global _nse_session, _nse_session_ts
    if _nse_session and (time.time() - _nse_session_ts) < 300:
        return _nse_session
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://www.nseindia.com/",
        "DNT":             "1",
        "Connection":      "keep-alive",
    })
    try:
        s.get("https://www.nseindia.com/option-chain", timeout=12)
        _nse_session = s
        _nse_session_ts = time.time()
    except Exception as e:
        print(f"[NSE SESSION] Warm-up failed: {e}")
        _nse_session = s
        _nse_session_ts = time.time()
    return s

def nse_get(path: str, retries: int = 3):
    """GET from NSE with session retry and HTML-block detection."""
    global _nse_session_ts
    s = _get_nse_session()
    for i in range(retries):
        try:
            r = s.get(path, timeout=20)
            if r.status_code in (401, 403):
                _nse_session_ts = 0
                s = _get_nse_session()
                continue
            t = r.text.strip()
            if t.startswith("<") or "<!doctype" in t.lower():
                print(f"[NSE] HTML on attempt {i+1} — Render IP blocked")
                _nse_session_ts = 0
                s = _get_nse_session()
                time.sleep(2 * (i + 1))
                continue
            return r.json()
        except Exception as e:
            print(f"[NSE] Error attempt {i+1}: {e}")
            time.sleep(2 * (i + 1))
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  INDEX DATA  (Kite primary)
# ═══════════════════════════════════════════════════════════════════════════════

INDEX_EXCHANGE_SYMS = {
    "NIFTY 50":          "NSE:NIFTY 50",
    "NIFTY BANK":        "NSE:NIFTY BANK",
    "INDIA VIX":         "NSE:INDIA VIX",
    "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
}

def fetch_indices(kite_state: dict) -> dict:
    """Fetch live index data. Uses Kite quotes if connected, else NSE."""
    c = _cget("idx", ttl=10)
    if c:
        return c

    # ── Kite path ──────────────────────────────────────────
    if kite_state.get("access_token"):
        try:
            from core.kite_client import kite_quotes
            syms = list(INDEX_EXCHANGE_SYMS.values())
            raw  = kite_quotes(syms, kite_state)
            if raw:
                result = {}
                for name, exch_sym in INDEX_EXCHANGE_SYMS.items():
                    d = raw.get(exch_sym, {})
                    if d:
                        result[name] = {
                            "last":          d["ltp"],
                            "open":          d["open"],
                            "high":          d["high"],
                            "low":           d["low"],
                            "previousClose": d["close"],
                            "change":        d["change"],
                            "pChange":       d["pChange"],
                        }
                if result:
                    _cset("idx", result)
                    return result
        except Exception as e:
            print(f"[FETCH INDICES] Kite error: {e}")

    # ── NSE fallback ───────────────────────────────────────
    try:
        data = nse_get("https://www.nseindia.com/api/allIndices")
        if data:
            WANTED = {"NIFTY 50", "NIFTY BANK", "INDIA VIX", "NIFTY FIN SERVICE"}
            result = {}
            for item in data.get("data", []):
                name = item.get("indexSymbol", "")
                if name in WANTED:
                    ltp   = float(item.get("last", 0))
                    prev  = float(item.get("previousClose", ltp))
                    chg   = round(ltp - prev, 2)
                    pchg  = round(chg / prev * 100, 2) if prev else 0
                    result[name] = {
                        "last":          ltp,
                        "open":          float(item.get("open", 0)),
                        "high":          float(item.get("high", 0)),
                        "low":           float(item.get("low", 0)),
                        "previousClose": prev,
                        "change":        chg,
                        "pChange":       pchg,
                    }
            if result:
                _cset("idx", result)
                return result
    except Exception as e:
        print(f"[FETCH INDICES] NSE error: {e}")

    return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION CHAIN  (NSE primary, Kite synthetic fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _max_pain(chain):
    if not chain: return 0
    strikes = [c["strike"] for c in chain]
    best, result = float("inf"), strikes[0]
    for t in strikes:
        pain = sum(
            max(0, t - c["strike"]) * c["ce_oi"] +
            max(0, c["strike"] - t) * c["pe_oi"]
            for c in chain
        )
        if pain < best:
            best, result = pain, t
    return result

def _oi_buildup(chain, side):
    key = f"{side}_oi_change"
    return [
        {"strike": c["strike"], "oi_change": c[key], "ltp": c[f"{side}_ltp"]}
        for c in sorted([x for x in chain if x.get(key, 0) > 0],
                        key=lambda x: x[key], reverse=True)[:3]
    ]

def _oi_unwind(chain, side):
    key = f"{side}_oi_change"
    return [
        {"strike": c["strike"], "oi_change": c[key], "ltp": c[f"{side}_ltp"]}
        for c in sorted([x for x in chain if x.get(key, 0) < 0],
                        key=lambda x: x[key])[:3]
    ]

def _get_nearest_expiry_str() -> str:
    """Return nearest weekly expiry (Thursday) as DD-Mon-YYYY."""
    today = datetime.date.today()
    days_to_thu = (3 - today.weekday()) % 7
    if days_to_thu == 0 and datetime.datetime.now().hour >= 15:
        days_to_thu = 7
    expiry = today + datetime.timedelta(days=days_to_thu)
    return expiry.strftime("%d-%b-%Y")

def _build_kite_chain(symbol: str, kite_state: dict) -> dict:
    """Build synthetic option chain from Kite LTP data when NSE is blocked."""
    try:
        from core.kite_client import kite_ltp_rest, get_index_ltp
        idx_map = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE"}
        spot = get_index_ltp(idx_map.get(symbol, "NIFTY 50"), kite_state)
        if not spot:
            return {}

        step = 100 if symbol == "BANKNIFTY" else 50
        atm  = round(spot / step) * step
        exp  = _get_nearest_expiry_str()

        # Fetch option LTPs around ATM (±5 strikes)
        strikes = [atm + i * step for i in range(-5, 6)]
        exp_dt  = datetime.datetime.strptime(exp, "%d-%b-%Y")
        ce_syms = [f"NFO:{symbol}{exp_dt.strftime('%y%b').upper()}{s}CE" for s in strikes]
        pe_syms = [f"NFO:{symbol}{exp_dt.strftime('%y%b').upper()}{s}PE" for s in strikes]

        from core.kite_client import kite_quotes
        ce_prices = kite_quotes(ce_syms, kite_state)
        pe_prices = kite_quotes(pe_syms, kite_state)

        chain_data = []
        for i, strike in enumerate(strikes):
            ce_sym = ce_syms[i]; pe_sym = pe_syms[i]
            chain_data.append({
                "strike":          strike,
                "distance_from_atm": int(strike - atm),
                "ce_oi":           int(ce_prices.get(ce_sym, {}).get("oi", 0)),
                "ce_oi_change":    0,
                "ce_ltp":          float(ce_prices.get(ce_sym, {}).get("ltp", 0)),
                "ce_iv":           0.0,
                "ce_volume":       int(ce_prices.get(ce_sym, {}).get("volume", 0)),
                "pe_oi":           int(pe_prices.get(pe_sym, {}).get("oi", 0)),
                "pe_oi_change":    0,
                "pe_ltp":          float(pe_prices.get(pe_sym, {}).get("ltp", 0)),
                "pe_iv":           0.0,
                "pe_volume":       int(pe_prices.get(pe_sym, {}).get("volume", 0)),
            })

        chain_data.sort(key=lambda x: x["strike"])
        total_ce_oi = sum(c["ce_oi"] for c in chain_data)
        total_pe_oi = sum(c["pe_oi"] for c in chain_data)
        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0

        return {
            "symbol": symbol, "spot": spot, "atm_strike": int(atm),
            "expiry": exp, "pcr": pcr, "pcr_oi": pcr, "pcr_volume": 1.0,
            "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi,
            "max_pain": _max_pain(chain_data), "iv_skew": 0.0,
            "atm_ce_iv": 0.0, "atm_pe_iv": 0.0,
            "ce_buildup": _oi_buildup(chain_data, "ce"),
            "pe_buildup": _oi_buildup(chain_data, "pe"),
            "ce_unwind":  _oi_unwind(chain_data, "ce"),
            "pe_unwind":  _oi_unwind(chain_data, "pe"),
            "resistance_strikes": sorted([c for c in chain_data if c["ce_oi"] > 0],
                key=lambda x: x["ce_oi"], reverse=True)[:3],
            "support_strikes": sorted([c for c in chain_data if c["pe_oi"] > 0],
                key=lambda x: x["pe_oi"], reverse=True)[:3],
            "chain": chain_data,
            "fetched_at": datetime.datetime.now().isoformat(),
            "source": "kite_synthetic",
        }
    except Exception as e:
        print(f"[KITE CHAIN] {e}")
        return {}

def fetch_chain(symbol: str = "NIFTY", kite_state: dict = None) -> dict:
    """Fetch option chain. NSE primary, Kite synthetic fallback."""
    key = f"oc_{symbol}"
    c   = _cget(key, ttl=60)
    if c:
        return c

    kite_state = kite_state or {}

    # ── NSE primary ───────────────────────────────────────
    try:
        raw = nse_get(f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}")
        if raw:
            records  = raw.get("records", {})
            spot     = float(records.get("underlyingValue", 0))
            expiries = records.get("expiryDates", [])
            if expiries and spot:
                nearest = expiries[0]
                step    = 100 if symbol == "BANKNIFTY" else 50
                atm     = round(spot / step) * step
                chain_data, tco, tpo, tcv, tpv = [], 0, 0, 0, 0
                for item in records.get("data", []):
                    if item.get("expiryDate") != nearest: continue
                    strike = item.get("strikePrice", 0)
                    ce = item.get("CE", {}); pe = item.get("PE", {})
                    ce_oi = float(ce.get("openInterest", 0))
                    pe_oi = float(pe.get("openInterest", 0))
                    ce_vol = float(ce.get("totalTradedVolume", 0))
                    pe_vol = float(pe.get("totalTradedVolume", 0))
                    tco += ce_oi; tpo += pe_oi; tcv += ce_vol; tpv += pe_vol
                    chain_data.append({
                        "strike": int(strike),
                        "distance_from_atm": int(strike - atm),
                        "ce_oi": int(ce_oi),
                        "ce_oi_change": int(ce.get("changeinOpenInterest", 0)),
                        "ce_ltp": float(ce.get("lastPrice", 0)),
                        "ce_iv":  float(ce.get("impliedVolatility", 0)),
                        "ce_volume": int(ce_vol),
                        "pe_oi": int(pe_oi),
                        "pe_oi_change": int(pe.get("changeinOpenInterest", 0)),
                        "pe_ltp": float(pe.get("lastPrice", 0)),
                        "pe_iv":  float(pe.get("impliedVolatility", 0)),
                        "pe_volume": int(pe_vol),
                    })
                chain_data.sort(key=lambda x: x["strike"])
                pcr = round(tpo / tco, 3) if tco > 0 else 1.0
                mp  = _max_pain(chain_data)
                atm_ce_iv = next((c["ce_iv"] for c in chain_data if c["strike"] == atm), 0)
                atm_pe_iv = next((c["pe_iv"] for c in chain_data if c["strike"] == atm), 0)

                # Enrich LTPs from Kite if available
                if kite_state.get("access_token"):
                    try:
                        from core.kite_client import kite_quotes
                        near = [c for c in chain_data if abs(c["strike"] - atm) <= 300]
                        exp_dt = datetime.datetime.strptime(nearest, "%d-%b-%Y")
                        syms_ce = [f"NFO:{symbol}{exp_dt.strftime('%y%b').upper()}{c['strike']}CE" for c in near]
                        syms_pe = [f"NFO:{symbol}{exp_dt.strftime('%y%b').upper()}{c['strike']}PE" for c in near]
                        prices  = kite_quotes(syms_ce + syms_pe, kite_state)
                        for c in near:
                            ce_sym = f"NFO:{symbol}{exp_dt.strftime('%y%b').upper()}{c['strike']}CE"
                            pe_sym = f"NFO:{symbol}{exp_dt.strftime('%y%b').upper()}{c['strike']}PE"
                            if prices.get(ce_sym, {}).get("ltp"): c["ce_ltp"] = prices[ce_sym]["ltp"]
                            if prices.get(pe_sym, {}).get("ltp"): c["pe_ltp"] = prices[pe_sym]["ltp"]
                            if prices.get(ce_sym, {}).get("oi"): c["ce_oi"] = int(prices[ce_sym]["oi"])
                            if prices.get(pe_sym, {}).get("oi"): c["pe_oi"] = int(prices[pe_sym]["oi"])
                    except Exception as e:
                        print(f"[CHAIN OI ENRICH] {e}")

                result = {
                    "symbol": symbol, "spot": spot, "atm_strike": int(atm),
                    "expiry": nearest, "pcr": pcr, "pcr_oi": pcr,
                    "pcr_volume": round(tpv / tcv, 3) if tcv > 0 else 1.0,
                    "total_ce_oi": int(tco), "total_pe_oi": int(tpo),
                    "max_pain": int(mp), "iv_skew": round(atm_pe_iv - atm_ce_iv, 2),
                    "atm_ce_iv": atm_ce_iv, "atm_pe_iv": atm_pe_iv,
                    "ce_buildup": _oi_buildup(chain_data, "ce"),
                    "pe_buildup": _oi_buildup(chain_data, "pe"),
                    "ce_unwind":  _oi_unwind(chain_data, "ce"),
                    "pe_unwind":  _oi_unwind(chain_data, "pe"),
                    "resistance_strikes": sorted([c for c in chain_data if c["ce_oi"] > 0],
                        key=lambda x: x["ce_oi"], reverse=True)[:3],
                    "support_strikes": sorted([c for c in chain_data if c["pe_oi"] > 0],
                        key=lambda x: x["pe_oi"], reverse=True)[:3],
                    "chain": chain_data,
                    "fetched_at": datetime.datetime.now().isoformat(),
                    "source": "nse",
                }
                _cset(key, result)
                return result
    except Exception as e:
        print(f"[FETCH CHAIN NSE] {e}")

    # ── Kite synthetic fallback ────────────────────────────
    if kite_state.get("access_token"):
        print(f"[FETCH CHAIN] NSE blocked — building Kite synthetic chain")
        result = _build_kite_chain(symbol, kite_state)
        if result:
            _cset(key, result)
            return result

    print(f"[FETCH CHAIN] All sources failed for {symbol}")
    return {"symbol": symbol, "spot": 0, "chain": [], "error": "All sources failed"}


# ═══════════════════════════════════════════════════════════════════════════════
#  FII/DII DATA
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_fii_dii() -> dict:
    c = _cget("fii", ttl=300)
    if c: return c
    try:
        data = nse_get("https://www.nseindia.com/api/fiidiiTradeReact")
        if data:
            r = {"fii_net": 0.0, "fii_buy": 0.0, "fii_sell": 0.0,
                 "dii_net": 0.0, "dii_buy": 0.0, "dii_sell": 0.0}
            for item in data:
                cat = item.get("category", "")
                try:
                    buy  = float(str(item.get("buyValue",  "0")).replace(",", ""))
                    sell = float(str(item.get("sellValue", "0")).replace(",", ""))
                    net  = float(str(item.get("netValue",  "0")).replace(",", ""))
                except:
                    buy = sell = net = 0.0
                if "FII" in cat or "FPI" in cat:
                    r["fii_buy"] += buy; r["fii_sell"] += sell; r["fii_net"] += net
                elif "DII" in cat:
                    r["dii_buy"] += buy; r["dii_sell"] += sell; r["dii_net"] += net
            for k in r: r[k] = round(r[k], 2)
            _cset("fii", r)
            return r
    except Exception as e:
        print(f"[FII/DII] {e}")
    return {"fii_net": 0, "dii_net": 0, "fii_buy": 0, "fii_sell": 0,
            "dii_buy": 0, "dii_sell": 0}


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_market_status() -> dict:
    now = datetime.datetime.now()
    wd  = now.weekday()
    mo  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    mc  = now.replace(hour=15, minute=30, second=0, microsecond=0)
    po  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    if wd >= 5:
        status, session = "CLOSED", "Weekend"
    elif now < po:
        status, session = "CLOSED", "Pre-Market"
    elif now < mo:
        status, session = "PRE-OPEN", "Pre-Open 9:00–9:15"
    elif now <= mc:
        mins = int((mc - now).total_seconds() / 60)
        status, session = "OPEN", f"Live | {mins}m to close"
    else:
        status, session = "CLOSED", "After Market"
    return {
        "status": status, "session": session, "is_open": status == "OPEN",
        "time": now.strftime("%H:%M:%S"), "date": now.strftime("%d %b %Y"),
    }
