# engines/all_engines.py
# Stage 2 — All 7 Analysis Engines
# Each engine: takes market data → returns score + reasons

from datetime import datetime

# ═══════════════════════════════════════════════════════
#  ENGINE BASE — standard output format
# ═══════════════════════════════════════════════════════
def engine_result(name, score, signal, confidence, reasons, data={}):
    score = max(-100, min(100, score))
    return {
        "engine":     name,
        "score":      round(score, 2),
        "signal":     signal,    # BULL / BEAR / NEUTRAL
        "confidence": round(min(100, max(0, confidence)), 1),
        "reasons":    reasons,
        "data":       data,
        "timestamp":  str(datetime.now()),
    }

def classify(score):
    if score > 20:   return "BULL"
    if score < -20:  return "BEAR"
    return "NEUTRAL"

# ═══════════════════════════════════════════════════════
#  ENGINE 1 — TREND ENGINE
#  Inputs: spot, open, high, low, prev_close, pChange
#  Logic: price action, day range, gap, momentum
# ═══════════════════════════════════════════════════════
def trend_engine(indices):
    reasons = []
    score   = 0

    nifty = indices.get("NIFTY 50", {})
    spot       = nifty.get("last", 0)
    prev_close = nifty.get("previousClose", spot)
    open_p     = nifty.get("open", spot)
    high       = nifty.get("high", spot)
    low        = nifty.get("low", spot)
    pchange    = nifty.get("pChange", 0)

    if spot == 0:
        return engine_result("TREND", 0, "NEUTRAL", 0,
                             ["No spot data available"])

    # 1. Price change from previous close
    if pchange > 1.0:
        score += 40
        reasons.append(f"Strong up move +{pchange:.2f}% from prev close")
    elif pchange > 0.5:
        score += 25
        reasons.append(f"Moderate up move +{pchange:.2f}%")
    elif pchange > 0:
        score += 10
        reasons.append(f"Slight up +{pchange:.2f}%")
    elif pchange > -0.5:
        score -= 10
        reasons.append(f"Slight down {pchange:.2f}%")
    elif pchange > -1.0:
        score -= 25
        reasons.append(f"Moderate down {pchange:.2f}%")
    else:
        score -= 40
        reasons.append(f"Strong down move {pchange:.2f}%")

    # 2. Position in day's range (high-low)
    hl = high - low
    if hl > 0:
        pos = (spot - low) / hl * 100
        if pos > 75:
            score += 30
            reasons.append(f"Spot at {pos:.0f}% of day range — strong upward momentum")
        elif pos > 55:
            score += 15
            reasons.append(f"Spot at {pos:.0f}% of day range — mild bullish")
        elif pos > 45:
            score += 0
            reasons.append(f"Spot at middle of day range — indecisive")
        elif pos > 25:
            score -= 15
            reasons.append(f"Spot at {pos:.0f}% of day range — mild bearish")
        else:
            score -= 30
            reasons.append(f"Spot at {pos:.0f}% of day range — strong downward pressure")

    # 3. Gap detection
    gap_pct = (open_p - prev_close) / prev_close * 100 if prev_close > 0 else 0
    if gap_pct > 0.3:
        score += 15
        reasons.append(f"Gap UP open +{gap_pct:.2f}% — bullish opening")
    elif gap_pct < -0.3:
        score -= 15
        reasons.append(f"Gap DOWN open {gap_pct:.2f}% — bearish opening")

    # 4. Year high/low proximity
    year_high = nifty.get("yearHigh", 0)
    year_low  = nifty.get("yearLow", 0)
    if year_high > 0 and year_low > 0:
        yr_range = year_high - year_low
        yr_pos   = (spot - year_low) / yr_range * 100 if yr_range > 0 else 50
        if yr_pos > 90:
            score += 10
            reasons.append(f"Near 52-week HIGH ({yr_pos:.0f}%) — strong trend")
        elif yr_pos < 15:
            score -= 10
            reasons.append(f"Near 52-week LOW ({yr_pos:.0f}%) — weak trend")

    conf = min(95, abs(score))
    return engine_result("TREND", score, classify(score), conf, reasons, {
        "spot": spot, "pChange": pchange,
        "high": high, "low": low, "open": open_p,
    })


