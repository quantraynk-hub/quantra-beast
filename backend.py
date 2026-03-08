from flask import Flask, jsonify
from flask_cors import CORS
import requests
import json
import math
import datetime

app = Flask(__name__)
CORS(app)  # Allow all origins — your HTML can call this freely

# ─────────────────────────────────────────
#  NSE SESSION  (handles cookies + headers)
# ─────────────────────────────────────────
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

def get_nse_session():
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=10)
    except:
        pass
    return s

# ─────────────────────────────────────────
#  FETCH: All Indices (Nifty, BankNifty, VIX)
# ─────────────────────────────────────────
def fetch_indices():
    try:
        s = get_nse_session()
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=10)
        data = r.json()
        result = {}
        for item in data.get("data", []):
            name = item.get("indexSymbol", "")
            if name in ["NIFTY 50", "NIFTY BANK", "INDIA VIX", "NIFTY FIN SERVICE", "NIFTY MIDCAP SELECT"]:
                result[name] = {
                    "last": item.get("last", 0),
                    "change": item.get("variation", 0),
                    "pChange": item.get("percentChange", 0),
                    "open": item.get("open", 0),
                    "high": item.get("high", 0),
                    "low": item.get("low", 0),
                    "previousClose": item.get("previousClose", 0),
                }
        return result
    except Exception as e:
        return {"error": str(e)}

# ─────────────────────────────────────────
#  FETCH: Option Chain
# ─────────────────────────────────────────
def fetch_option_chain(symbol="NIFTY"):
    try:
        s = get_nse_session()
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        r = s.get(url, timeout=15)
        raw = r.json()

        records = raw.get("records", {})
        filtered = raw.get("filtered", {})
        spot = records.get("underlyingValue", 0)
        expiry_dates = records.get("expiryDates", [])
        nearest_expiry = expiry_dates[0] if expiry_dates else None

        # Process chain for nearest expiry
        chain_data = []
        total_ce_oi = 0
        total_pe_oi = 0
        atm_strike = round(spot / 50) * 50

        for item in records.get("data", []):
            if item.get("expiryDate") != nearest_expiry:
                continue

            strike = item.get("strikePrice", 0)
            ce = item.get("CE", {})
            pe = item.get("PE", {})

            ce_oi = ce.get("openInterest", 0)
            pe_oi = pe.get("openInterest", 0)
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi

            chain_data.append({
                "strike": strike,
                "ce_oi": ce_oi,
                "ce_oi_change": ce.get("changeinOpenInterest", 0),
                "ce_ltp": ce.get("lastPrice", 0),
                "ce_iv": ce.get("impliedVolatility", 0),
                "ce_volume": ce.get("totalTradedVolume", 0),
                "pe_oi": pe_oi,
                "pe_oi_change": pe.get("changeinOpenInterest", 0),
                "pe_ltp": pe.get("lastPrice", 0),
                "pe_iv": pe.get("impliedVolatility", 0),
                "pe_volume": pe.get("totalTradedVolume", 0),
            })

        # Sort by strike
        chain_data.sort(key=lambda x: x["strike"])

        # PCR
        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0

        # Max Pain
        max_pain = calculate_max_pain(chain_data)

        # OI buildup signals
        ce_buildup = analyze_oi_buildup(chain_data, "ce")
        pe_buildup = analyze_oi_buildup(chain_data, "pe")

        return {
            "symbol": symbol,
            "spot": spot,
            "expiry": nearest_expiry,
            "atm_strike": atm_strike,
            "pcr": pcr,
            "max_pain": max_pain,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "ce_buildup": ce_buildup,
            "pe_buildup": pe_buildup,
            "chain": chain_data,
        }
    except Exception as e:
        return {"error": str(e)}

# ─────────────────────────────────────────
#  MAX PAIN CALCULATION
# ─────────────────────────────────────────
def calculate_max_pain(chain):
    if not chain:
        return 0
    strikes = [x["strike"] for x in chain]
    min_pain = float("inf")
    max_pain_strike = strikes[0]

    for target in strikes:
        pain = 0
        for c in chain:
            s = c["strike"]
            pain += max(0, target - s) * c["ce_oi"]
            pain += max(0, s - target) * c["pe_oi"]
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = target

    return max_pain_strike

