from flask import Flask, jsonify
from flask_cors import CORS
import requests
import datetime
import time
import threading

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────
#  SELF PING — keeps Render awake forever
# ─────────────────────────────────────────
import os

def self_ping():
    """Pings itself every 10 minutes so Render never sleeps"""
    time.sleep(30)  # wait for server to fully start
    while True:
        try:
            own_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
            requests.get(f"{own_url}/ping", timeout=10)
            print(f"[SELF-PING] Alive at {datetime.datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[SELF-PING] Error: {e}")
        time.sleep(600)  # ping every 10 minutes

# Start self-ping in background thread
ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()

# ─────────────────────────────────────────
#  NSE SESSION — mimics real Chrome browser
# ─────────────────────────────────────────
def get_nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    })
    try:
        # Step 1: hit homepage to get session cookies
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1.5)
        # Step 2: hit market page to warm session
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=15)
        time.sleep(1)
        # Step 3: switch to API headers
        s.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
    except Exception as e:
        print(f"[NSE-SESSION] Warmup error: {e}")
    return s

def is_html(text):
    return "<!doctype" in text.lower() or "<html" in text.lower()

# ─────────────────────────────────────────
#  FETCH INDICES
# ─────────────────────────────────────────
def fetch_indices():
    try:
        s = get_nse_session()
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if is_html(r.text):
            return {"error": "NSE session blocked — retrying later"}
        data = r.json()
        result = {}
        targets = ["NIFTY 50", "NIFTY BANK", "INDIA VIX", "NIFTY FIN SERVICE", "NIFTY MIDCAP SELECT"]
        for item in data.get("data", []):
            name = item.get("indexSymbol", "")
            if name in targets:
                result[name] = {
                    "last":          item.get("last", 0),
                    "change":        item.get("variation", 0),
                    "pChange":       item.get("percentChange", 0),
                    "open":          item.get("open", 0),
                    "high":          item.get("high", 0),
                    "low":           item.get("low", 0),
                    "previousClose": item.get("previousClose", 0),
                }
        return result
    except Exception as e:
        print(f"[INDICES] Error: {e}")
        return {"error": str(e)}

# ─────────────────────────────────────────
#  FETCH OPTION CHAIN
# ─────────────────────────────────────────
def fetch_option_chain(symbol="NIFTY"):
    try:
        s = get_nse_session()
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        r = s.get(url, timeout=20)
        if is_html(r.text):
            return {"error": "NSE option chain blocked", "spot": 0, "pcr": 1, "chain": []}
        raw = r.json()
        records = raw.get("records", {})
        spot    = records.get("underlyingValue", 0)
        expiries = records.get("expiryDates", [])
        nearest  = expiries[0] if expiries else None
        atm      = round(spot / 50) * 50

        chain_data = []
        total_ce_oi = 0
        total_pe_oi = 0

        for item in records.get("data", []):
            if item.get("expiryDate") != nearest:
                continue
            strike = item.get("strikePrice", 0)
            ce = item.get("CE", {})
            pe = item.get("PE", {})
            ce_oi = ce.get("openInterest", 0)
            pe_oi = pe.get("openInterest", 0)
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            chain_data.append({
                "strike":       strike,
                "ce_oi":        ce_oi,
                "ce_oi_change": ce.get("changeinOpenInterest", 0),
                "ce_ltp":       ce.get("lastPrice", 0),
                "ce_iv":        ce.get("impliedVolatility", 0),
                "ce_volume":    ce.get("totalTradedVolume", 0),
                "pe_oi":        pe_oi,
                "pe_oi_change": pe.get("changeinOpenInterest", 0),
                "pe_ltp":       pe.get("lastPrice", 0),
                "pe_iv":        pe.get("impliedVolatility", 0),
                "pe_volume":    pe.get("totalTradedVolume", 0),
            })

        chain_data.sort(key=lambda x: x["strike"])
        pcr      = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0
        max_pain = calculate_max_pain(chain_data)

        return {
            "symbol":       symbol,
            "spot":         spot,
            "expiry":       nearest,
            "atm_strike":   atm,
            "pcr":          pcr,
            "max_pain":     max_pain,
            "total_ce_oi":  total_ce_oi,
            "total_pe_oi":  total_pe_oi,
            "ce_buildup":   analyze_oi_buildup(chain_data, "ce"),
            "pe_buildup":   analyze_oi_buildup(chain_data, "pe"),
            "chain":        chain_data,
        }
    except Exception as e:
        print(f"[OPTION-CHAIN] Error: {e}")
        return {"error": str(e), "spot": 0, "pcr": 1, "chain": []}