# ═══════════════════════════════════════════════════════
#  ENGINE 2 — OPTIONS ENGINE
#  Inputs: PCR, max pain, OI buildup, IV skew
# ═══════════════════════════════════════════════════════
def options_engine(options):
    reasons = []
    score   = 0

    spot     = options.get("spot", 0)
    pcr      = options.get("pcr", 1.0)
    max_pain = options.get("max_pain", spot)
    iv_skew  = options.get("iv_skew", 0)
    ce_build = options.get("ce_buildup", [])
    pe_build = options.get("pe_buildup", [])

    # 1. PCR analysis
    if pcr > 1.5:
        score += 45
        reasons.append(f"PCR {pcr} very high — extreme PUT writing = strong BULLISH support")
    elif pcr > 1.3:
        score += 35
        reasons.append(f"PCR {pcr} high (>1.3) — PUT writing dominates = BULLISH")
    elif pcr > 1.1:
        score += 20
        reasons.append(f"PCR {pcr} mildly bullish (1.1–1.3)")
    elif pcr > 0.9:
        score -= 5
        reasons.append(f"PCR {pcr} neutral zone (0.9–1.1) — balanced")
    elif pcr > 0.7:
        score -= 25
        reasons.append(f"PCR {pcr} bearish — CALL writing dominant")
    else:
        score -= 45
        reasons.append(f"PCR {pcr} very low — heavy CALL writing = strong BEARISH cap")

    # 2. Max Pain
    if spot > 0 and max_pain > 0:
        diff_pct = (spot - max_pain) / max_pain * 100
        if diff_pct < -1.0:
            score += 30
            reasons.append(f"Spot {abs(diff_pct):.1f}% BELOW max pain {max_pain} — strong upward pull")
        elif diff_pct < -0.3:
            score += 15
            reasons.append(f"Spot slightly below max pain {max_pain} — mild upward bias")
        elif diff_pct > 1.0:
            score -= 30
            reasons.append(f"Spot {diff_pct:.1f}% ABOVE max pain {max_pain} — downward pull")
        elif diff_pct > 0.3:
            score -= 15
            reasons.append(f"Spot slightly above max pain {max_pain} — mild downward bias")
        else:
            reasons.append(f"Spot near max pain {max_pain} — rangebound likely")

    # 3. OI Buildup direction
    ce_add = sum(x.get("oi_change", 0) for x in ce_build)
    pe_add = sum(x.get("oi_change", 0) for x in pe_build)
    if pe_add > 0 and ce_add > 0:
        ratio = pe_add / ce_add if ce_add > 0 else 999
        if ratio > 2:
            score += 25
            reasons.append(f"PE OI building {ratio:.1f}x faster than CE — PUT writing = BULLISH")
        elif ratio < 0.5:
            score -= 25
            reasons.append(f"CE OI building {1/ratio:.1f}x faster than PE — CALL writing = BEARISH cap")

    # 4. IV Skew
    if iv_skew > 3:
        score -= 10
        reasons.append(f"Put IV skew +{iv_skew:.1f} — market pricing downside fear")
    elif iv_skew < -3:
        score += 10
        reasons.append(f"Call IV skew {iv_skew:.1f} — market calm, upside expected")

    conf = min(95, abs(score))
    return engine_result("OPTIONS", score, classify(score), conf, reasons, {
        "pcr": pcr, "max_pain": max_pain, "iv_skew": iv_skew,
        "ce_oi_add": ce_add, "pe_oi_add": pe_add,
    })