# ─────────────────────────────────────────
#  OI BUILDUP ANALYSIS
# ─────────────────────────────────────────
def analyze_oi_buildup(chain, side):
    # Find strikes with highest OI addition
    key_oi = f"{side}_oi"
    key_chg = f"{side}_oi_change"
    sorted_chain = sorted(chain, key=lambda x: x.get(key_chg, 0), reverse=True)
    top = sorted_chain[:3] if sorted_chain else []
    return [{"strike": x["strike"], "oi_change": x.get(key_chg, 0)} for x in top]

# ─────────────────────────────────────────
#  FETCH: FII / DII Data
# ─────────────────────────────────────────
def fetch_fii_dii():
    try:
        s = get_nse_session()
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        data = r.json()
        result = {"fii_net": 0, "dii_net": 0}
        for item in data:
            cat = item.get("category", "")
            net = float(str(item.get("netValue", "0")).replace(",", ""))
            if "FII" in cat or "FPI" in cat:
                result["fii_net"] += net
            elif "DII" in cat:
                result["dii_net"] += net
        return result
    except Exception as e:
        return {"fii_net": 0, "dii_net": 0, "error": str(e)}

# ─────────────────────────────────────────
#  SIGNAL ENGINE  (Real Logic)
# ─────────────────────────────────────────
def run_signal_engine(indices, option_data, fii_dii):
    scores = {}
    reasons = []

    spot = option_data.get("spot", 0)
    vix = indices.get("INDIA VIX", {}).get("last", 15)
    pcr = option_data.get("pcr", 1.0)
    max_pain = option_data.get("max_pain", spot)
    fii_net = fii_dii.get("fii_net", 0)
    dii_net = fii_dii.get("dii_net", 0)

    # ── 1. PCR ENGINE (Put-Call Ratio)
    # PCR > 1.3 = bullish (heavy put writing = support)
    # PCR < 0.7 = bearish (heavy call writing = resistance)
    if pcr > 1.3:
        scores["options_chain"] = 80
        reasons.append(f"PCR {pcr} > 1.3 → Strong PUT writing → BULLISH support")
    elif pcr > 1.0:
        scores["options_chain"] = 50
        reasons.append(f"PCR {pcr} between 1.0–1.3 → Moderate bullish bias")
    elif pcr > 0.8:
        scores["options_chain"] = -30
        reasons.append(f"PCR {pcr} → Neutral to bearish, CALL writing dominant")
    else:
        scores["options_chain"] = -75
        reasons.append(f"PCR {pcr} < 0.8 → Heavy CALL writing → BEARISH resistance")

    # ── 2. MAX PAIN ENGINE
    # Spot below max pain → likely to drift up (bulls needed)
    # Spot above max pain → likely to drift down (bears)
    pain_diff_pct = (spot - max_pain) / max_pain * 100 if max_pain > 0 else 0
    if pain_diff_pct < -0.5:
        scores["max_pain"] = 65
        reasons.append(f"Spot {spot} BELOW max pain {max_pain} ({pain_diff_pct:.2f}%) → Upward pull likely")
    elif pain_diff_pct > 0.5:
        scores["max_pain"] = -65
        reasons.append(f"Spot {spot} ABOVE max pain {max_pain} (+{pain_diff_pct:.2f}%) → Downward pull likely")
    else:
        scores["max_pain"] = 0
        reasons.append(f"Spot near max pain {max_pain} → Range bound expected")

    # ── 3. VIX ENGINE
    # VIX < 13 = Low vol, trending market, buy directional options
    # VIX 13–18 = Normal, moderate premium
    # VIX > 20 = High fear, buy PE or sell premium
    # VIX > 25 = Extreme fear, sell CE straddle
    if vix < 13:
        scores["volatility"] = 60
        reasons.append(f"India VIX {vix} LOW (<13) → Low IV, cheap options, BUY directional")
    elif vix < 18:
        scores["volatility"] = 20
        reasons.append(f"India VIX {vix} NORMAL (13–18) → Standard premium environment")
    elif vix < 22:
        scores["volatility"] = -40
        reasons.append(f"India VIX {vix} ELEVATED (18–22) → Caution, increased fear")
    else:
        scores["volatility"] = -80
        reasons.append(f"India VIX {vix} HIGH (>{22}) → Fear spike, BEARISH bias or sell premium")

    # ── 4. FII/DII FLOW ENGINE
    if fii_net > 1000:
        scores["inst_flow"] = 85
        reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr → Strong institutional BULL signal")
    elif fii_net > 0:
        scores["inst_flow"] = 40
        reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr → Moderate institutional support")
    elif fii_net > -1000:
        scores["inst_flow"] = -35
        reasons.append(f"FII NET SELL ₹{abs(fii_net):.0f}Cr → Mild selling pressure")
    else:
        scores["inst_flow"] = -80
        reasons.append(f"FII NET SELL ₹{abs(fii_net):.0f}Cr → Heavy INSTITUTIONAL SELLING")

    # DII modifier (DII usually buys when FII sells)
    if dii_net > 500:
        scores["inst_flow"] = scores["inst_flow"] + 15
        reasons.append(f"DII also buying ₹{dii_net:.0f}Cr → Double support")

    # ── 5. OI BUILDUP ENGINE
    ce_buildup = option_data.get("ce_buildup", [])
    pe_buildup = option_data.get("pe_buildup", [])

    ce_total_add = sum(x.get("oi_change", 0) for x in ce_buildup)
    pe_total_add = sum(x.get("oi_change", 0) for x in pe_buildup)

    if pe_total_add > ce_total_add * 1.5:
        scores["oi_buildup"] = 70
        reasons.append(f"PE OI buildup ({pe_total_add:,.0f}) >> CE ({ce_total_add:,.0f}) → PUT writing = BULLISH")
    elif ce_total_add > pe_total_add * 1.5:
        scores["oi_buildup"] = -70
        reasons.append(f"CE OI buildup ({ce_total_add:,.0f}) >> PE ({pe_total_add:,.0f}) → CALL writing = BEARISH")
    else:
        scores["oi_buildup"] = 0
        reasons.append("OI buildup balanced between CE and PE → sideways / wait")

    # ── 6. SPOT vs PREVIOUS CLOSE (Trend)
    nifty = indices.get("NIFTY 50", {})
    prev_close = nifty.get("previousClose", spot)
    pchange = nifty.get("pChange", 0)

    if pchange > 0.5:
        scores["trend"] = 75
        reasons.append(f"NIFTY up {pchange:.2f}% from prev close → Bullish intraday trend")
    elif pchange > 0:
        scores["trend"] = 30
        reasons.append(f"NIFTY mildly up {pchange:.2f}% → Slight bullish bias")
    elif pchange > -0.5:
        scores["trend"] = -30
        reasons.append(f"NIFTY slightly down {pchange:.2f}% → Slight bearish pressure")
    else:
        scores["trend"] = -75
        reasons.append(f"NIFTY down {pchange:.2f}% → Clear bearish intraday trend")

    # ── 7. HIGH-LOW POSITION (Momentum)
    high = nifty.get("high", spot)
    low = nifty.get("low", spot)
    hl_range = high - low
    if hl_range > 0:
        position_pct = (spot - low) / hl_range * 100
        if position_pct > 70:
            scores["momentum"] = 65
            reasons.append(f"Spot at {position_pct:.0f}% of day's range → Strong upward momentum")
        elif position_pct > 50:
            scores["momentum"] = 25
            reasons.append(f"Spot at {position_pct:.0f}% of day's range → Mild bullish")
        elif position_pct > 30:
            scores["momentum"] = -25
            reasons.append(f"Spot at {position_pct:.0f}% of day's range → Mild bearish")
        else:
            scores["momentum"] = -65
            reasons.append(f"Spot at {position_pct:.0f}% of day's range → Downward momentum")
    else:
        scores["momentum"] = 0

    # ── WEIGHTS
    weights = {
        "options_chain": 0.20,
        "max_pain": 0.15,
        "volatility": 0.12,
        "inst_flow": 0.20,
        "oi_buildup": 0.18,
        "trend": 0.10,
        "momentum": 0.05,
    }

    composite = sum(scores.get(k, 0) * weights[k] for k in weights)
    confidence = min(95, max(40, abs(composite)))

    # ── FINAL SIGNAL DECISION
    if composite > 20:
        action = "BUY CE"
        signal_cls = "bull"
        signal_sub = get_bull_sub(composite)
    elif composite < -20:
        action = "BUY PE"
        signal_cls = "bear"
        signal_sub = get_bear_sub(composite)
    else:
        action = "BUY CE + PE"
        signal_cls = "hedge"
        signal_sub = "SIDEWAYS MARKET — HEDGE BOTH SIDES (STRADDLE)"

    return {
        "action": action,
        "signal_cls": signal_cls,
        "signal_sub": signal_sub,
        "composite": round(composite, 2),
        "confidence": round(confidence, 1),
        "scores": scores,
        "reasons": reasons,
        "spot": spot,
        "vix": vix,
        "pcr": pcr,
        "max_pain": max_pain,
        "fii_net": fii_net,
        "dii_net": dii_net,
    }