# ─────────────────────────────────────────
#  MAX PAIN
# ─────────────────────────────────────────
def calculate_max_pain(chain):
    if not chain:
        return 0
    strikes   = [x["strike"] for x in chain]
    min_pain  = float("inf")
    mp_strike = strikes[0]
    for target in strikes:
        pain = sum(
            max(0, target - c["strike"]) * c["ce_oi"] +
            max(0, c["strike"] - target) * c["pe_oi"]
            for c in chain
        )
        if pain < min_pain:
            min_pain  = pain
            mp_strike = target
    return mp_strike

# ─────────────────────────────────────────
#  OI BUILDUP
# ─────────────────────────────────────────
def analyze_oi_buildup(chain, side):
    key = f"{side}_oi_change"
    top = sorted(chain, key=lambda x: x.get(key, 0), reverse=True)[:3]
    return [{"strike": x["strike"], "oi_change": x.get(key, 0)} for x in top]

# ─────────────────────────────────────────
#  FII / DII
# ─────────────────────────────────────────
def fetch_fii_dii():
    try:
        s = get_nse_session()
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=15)
        if is_html(r.text):
            return {"fii_net": 0, "dii_net": 0}
        data   = r.json()
        result = {"fii_net": 0, "dii_net": 0}
        for item in data:
            cat = item.get("category", "")
            try:
                net = float(str(item.get("netValue", "0")).replace(",", ""))
            except:
                net = 0
            if "FII" in cat or "FPI" in cat:
                result["fii_net"] += net
            elif "DII" in cat:
                result["dii_net"] += net
        return result
    except Exception as e:
        print(f"[FII-DII] Error: {e}")
        return {"fii_net": 0, "dii_net": 0}