# ═══════════════════════════════════════════════════════
#  ENGINE 3 — GAMMA ENGINE
#  GEX = Open Interest × Gamma proxy × Lot Size
#  Positive GEX → market makers pin (low volatility)
#  Negative GEX → market makers amplify (big moves)
# ═══════════════════════════════════════════════════════
def gamma_engine(options):
    reasons = []
    score   = 0

    chain    = options.get("chain", [])
    spot     = options.get("spot", 0)
    lot_size = 75  # NIFTY default

    if not chain or spot == 0:
        return engine_result("GAMMA", 0, "NEUTRAL", 0, ["No chain data"])

    # Calculate simplified GEX per strike
    # Gamma proxy = 1 / (strike distance from spot + 1)²
    net_gex    = 0
    gex_by_str = []

    for c in chain:
        strike = c["strike"]
        dist   = abs(strike - spot)
        # Gamma approximation (highest near ATM, falls off)
        gamma_proxy = 1 / ((dist / spot * 100 + 0.5) ** 2) if spot > 0 else 0
        ce_gex =  c.get("ce_oi", 0) * gamma_proxy * lot_size  # MMs are short CE
        pe_gex = -c.get("pe_oi", 0) * gamma_proxy * lot_size  # MMs are long PE
        strike_gex = ce_gex + pe_gex
        net_gex += strike_gex
        gex_by_str.append({"strike": strike, "gex": round(strike_gex, 2)})

    # Normalize
    gex_norm = net_gex / 1e6 if net_gex != 0 else 0

    if gex_norm > 5:
        score = 20
        reasons.append(f"HIGH positive GEX ({gex_norm:.1f}) — market makers PINNING price, low volatility expected")
        reasons.append("Best for: premium selling, not directional buying")
    elif gex_norm > 1:
        score = 10
        reasons.append(f"Moderate positive GEX ({gex_norm:.1f}) — mild pinning effect")
    elif gex_norm > -1:
        score = 0
        reasons.append(f"Flat GEX near zero — no dominant market maker effect")
    elif gex_norm > -5:
        score = -10
        reasons.append(f"Negative GEX ({gex_norm:.1f}) — market makers will AMPLIFY moves (volatile)")
    else:
        score = -20
        reasons.append(f"HIGH negative GEX ({gex_norm:.1f}) — explosive move likely, high conviction directional trade")

    # Find largest GEX walls (support/resistance)
    top_pos = sorted(gex_by_str, key=lambda x: x["gex"], reverse=True)[:2]
    top_neg = sorted(gex_by_str, key=lambda x: x["gex"])[:2]
    for g in top_pos:
        if g["gex"] > 0:
            reasons.append(f"GEX support wall at {g['strike']} — strong pin level")
    for g in top_neg:
        if g["gex"] < 0:
            reasons.append(f"GEX negative at {g['strike']} — potential breakout zone")

    conf = min(85, abs(score) * 3)
    return engine_result("GAMMA", score, classify(score), conf, reasons, {
        "net_gex": round(gex_norm, 2),
        "top_support": top_pos,
        "top_breakout": top_neg,
    })


