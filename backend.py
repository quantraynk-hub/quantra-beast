from flask import Flask, jsonify
from flask_cors import CORS
import requests
import datetime
import time

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────
#  NSE SESSION — mimics a real browser
# ─────────────────────────────────────────
def get_nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    })
    try:
        # Step 1: Visit homepage to get cookies
        s.get("https://www.nseindia.com", timeout=12)
        time.sleep(1)
        # Step 2: Visit market data page to warm up session
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=12)
        time.sleep(0.5)
        # Update headers for API calls
        s.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
    except Exception as e:
        print(f"Session warmup error: {e}")
    return s

# ─────────────────────────────────────────
#  FETCH INDICES
# ─────────────────────────────────────────
def fetch_indices():
    try:
        s = get_nse_session()
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=15)
        r.raise_for_status()
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
        print(f"Indices error: {e}")
        return {"error": str(e)}

# ─────────────────────────────────────────
#  FETCH OPTION CHAIN
# ─────────────────────────────────────────
def fetch_option_chain(symbol="NIFTY"):
    try:
        s = get_nse_session()
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        r = s.get(url, timeout=20)
        r.raise_for_status()

        # Check if response is actually JSON
        if "<!doctype" in r.text.lower() or "<html" in r.text.lower():
            return {"error": "NSE returned HTML instead of JSON — session blocked"}

        raw = r.json()
        records = raw.get("records", {})
        spot = records.get("underlyingValue", 0)
        expiry_dates = records.get("expiryDates", [])
        nearest_expiry = expiry_dates[0] if expiry_dates else None

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

        chain_data.sort(key=lambda x: x["strike"])
        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0
        max_pain = calculate_max_pain(chain_data)
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
        print(f"Option chain error: {e}")
        return {"error": str(e)}

# ─────────────────────────────────────────
#  MAX PAIN
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
#  OI BUILDUP
# ─────────────────────────────────────────
def analyze_oi_buildup(chain, side):
    key_chg = f"{side}_oi_change"
    sorted_chain = sorted(chain, key=lambda x: x.get(key_chg, 0), reverse=True)
    top = sorted_chain[:3] if sorted_chain else []
    return [{"strike": x["strike"], "oi_change": x.get(key_chg, 0)} for x in top]

# ─────────────────────────────────────────
#  FII / DII
# ─────────────────────────────────────────
def fetch_fii_dii():
    try:
        s = get_nse_session()
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=15)
        r.raise_for_status()
        if "<!doctype" in r.text.lower():
            return {"fii_net": 0, "dii_net": 0, "error": "blocked"}
        data = r.json()
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
        return {"fii_net": 0, "dii_net": 0, "error": str(e)}

