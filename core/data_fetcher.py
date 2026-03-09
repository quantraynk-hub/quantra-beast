"""
QUANTRA BEAST v2 — Stage 1: Data Pipeline
Fetches all NSE data via ScraperAPI (residential IPs, never blocked).
"""

import requests, json, time, datetime, os
from typing import Optional

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "620fb119d0569d21a4303effdd303228")
SCRAPER_URL     = "http://api.scraperapi.com"
NSE_BASE        = "https://www.nseindia.com/api"
CACHE_TTL       = 60

_cache = {}

def _cache_get(key):
    e = _cache.get(key)
    if not e or (time.time() - e["ts"]) > CACHE_TTL:
        return None
    return e["data"]

def _cache_set(key, data):
    _cache[key] = {"ts": time.time(), "data": data}

def scraper_get(url, retries=3):
    params = {
        "api_key":      SCRAPER_API_KEY,
        "url":          url,
        "country_code": "in",
        "render":       "false",
        "keep_headers": "true",
    }
    for attempt in range(retries):
        try:
            resp = requests.get(SCRAPER_URL, params=params, timeout=30,
                headers={"Accept": "application/json", "Accept-Language": "en-IN,en;q=0.9"})
            text = resp.text.strip()
            if text.startswith("<") or "<!doctype" in text.lower():
                print(f"[SCRAPER] HTML on attempt {attempt+1}, retrying...")
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[SCRAPER] Error attempt {attempt+1}: {e}")
            time.sleep(2)
    return None

def fetch_indices():
    cached = _cache_get("indices")
    if cached: return cached
    data = scraper_get(f"{NSE_BASE}/allIndices")
    if not data: return {"error": "Failed to fetch indices"}
    result = {}
    WANTED = {"NIFTY 50","NIFTY BANK","INDIA VIX","NIFTY FIN SERVICE","NIFTY MIDCAP SELECT"}
    for item in data.get("data", []):
        name = item.get("indexSymbol","")
        if name in WANTED:
            result[name] = {
                "last":          round(float(item.get("last",0)),2),
                "open":          round(float(item.get("open",0)),2),
                "high":          round(float(item.get("high",0)),2),
                "low":           round(float(item.get("low",0)),2),
                "previousClose": round(float(item.get("previousClose",0)),2),
                "change":        round(float(item.get("variation",0)),2),
                "pChange":       round(float(item.get("percentChange",0)),2),
            }
    _cache_set("indices", result)
    print(f"[DATA] Indices: {list(result.keys())}")
    return result

def fetch_option_chain(symbol="NIFTY"):
    key = f"oc_{symbol}"
    cached = _cache_get(key)
    if cached: return cached
    raw = scraper_get(f"{NSE_BASE}/option-chain-indices?symbol={symbol}")
    if not raw: return {"error": f"Failed {symbol}", "chain": [], "spot": 0}
    records  = raw.get("records", {})
    spot     = float(records.get("underlyingValue", 0))
    expiries = records.get("expiryDates", [])
    if not expiries: return {"error": "No expiries", "chain": [], "spot": spot}
    nearest  = expiries[0]
    step     = 100 if symbol == "BANKNIFTY" else 50
    atm      = round(spot / step) * step
    chain_data, total_ce_oi, total_pe_oi, total_ce_vol, total_pe_vol = [], 0, 0, 0, 0
    for item in records.get("data", []):
        if item.get("expiryDate") != nearest: continue
        strike = item.get("strikePrice", 0)
        ce = item.get("CE", {}); pe = item.get("PE", {})
        ce_oi = float(ce.get("openInterest",0)); pe_oi = float(pe.get("openInterest",0))
        ce_vol= float(ce.get("totalTradedVolume",0)); pe_vol=float(pe.get("totalTradedVolume",0))
        total_ce_oi += ce_oi; total_pe_oi += pe_oi
        total_ce_vol+= ce_vol; total_pe_vol+= pe_vol
        chain_data.append({
            "strike": int(strike), "distance_from_atm": int(strike - atm),
            "ce_oi": int(ce_oi), "ce_oi_change": int(ce.get("changeinOpenInterest",0)),
            "ce_ltp": float(ce.get("lastPrice",0)), "ce_iv": float(ce.get("impliedVolatility",0)),
            "ce_volume": int(ce_vol),
            "pe_oi": int(pe_oi), "pe_oi_change": int(pe.get("changeinOpenInterest",0)),
            "pe_ltp": float(pe.get("lastPrice",0)), "pe_iv": float(pe.get("impliedVolatility",0)),
            "pe_volume": int(pe_vol),
        })
    chain_data.sort(key=lambda x: x["strike"])
    pcr = round(total_pe_oi/total_ce_oi,3) if total_ce_oi > 0 else 1.0
    max_pain = _max_pain(chain_data)
    atm_ce_iv = next((c["ce_iv"] for c in chain_data if c["strike"]==atm), 0)
    atm_pe_iv = next((c["pe_iv"] for c in chain_data if c["strike"]==atm), 0)
    result = {
        "symbol": symbol, "spot": spot, "atm_strike": int(atm),
        "expiry": nearest, "pcr": pcr, "pcr_oi": pcr,
        "pcr_volume": round(total_pe_vol/total_ce_vol,3) if total_ce_vol > 0 else 1.0,
        "total_ce_oi": int(total_ce_oi), "total_pe_oi": int(total_pe_oi),
        "max_pain": int(max_pain), "iv_skew": round(atm_pe_iv-atm_ce_iv,2),
        "atm_ce_iv": atm_ce_iv, "atm_pe_iv": atm_pe_iv,
        "ce_buildup": _oi_buildup(chain_data,"ce"), "pe_buildup": _oi_buildup(chain_data,"pe"),
        "ce_unwind":  _oi_unwind(chain_data,"ce"),  "pe_unwind":  _oi_unwind(chain_data,"pe"),
        "resistance_strikes": sorted([c for c in chain_data if c["ce_oi"]>0],
            key=lambda x:x["ce_oi"],reverse=True)[:3],
        "support_strikes": sorted([c for c in chain_data if c["pe_oi"]>0],
            key=lambda x:x["pe_oi"],reverse=True)[:3],
        "chain": chain_data, "fetched_at": datetime.datetime.now().isoformat(),
    }
    _cache_set(key, result)
    print(f"[DATA] Chain {symbol}: spot={spot} atm={atm} pcr={pcr} strikes={len(chain_data)}")
    return result