# ═══════════════════════════════════════════════════════
#  ENGINE 4 — VOLATILITY ENGINE
#  Adjusts TRADE TYPE recommendation, not direction
#  Low VIX = buy directional | High VIX = sell premium
# ═══════════════════════════════════════════════════════
def volatility_engine(indices, options):
    reasons = []
    score   = 0

    vix     = indices.get("INDIA VIX", {}).get("last", 15)
    vix_chg = indices.get("INDIA VIX", {}).get("pChange", 0)
    ce_iv   = options.get("ce_iv_atm", 0)
    pe_iv   = options.get("pe_iv_atm", 0)
    avg_iv  = (ce_iv + pe_iv) / 2 if (ce_iv + pe_iv) > 0 else vix

    # 1. VIX level
    if vix < 12:
        score += 30
        reasons.append(f"VIX {vix:.2f} very LOW — cheap options, BUY directional aggressively")
    elif vix < 15:
        score += 15
        reasons.append(f"VIX {vix:.2f} low — good buying environment")
    elif vix < 18:
        score += 5
        reasons.append(f"VIX {vix:.2f} normal — standard conditions, proceed")
    elif vix < 22:
        score -= 20
        reasons.append(f"VIX {vix:.2f} elevated — expensive premiums, reduce size")
    elif vix < 27:
        score -= 40
        reasons.append(f"VIX {vix:.2f} HIGH — fear spike, prefer selling or small PE buy")
    else:
        score -= 60
        reasons.append(f"VIX {vix:.2f} EXTREME — danger zone, do not buy options")

    # 2. VIX direction (rising = bad for buyers)
    if vix_chg > 5:
        score -= 20
        reasons.append(f"VIX rising sharply +{vix_chg:.1f}% — fear increasing, avoid CE buying")
    elif vix_chg > 2:
        score -= 10
        reasons.append(f"VIX rising {vix_chg:.1f}% — slight caution")
    elif vix_chg < -5:
        score += 20
        reasons.append(f"VIX falling {vix_chg:.1f}% — fear subsiding, BULLISH environment")
    elif vix_chg < -2:
        score += 10
        reasons.append(f"VIX easing {vix_chg:.1f}% — good buying conditions")

    # 3. IV vs VIX (IV overpriced?)
    if avg_iv > 0 and vix > 0:
        iv_premium = ((avg_iv - vix) / vix) * 100
        if iv_premium > 20:
            score -= 10
            reasons.append(f"Options overpriced (IV {avg_iv:.1f} vs VIX {vix:.1f}) — consider selling instead")

    conf = min(90, abs(score))
    return engine_result("VOLATILITY", score, classify(score), conf, reasons, {
        "vix": vix, "vix_change": vix_chg,
        "ce_iv": ce_iv, "pe_iv": pe_iv,
    })


# ═══════════════════════════════════════════════════════
#  ENGINE 5 — REGIME ENGINE
#  Classifies current market state
#  Output: TRENDING_BULL / TRENDING_BEAR / RANGING / VOLATILE
# ═══════════════════════════════════════════════════════
def regime_engine(indices, options):
    reasons = []
    score   = 0

    vix     = indices.get("INDIA VIX", {}).get("last", 15)
    vix_chg = indices.get("INDIA VIX", {}).get("pChange", 0)
    pchange = indices.get("NIFTY 50", {}).get("pChange", 0)
    high    = indices.get("NIFTY 50", {}).get("high", 0)
    low     = indices.get("NIFTY 50", {}).get("low", 0)
    spot    = indices.get("NIFTY 50", {}).get("last", 0)
    pcr     = options.get("pcr", 1.0)

    # Day range as % of spot
    day_range_pct = (high - low) / spot * 100 if spot > 0 else 0

    # Classify regime
    if vix < 16 and abs(pchange) > 0.5 and day_range_pct < 1.5:
        regime = "TRENDING"
        score  = 60 if pchange > 0 else -60
        reasons.append(f"TRENDING regime — low VIX {vix:.1f}, clear directional move {pchange:.2f}%")
        reasons.append("Best strategy: BUY CE or BUY PE, directional options work well")

    elif vix > 20 or day_range_pct > 2.0:
        regime = "VOLATILE"
        score  = -20  # penalize directional trades in volatile regime
        reasons.append(f"VOLATILE regime — VIX {vix:.1f}, range {day_range_pct:.1f}%")
        reasons.append("Best strategy: reduce size, prefer straddle or skip")

    elif abs(pchange) < 0.3 and day_range_pct < 1.0:
        regime = "RANGING"
        score  = 0
        reasons.append(f"RANGING regime — spot flat {pchange:.2f}%, tight range {day_range_pct:.1f}%")
        reasons.append("Best strategy: sell premium or wait for breakout")

    else:
        regime = "NORMAL"
        score  = 15 if pchange > 0 else -15
        reasons.append(f"NORMAL regime — standard conditions")

    conf = min(85, abs(score))
    return engine_result("REGIME", score, regime, conf, reasons, {
        "regime": regime, "vix": vix,
        "day_range_pct": round(day_range_pct, 2),
        "pchange": pchange,
    })