# ─────────────────────────────────────────
#  SIGNAL ENGINE
# ─────────────────────────────────────────
def run_signal_engine(indices, option_data, fii_dii):
    scores = {}
    reasons = []

    spot    = option_data.get("spot", 0)
    vix     = indices.get("INDIA VIX", {}).get("last", 15)
    pcr     = option_data.get("pcr", 1.0)
    max_pain = option_data.get("max_pain", spot)
    fii_net = fii_dii.get("fii_net", 0)
    dii_net = fii_dii.get("dii_net", 0)
    nifty   = indices.get("NIFTY 50", {})
    pchange = nifty.get("pChange", 0)
    high    = nifty.get("high", spot)
    low     = nifty.get("low", spot)

    # 1. PCR
    if pcr > 1.3:
        scores["options_chain"] = 80
        reasons.append(f"PCR {pcr} > 1.3 — Heavy PUT writing — Strong BULLISH support")
    elif pcr > 1.0:
        scores["options_chain"] = 50
        reasons.append(f"PCR {pcr} — Moderate bullish (1.0–1.3 zone)")
    elif pcr > 0.8:
        scores["options_chain"] = -30
        reasons.append(f"PCR {pcr} — Neutral to bearish, CALL writing dominant")
    else:
        scores["options_chain"] = -75
        reasons.append(f"PCR {pcr} < 0.8 — Heavy CALL writing — BEARISH resistance")

    # 2. Max Pain
    pain_diff = (spot - max_pain) / max_pain * 100 if max_pain > 0 else 0
    if pain_diff < -0.5:
        scores["max_pain"] = 65
        reasons.append(f"Spot BELOW max pain {max_pain} by {abs(pain_diff):.2f}% — Upward drift expected")
    elif pain_diff > 0.5:
        scores["max_pain"] = -65
        reasons.append(f"Spot ABOVE max pain {max_pain} by {pain_diff:.2f}% — Downward pull likely")
    else:
        scores["max_pain"] = 0
        reasons.append(f"Spot near max pain {max_pain} — Range bound, wait for breakout")

    # 3. VIX
    if vix < 13:
        scores["volatility"] = 60
        reasons.append(f"VIX {vix} very LOW — Cheap premiums, buy directional")
    elif vix < 18:
        scores["volatility"] = 20
        reasons.append(f"VIX {vix} normal (13–18) — Standard premium, proceed")
    elif vix < 22:
        scores["volatility"] = -40
        reasons.append(f"VIX {vix} elevated — Caution, expensive premiums")
    else:
        scores["volatility"] = -80
        reasons.append(f"VIX {vix} HIGH — Fear spike, BEARISH or sell premium only")

    # 4. FII Flow
    if fii_net > 1000:
        scores["inst_flow"] = 85
        reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr — Strong institutional BULL signal")
    elif fii_net > 0:
        scores["inst_flow"] = 40
        reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr — Moderate support")
    elif fii_net > -1000:
        scores["inst_flow"] = -35
        reasons.append(f"FII NET SELL ₹{abs(fii_net):.0f}Cr — Mild selling pressure")
    else:
        scores["inst_flow"] = -80
        reasons.append(f"FII NET SELL ₹{abs(fii_net):.0f}Cr — Heavy institutional selling")
    if dii_net > 500:
        scores["inst_flow"] = scores["inst_flow"] + 15
        reasons.append(f"DII also buying ₹{dii_net:.0f}Cr — Double institutional support")

    # 5. OI Buildup
    ce_buildup = option_data.get("ce_buildup", [])
    pe_buildup = option_data.get("pe_buildup", [])
    ce_add = sum(x.get("oi_change", 0) for x in ce_buildup)
    pe_add = sum(x.get("oi_change", 0) for x in pe_buildup)
    if pe_add > ce_add * 1.5:
        scores["oi_buildup"] = 70
        reasons.append(f"PE OI addition {pe_add:,.0f} >> CE {ce_add:,.0f} — PUT writing = BULLISH")
    elif ce_add > pe_add * 1.5:
        scores["oi_buildup"] = -70
        reasons.append(f"CE OI addition {ce_add:,.0f} >> PE {pe_add:,.0f} — CALL writing = BEARISH cap")
    else:
        scores["oi_buildup"] = 0
        reasons.append("OI buildup balanced — sideways market likely")

    # 6. Trend (price change)
    if pchange > 0.5:
        scores["trend"] = 75
        reasons.append(f"NIFTY up {pchange:.2f}% today — Bullish intraday trend")
    elif pchange > 0:
        scores["trend"] = 30
        reasons.append(f"NIFTY slightly up {pchange:.2f}% — Mild bullish")
    elif pchange > -0.5:
        scores["trend"] = -30
        reasons.append(f"NIFTY slightly down {pchange:.2f}% — Mild bearish pressure")
    else:
        scores["trend"] = -75
        reasons.append(f"NIFTY down {pchange:.2f}% today — Clear bearish trend")

    # 7. Day range position
    hl = high - low
    if hl > 0:
        pos = (spot - low) / hl * 100
        if pos > 70:
            scores["momentum"] = 65
            reasons.append(f"Spot at {pos:.0f}% of day range — Strong upward momentum")
        elif pos > 50:
            scores["momentum"] = 25
            reasons.append(f"Spot at {pos:.0f}% of day range — Mild bullish momentum")
        elif pos > 30:
            scores["momentum"] = -25
            reasons.append(f"Spot at {pos:.0f}% of day range — Mild bearish")
        else:
            scores["momentum"] = -65
            reasons.append(f"Spot at {pos:.0f}% of day range — Strong downward momentum")
    else:
        scores["momentum"] = 0

    weights = {
        "options_chain": 0.20,
        "max_pain":       0.15,
        "volatility":     0.12,
        "inst_flow":      0.20,
        "oi_buildup":     0.18,
        "trend":          0.10,
        "momentum":       0.05,
    }

    composite = sum(scores.get(k, 0) * weights[k] for k in weights)
    confidence = min(95, max(40, abs(composite)))

    if composite > 20:
        action = "BUY CE"
        cls = "bull"
        sub = "STRONG BULLISH" if composite > 50 else "MILD BULLISH — BUY CE WITH CAUTION"
    elif composite < -20:
        action = "BUY PE"
        cls = "bear"
        sub = "STRONG BEARISH" if composite < -50 else "MILD BEARISH — BUY PE WITH CAUTION"
    else:
        action = "BUY CE + PE"
        cls = "hedge"
        sub = "SIDEWAYS MARKET — STRADDLE (BUY BOTH)"

    return {
        "action": action,
        "signal_cls": cls,
        "signal_sub": sub,
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

    ce_price, pe_price = 0, 0
    for c in chain:
        if c["strike"] == atm:
            ce_price = c.get("ce_ltp", 0)
            pe_price = c.get("pe_ltp", 0)
            break

    if ce_price == 0: ce_price = round(spot * 0.006, 1)
    if pe_price == 0: pe_price = round(spot * 0.005, 1)

    sl_pct = 0.40
    risk_amount = capital * (risk_pct / 100)
    price_for_calc = ce_price if action == "BUY CE" else pe_price if action == "BUY PE" else (ce_price + pe_price) / 2
    per_lot_sl = price_for_calc * sl_pct * lot_size
    lots = max(1, int(risk_amount / per_lot_sl)) if per_lot_sl > 0 else 1

    multiplier = 2 if action == "BUY CE + PE" else 1
    capital_used = lots * margin * multiplier
    max_loss     = round(lots * price_for_calc * lot_size * sl_pct * multiplier)
    target_pnl   = round(max_loss * rr_ratio)

    return {
        "action": action,
        "ce_strike": atm if action in ["BUY CE", "BUY CE + PE"] else None,
        "pe_strike": atm if action in ["BUY PE", "BUY CE + PE"] else None,
        "ce_price": ce_price,
        "pe_price": pe_price,
        "ce_target": round(ce_price * (1 + rr_ratio * sl_pct), 1),
        "ce_sl":     round(ce_price * (1 - sl_pct), 1),
        "pe_target": round(pe_price * (1 + rr_ratio * sl_pct), 1),
        "pe_sl":     round(pe_price * (1 - sl_pct), 1),
        "lots": lots,
        "lot_size": lot_size,
        "capital_used": int(capital_used),
        "max_loss":     int(max_loss),
        "target_pnl":   int(target_pnl),
        "expiry": option_data.get("expiry", ""),
        "support": option_data.get("max_pain", atm - 100),
        "resistance": atm + 150,
        "spot": spot,
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
    return jsonify({
        "signal": sig,
        "trade": trade,
        "option_data": {k: v for k, v in oc.items() if k != "chain"},
        "indices": idx,
        "fii_dii": fii,
        "chain": oc.get("chain", []),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