def fetch_fii_dii():
    cached = _cache_get("fii_dii")
    if cached: return cached
    data = scraper_get(f"{NSE_BASE}/fiidiiTradeReact")
    if not data: return {"fii_net":0,"dii_net":0}
    r = {"fii_net":0.0,"fii_buy":0.0,"fii_sell":0.0,"dii_net":0.0,"dii_buy":0.0,"dii_sell":0.0}
    for item in data:
        cat = item.get("category","")
        try:
            buy  = float(str(item.get("buyValue","0")).replace(",",""))
            sell = float(str(item.get("sellValue","0")).replace(",",""))
            net  = float(str(item.get("netValue","0")).replace(",",""))
        except: buy=sell=net=0.0
        if "FII" in cat or "FPI" in cat:
            r["fii_buy"]+=buy; r["fii_sell"]+=sell; r["fii_net"]+=net
        elif "DII" in cat:
            r["dii_buy"]+=buy; r["dii_sell"]+=sell; r["dii_net"]+=net
    for k in r: r[k] = round(r[k],2)
    _cache_set("fii_dii", r)
    print(f"[DATA] FII={r['fii_net']}Cr DII={r['dii_net']}Cr")
    return r

def fetch_market_status():
    now = datetime.datetime.now()
    wd  = now.weekday()
    mo  = now.replace(hour=9,minute=15,second=0,microsecond=0)
    mc  = now.replace(hour=15,minute=30,second=0,microsecond=0)
    po  = now.replace(hour=9,minute=0,second=0,microsecond=0)
    if wd >= 5: status,session = "CLOSED","Weekend"
    elif now < po: status,session = "CLOSED","Pre-Market"
    elif now < mo: status,session = "PRE-OPEN","Pre-Open 9:00–9:15"
    elif now <= mc:
        mins = int((mc-now).total_seconds()/60)
        status,session = "OPEN",f"Live | {mins}m to close"
    else: status,session = "CLOSED","After Market"
    return {"status":status,"session":session,"is_open":status=="OPEN",
            "time":now.strftime("%H:%M:%S"),"date":now.strftime("%d %b %Y")}

def fetch_full_snapshot(symbol="NIFTY"):
    print(f"\n[SNAPSHOT] Fetching full data for {symbol}...")
    indices     = fetch_indices()
    option_data = fetch_option_chain(symbol)
    fii_dii     = fetch_fii_dii()
    mkt         = fetch_market_status()
    return {
        "symbol": symbol, "indices": indices, "option_data": option_data,
        "fii_dii": fii_dii, "market_status": mkt,
        "fetched_at": datetime.datetime.now().isoformat(),
        "errors": [f"{k}:{v['error']}" for k,v in {"indices":indices,"option_data":option_data}.items() if "error" in v]
    }

def _max_pain(chain):
    if not chain: return 0
    strikes = [c["strike"] for c in chain]
    best,result = float("inf"),strikes[0]
    for t in strikes:
        pain = sum(max(0,t-c["strike"])*c["ce_oi"]+max(0,c["strike"]-t)*c["pe_oi"] for c in chain)
        if pain < best: best,result = pain,t
    return result

def _oi_buildup(chain,side):
    key=f"{side}_oi_change"
    return [{"strike":c["strike"],"oi_change":c[key],"ltp":c[f"{side}_ltp"]}
            for c in sorted([x for x in chain if x.get(key,0)>0],key=lambda x:x[key],reverse=True)[:3]]

def _oi_unwind(chain,side):
    key=f"{side}_oi_change"
    return [{"strike":c["strike"],"oi_change":c[key],"ltp":c[f"{side}_ltp"]}
            for c in sorted([x for x in chain if x.get(key,0)<0],key=lambda x:x[key])[:3]]