# ─────────────────────────────────────────
#  SIGNAL ENGINE — Real Logic
# ─────────────────────────────────────────
def run_signal_engine(indices, option_data, fii_dii):
    scores  = {}
    reasons = []

    spot     = option_data.get("spot", 0)
    vix      = indices.get("INDIA VIX", {}).get("last", 15)
    pcr      = option_data.get("pcr", 1.0)
    max_pain = option_data.get("max_pain", spot)
    fii_net  = fii_dii.get("fii_net", 0)
    dii_net  = fii_dii.get("dii_net", 0)
    nifty    = indices.get("NIFTY 50", {})
    pchange  = nifty.get("pChange", 0)
    high     = nifty.get("high", spot)
    low      = nifty.get("low", spot)

    # ── 1. PCR
    if pcr > 1.3:
        scores["options_chain"] = 80
        reasons.append(f"PCR {pcr} > 1.3 — Heavy PUT writing — Strong BULLISH support")
    elif pcr > 1.0:
        scores["options_chain"] = 50
        reasons.append(f"PCR {pcr} — Moderate bullish bias (1.0–1.3)")
    elif pcr > 0.8:
        scores["options_chain"] = -30
        reasons.append(f"PCR {pcr} — Bearish tilt, CALL writing dominant")
    else:
        scores["options_chain"] = -75
        reasons.append(f"PCR {pcr} < 0.8 — Heavy CALL writing — BEARISH cap")

    # ── 2. Max Pain
    pain_diff = (spot - max_pain) / max_pain * 100 if max_pain > 0 else 0
    if pain_diff < -0.5:
        scores["max_pain"] = 65
        reasons.append(f"Spot BELOW max pain {max_pain} — upward drift expected")
    elif pain_diff > 0.5:
        scores["max_pain"] = -65
        reasons.append(f"Spot ABOVE max pain {max_pain} — downward pull likely")
    else:
        scores["max_pain"] = 0
        reasons.append(f"Spot near max pain {max_pain} — sideways, wait for breakout")

    # ── 3. VIX
    if vix < 13:
        scores["volatility"] = 60
        reasons.append(f"VIX {vix} — Very LOW, cheap options, BUY directional")
    elif vix < 18:
        scores["volatility"] = 20
        reasons.append(f"VIX {vix} — Normal range (13–18), standard premiums")
    elif vix < 22:
        scores["volatility"] = -40
        reasons.append(f"VIX {vix} — Elevated, expensive premiums, use caution")
    else:
        scores["volatility"] = -80
        reasons.append(f"VIX {vix} — HIGH fear, BEARISH signal")

    # ── 4. FII Flow
    if fii_net > 1000:
        scores["inst_flow"] = 85
        reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr — Strong institutional buying")
    elif fii_net > 0:
        scores["inst_flow"] = 40
        reasons.append(f"FII buying ₹{fii_net:.0f}Cr — Moderate support")
    elif fii_net > -1000:
        scores["inst_flow"] = -35
        reasons.append(f"FII selling ₹{abs(fii_net):.0f}Cr — Mild pressure")
    else:
        scores["inst_flow"] = -80
        reasons.append(f"FII NET SELL ₹{abs(fii_net):.0f}Cr — Heavy selling")
    if dii_net > 500:
        scores["inst_flow"] += 15
        reasons.append(f"DII also buying ₹{dii_net:.0f}Cr — Double support")

    # ── 5. OI Buildup
    ce_add = sum(x.get("oi_change", 0) for x in option_data.get("ce_buildup", []))
    pe_add = sum(x.get("oi_change", 0) for x in option_data.get("pe_buildup", []))
    if pe_add > ce_add * 1.5:
        scores["oi_buildup"] = 70
        reasons.append(f"PE OI {pe_add:,.0f} >> CE {ce_add:,.0f} — PUT writing = BULLISH")
    elif ce_add > pe_add * 1.5:
        scores["oi_buildup"] = -70
        reasons.append(f"CE OI {ce_add:,.0f} >> PE {pe_add:,.0f} — CALL writing = BEARISH cap")
    else:
        scores["oi_buildup"] = 0
        reasons.append("OI buildup balanced — sideways likely")

    # ── 6. Trend
    if pchange > 0.5:
        scores["trend"] = 75
        reasons.append(f"NIFTY +{pchange:.2f}% today — Clear bullish intraday trend")
    elif pchange > 0:
        scores["trend"] = 30
        reasons.append(f"NIFTY +{pchange:.2f}% — Slight bullish")
    elif pchange > -0.5:
        scores["trend"] = -30
        reasons.append(f"NIFTY {pchange:.2f}% — Slight bearish pressure")
    else:
        scores["trend"] = -75
        reasons.append(f"NIFTY {pchange:.2f}% — Strong bearish trend")

    # ── 7. Day range position
    hl = high - low
    if hl > 0:
        pos = (spot - low) / hl * 100
        if pos > 70:
            scores["momentum"] = 65
            reasons.append(f"Spot at {pos:.0f}% of day range — strong upward momentum")
        elif pos > 50:
            scores["momentum"] = 25
            reasons.append(f"Spot at {pos:.0f}% of day range — mild bullish")
        elif pos > 30:
            scores["momentum"] = -25
            reasons.append(f"Spot at {pos:.0f}% of day range — mild bearish")
        else:
            scores["momentum"] = -65
            reasons.append(f"Spot at {pos:.0f}% of day range — strong downward momentum")
    else:
        scores["momentum"] = 0

    weights = {
        "options_chain": 0.20,
        "max_pain":      0.15,
        "volatility":    0.12,
        "inst_flow":     0.20,
        "oi_buildup":    0.18,
        "trend":         0.10,
        "momentum":      0.05,
    }

    composite  = sum(scores.get(k, 0) * weights[k] for k in weights)
    confidence = min(95, max(40, abs(composite)))

    if composite > 20:
        action, cls = "BUY CE", "bull"
        sub = "STRONG BULLISH — HIGH CONVICTION" if composite > 50 else "MILD BULLISH — BUY CE WITH CAUTION"
    elif composite < -20:
        action, cls = "BUY PE", "bear"
        sub = "STRONG BEARISH — HIGH CONVICTION" if composite < -50 else "MILD BEARISH — BUY PE WITH CAUTION"
    else:
        action, cls = "BUY CE + PE", "hedge"
        sub = "SIDEWAYS MARKET — STRADDLE (HEDGE BOTH SIDES)"

    return {
        "action": action, "signal_cls": cls, "signal_sub": sub,
        "composite": round(composite, 2), "confidence": round(confidence, 1),
        "scores": scores, "reasons": reasons,
        "spot": spot, "vix": vix, "pcr": pcr,
        "max_pain": max_pain, "fii_net": fii_net, "dii_net": dii_net,
    }