def get_bull_sub(score):
    if score > 60: return "STRONG BULLISH — HIGH CONVICTION BUY CE"
    if score > 40: return "BULLISH MOMENTUM — BUY CE ON DIPS"
    return "MILD BULLISH BIAS — BUY CE WITH CAUTION"

def get_bear_sub(score):
    if score < -60: return "STRONG BEARISH — HIGH CONVICTION BUY PE"
    if score < -40: return "BEARISH PRESSURE — BUY PE ON BOUNCES"
    return "MILD BEARISH BIAS — BUY PE WITH CAUTION"

# ─────────────────────────────────────────
#  STRIKE + LOT CALCULATOR
# ─────────────────────────────────────────
def calculate_trade(signal, option_data, capital, risk_pct, rr_ratio, index):
    LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 120}
    MARGINS   = {"NIFTY": 6000, "BANKNIFTY": 10000, "FINNIFTY": 5000, "MIDCPNIFTY": 4000}

    lot_size = LOT_SIZES.get(index, 75)
    margin   = MARGINS.get(index, 6000)
    spot     = signal["spot"]
    atm      = option_data.get("atm_strike", round(spot / 50) * 50)
    action   = signal["action"]
    chain    = option_data.get("chain", [])

    # Find ATM option price from real chain
    ce_price, pe_price = 0, 0
    for c in chain:
        if c["strike"] == atm:
            ce_price = c.get("ce_ltp", 0)
            pe_price = c.get("pe_ltp", 0)
            break

    # Fallback estimate if chain missing
    if ce_price == 0: ce_price = round(spot * 0.006, 1)
    if pe_price == 0: pe_price = round(spot * 0.005, 1)

    risk_amount = capital * (risk_pct / 100)
    sl_pct = 0.40  # SL at 40% of premium (industry standard)

    # Lots calculation: risk_amount = lots × price × lot_size × sl_pct
    price_for_calc = ce_price if action == "BUY CE" else pe_price if action == "BUY PE" else (ce_price + pe_price) / 2
    per_lot_sl = price_for_calc * sl_pct * lot_size
    lots = max(1, int(risk_amount / per_lot_sl)) if per_lot_sl > 0 else 1

    multiplier = 2 if action == "BUY CE + PE" else 1
    capital_used = lots * margin * multiplier
    max_loss = round(lots * price_for_calc * lot_size * sl_pct * multiplier, 0)
    target_pnl = round(max_loss * rr_ratio, 0)

    # Target and SL prices
    ce_target = round(ce_price * (1 + rr_ratio * sl_pct), 1) if ce_price else 0
    ce_sl     = round(ce_price * (1 - sl_pct), 1) if ce_price else 0
    pe_target = round(pe_price * (1 + rr_ratio * sl_pct), 1) if pe_price else 0
    pe_sl     = round(pe_price * (1 - sl_pct), 1) if pe_price else 0

    expiry = option_data.get("expiry", "")
    # Support/Resistance from max pain and OI
    support = option_data.get("max_pain", atm - 100)
    resistance = atm + 150 if action != "BUY PE" else atm - 150

    return {
        "action": action,
        "ce_strike": atm if action in ["BUY CE", "BUY CE + PE"] else None,
        "pe_strike": atm if action in ["BUY PE", "BUY CE + PE"] else None,
        "ce_price": ce_price,
        "pe_price": pe_price,
        "ce_target": ce_target,
        "ce_sl": ce_sl,
        "pe_target": pe_target,
        "pe_sl": pe_sl,
        "lots": lots,
        "lot_size": lot_size,
        "capital_used": int(capital_used),
        "max_loss": int(max_loss),
        "target_pnl": int(target_pnl),
        "expiry": expiry,
        "support": support,
        "resistance": resistance,
        "spot": spot,
    }

# ─────────────────────────────────────────
#  API ROUTES
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
    idx = fetch_indices()
    oc  = fetch_option_chain(symbol.upper())
    fii = fetch_fii_dii()
    sig = run_signal_engine(idx, oc, fii)
    trade = calculate_trade(sig, oc, capital, risk, rr, symbol.upper())
    return jsonify({
        "signal": sig,
        "trade": trade,
        "option_data": {k: v for k, v in oc.items() if k != "chain"},
        "indices": idx,
        "fii_dii": fii,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