# ═══════════════════════════════════════════════════════
#  ENGINE 6 — SENTIMENT ENGINE
#  Inputs: FII/DII flows, advance/decline ratio
# ═══════════════════════════════════════════════════════
def sentiment_engine(fii_dii, breadth):
    reasons = []
    score   = 0

    fii_net = fii_dii.get("fii_net", 0)
    dii_net = fii_dii.get("dii_net", 0)
    advances = breadth.get("advances", 0)
    declines = breadth.get("declines", 0)
    ad_ratio = breadth.get("ad_ratio", 1.0)

    # 1. FII Flow (most important — 60% weight in this engine)
    if fii_net > 2000:
        score += 55
        reasons.append(f"FII MASSIVE BUY ₹{fii_net:.0f}Cr — strong institutional conviction")
    elif fii_net > 1000:
        score += 40
        reasons.append(f"FII heavy buy ₹{fii_net:.0f}Cr — institutional accumulation")
    elif fii_net > 500:
        score += 25
        reasons.append(f"FII buying ₹{fii_net:.0f}Cr — positive institutional flow")
    elif fii_net > 0:
        score += 10
        reasons.append(f"FII mild buy ₹{fii_net:.0f}Cr")
    elif fii_net > -500:
        score -= 10
        reasons.append(f"FII mild sell ₹{abs(fii_net):.0f}Cr")
    elif fii_net > -1000:
        score -= 25
        reasons.append(f"FII selling ₹{abs(fii_net):.0f}Cr — moderate distribution")
    elif fii_net > -2000:
        score -= 40
        reasons.append(f"FII heavy sell ₹{abs(fii_net):.0f}Cr — institutional exit")
    else:
        score -= 55
        reasons.append(f"FII MASSIVE SELL ₹{abs(fii_net):.0f}Cr — panic selling")

    # 2. DII (usually counter to FII, but combined = powerful signal)
    if dii_net > 1000 and fii_net > 0:
        score += 20
        reasons.append(f"DII also buying ₹{dii_net:.0f}Cr — DOUBLE institutional support")
    elif dii_net > 500:
        score += 10
        reasons.append(f"DII support buying ₹{dii_net:.0f}Cr")
    elif dii_net < -1000:
        score -= 15
        reasons.append(f"DII selling ₹{abs(dii_net):.0f}Cr — unusual, caution")

    # 3. Market Breadth
    if ad_ratio > 2.0:
        score += 20
        reasons.append(f"Breadth very strong — {advances} advancing vs {declines} declining")
    elif ad_ratio > 1.5:
        score += 10
        reasons.append(f"Breadth positive A/D ratio {ad_ratio:.1f}")
    elif ad_ratio < 0.5:
        score -= 20
        reasons.append(f"Breadth very weak — only {advances} advancing, {declines} declining")
    elif ad_ratio < 0.8:
        score -= 10
        reasons.append(f"Breadth negative A/D ratio {ad_ratio:.1f}")

    conf = min(95, abs(score))
    return engine_result("SENTIMENT", score, classify(score), conf, reasons, {
        "fii_net": fii_net, "dii_net": dii_net,
        "advances": advances, "declines": declines,
        "ad_ratio": ad_ratio,
    })