# ─────────────────────────────────────────
#  TRADE CALCULATOR
# ─────────────────────────────────────────
def calculate_trade(signal, option_data, capital, risk_pct, rr_ratio, index):
    LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65}
    MARGINS   = {"NIFTY": 6000, "BANKNIFTY": 10000, "FINNIFTY": 5000}

    lot_size = LOT_SIZES.get(index, 75)
    margin   = MARGINS.get(index, 6000)
    spot     = signal["spot"]
    atm      = option_data.get("atm_strike", round(spot / 50) * 50)
    action   = signal["action"]
    chain    = option_data.get("chain", [])

    ce_price = pe_price = 0
    for c in chain:
        if c["strike"] == atm:
            ce_price = c.get("ce_ltp", 0)
            pe_price = c.get("pe_ltp", 0)
            break

    if ce_price == 0: ce_price = round(spot * 0.006, 1)
    if pe_price == 0: pe_price = round(spot * 0.005, 1)

    sl_pct       = 0.40
    risk_amount  = capital * (risk_pct / 100)
    price_ref    = ce_price if action == "BUY CE" else pe_price if action == "BUY PE" else (ce_price + pe_price) / 2
    per_lot_sl   = price_ref * sl_pct * lot_size
    lots         = max(1, int(risk_amount / per_lot_sl)) if per_lot_sl > 0 else 1
    multiplier   = 2 if action == "BUY CE + PE" else 1
    capital_used = lots * margin * multiplier
    max_loss     = round(lots * price_ref * lot_size * sl_pct * multiplier)
    target_pnl   = round(max_loss * rr_ratio)

    return {
        "action":       action,
        "ce_strike":    atm if action in ["BUY CE", "BUY CE + PE"] else None,
        "pe_strike":    atm if action in ["BUY PE", "BUY CE + PE"] else None,
        "ce_price":     ce_price,
        "pe_price":     pe_price,
        "ce_target":    round(ce_price * (1 + rr_ratio * sl_pct), 1),
        "ce_sl":        round(ce_price * (1 - sl_pct), 1),
        "pe_target":    round(pe_price * (1 + rr_ratio * sl_pct), 1),
        "pe_sl":        round(pe_price * (1 - sl_pct), 1),
        "lots":         lots,
        "lot_size":     lot_size,
        "capital_used": int(capital_used),
        "max_loss":     int(max_loss),
        "target_pnl":   int(target_pnl),
        "expiry":       option_data.get("expiry", ""),
        "support":      option_data.get("max_pain", atm - 100),
        "resistance":   atm + 150,
        "spot":         spot,
    }

# ─────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "time": str(datetime.datetime.now())})

@app.route("/indices")
def indices():
    return jsonify(fetch_indices())

@app.route("/option-chain/<symbol>")
def option_chain(symbol):
    return jsonify(fetch_option_chain(symbol.upper()))

@app.route("/fii-dii")
def fii_dii():
    return jsonify(fetch_fii_dii())

@app.route("/signal/<symbol>")
def signal(symbol):
    idx = fetch_indices()
    oc  = fetch_option_chain(symbol.upper())
    fii = fetch_fii_dii()
    sig = run_signal_engine(idx, oc, fii)
    return jsonify({"signal": sig, "option_data": oc, "indices": idx, "fii_dii": fii})

@app.route("/full-signal/<symbol>/<int:capital>/<float:risk>/<float:rr>")
def full_signal(symbol, capital, risk, rr):
    idx   = fetch_indices()
    oc    = fetch_option_chain(symbol.upper())
    fii   = fetch_fii_dii()
    sig   = run_signal_engine(idx, oc, fii)
    trade = calculate_trade(sig, oc, capital, risk, rr, symbol.upper())
    chain = oc.pop("chain", [])
    return jsonify({
        "signal": sig, "trade": trade,
        "option_data": oc, "indices": idx,
        "fii_dii": fii, "chain": chain,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