# ═══════════════════════════════════════════════════════
#  ENGINE 7 — FLOW ENGINE
#  Detects smart money / institutional positioning
#  from unusual OI changes at specific strikes
# ═══════════════════════════════════════════════════════
def flow_engine(options):
    reasons = []
    score   = 0

    chain    = options.get("chain", [])
    spot     = options.get("spot", 0)
    ce_build = options.get("ce_buildup", [])
    pe_build = options.get("pe_buildup", [])

    if not chain or spot == 0:
        return engine_result("FLOW", 0, "NEUTRAL", 0, ["No chain data"])

    # 1. Check if OI being ADDED or UNWOUND
    # Large OI addition = fresh positioning (conviction)
    # Large OI reduction = covering (uncertainty)
    total_ce_chg = sum(c.get("ce_oi_chg", 0) for c in chain)
    total_pe_chg = sum(c.get("pe_oi_chg", 0) for c in chain)

    if total_pe_chg > 0 and total_ce_chg < 0:
        score += 40
        reasons.append(f"CE OI unwinding + PE OI building — short covering + put writing = BULLISH")
    elif total_pe_chg < 0 and total_ce_chg > 0:
        score -= 40
        reasons.append(f"PE OI unwinding + CE OI building — put covering + call writing = BEARISH cap")
    elif total_pe_chg > 0 and total_ce_chg > 0:
        # Both building — check direction
        if total_pe_chg > total_ce_chg * 1.3:
            score += 25
            reasons.append(f"Both OI building, PE faster — PUT WRITING dominant = BULLISH")
        elif total_ce_chg > total_pe_chg * 1.3:
            score -= 25
            reasons.append(f"Both OI building, CE faster — CALL WRITING dominant = BEARISH")
        else:
            reasons.append("Balanced OI buildup — indecisive market")

    # 2. Identify strong OTM option writing (smart money)
    atm = options.get("atm_strike", round(spot / 50) * 50)
    otm_ce_write = [c for c in chain
                    if c["strike"] > atm + 100
                    and c.get("ce_oi_chg", 0) > 50000]
    otm_pe_write = [c for c in chain
                    if c["strike"] < atm - 100
                    and c.get("pe_oi_chg", 0) > 50000]

    if otm_ce_write:
        score -= 15
        strikes = [c["strike"] for c in otm_ce_write[:2]]
        reasons.append(f"Heavy OTM CALL writing at {strikes} — strong resistance, smart money bearish above")

    if otm_pe_write:
        score += 15
        strikes = [c["strike"] for c in otm_pe_write[:2]]
        reasons.append(f"Heavy OTM PUT writing at {strikes} — strong support, smart money bullish below")

    # 3. Unusual activity near ATM
    atm_data = next((c for c in chain if c["strike"] == atm), None)
    if atm_data:
        atm_ce_chg = atm_data.get("ce_oi_chg", 0)
        atm_pe_chg = atm_data.get("pe_oi_chg", 0)
        if atm_pe_chg > 100000:
            score += 20
            reasons.append(f"Massive ATM PUT writing at {atm} — strong BULLISH signal")
        if atm_ce_chg > 100000:
            score -= 20
            reasons.append(f"Massive ATM CALL writing at {atm} — strong BEARISH cap")

    conf = min(90, abs(score))
    return engine_result("FLOW", score, classify(score), conf, reasons, {
        "total_ce_chg": total_ce_chg,
        "total_pe_chg": total_pe_chg,
        "ce_buildup": ce_build[:3],
        "pe_buildup": pe_build[:3],
    })


# ═══════════════════════════════════════════════════════
#  RUN ALL ENGINES
#  Returns dict of all 7 engine results
# ═══════════════════════════════════════════════════════
def run_all_engines(market_data):
    indices = market_data.get("indices", {})
    options = market_data.get("options", {})
    fii_dii = market_data.get("fii_dii", {})
    breadth = market_data.get("breadth", {})

    results = {
        "trend":      trend_engine(indices),
        "options":    options_engine(options),
        "gamma":      gamma_engine(options),
        "volatility": volatility_engine(indices, options),
        "regime":     regime_engine(indices, options),
        "sentiment":  sentiment_engine(fii_dii, breadth),
        "flow":       flow_engine(options),
    }

    print(f"[ENGINES] Scores: " +
          " | ".join([f"{k.upper()}: {v['score']}" for k, v in results.items()]))

    return results
