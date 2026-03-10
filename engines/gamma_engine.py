"""
GAMMA ENGINE
GEX (Gamma Exposure) calculation.
Positive GEX = market makers stabilise price (range).
Negative GEX = market makers amplify moves (trending).
"""

def run(option_data: dict) -> dict:
    chain = option_data.get("chain", [])
    spot  = option_data.get("spot", 0)
    atm   = option_data.get("atm_strike", round(spot/50)*50)

    if not chain or spot == 0:
        return {"name":"Gamma Engine","score":0,"signal":"NEUTRAL",
                "confidence":0,"reasons":["No data"],"data":{}}

    score   = 0
    reasons = []

    # Approximate GEX: OI × delta_proxy × gamma_proxy
    # For simplicity: strikes near ATM have highest gamma
    # gamma_weight = 1 / (1 + abs(strike - spot) / spot * 20)
    net_gex    = 0
    ce_gex     = 0
    pe_gex     = 0
    atm_ce_oi  = 0
    atm_pe_oi  = 0

    for c in chain:
        dist   = abs(c["strike"] - spot)
        weight = max(0, 1 - dist / (spot * 0.03))  # decays within 3% of spot
        gex_c  =  c["ce_oi"] * weight   # CE gamma exposure (positive = MMs short gamma)
        gex_p  = -c["pe_oi"] * weight   # PE gamma exposure (negative = MMs long gamma)
        net_gex += gex_c + gex_p
        ce_gex  += gex_c
        pe_gex  += gex_p
        if c["strike"] == atm:
            atm_ce_oi = c["ce_oi"]
            atm_pe_oi = c["pe_oi"]

    # Normalize
    if ce_gex + abs(pe_gex) > 0:
        gex_ratio = net_gex / (ce_gex + abs(pe_gex))
    else:
        gex_ratio = 0

    # Key levels analysis
    high_ce_strikes = sorted(chain, key=lambda x: x["ce_oi"], reverse=True)[:2]
    high_pe_strikes = sorted(chain, key=lambda x: x["pe_oi"], reverse=True)[:2]
    nearest_resistance = min([c["strike"] for c in high_ce_strikes if c["strike"] >= spot], default=spot+200)
    nearest_support    = max([c["strike"] for c in high_pe_strikes if c["strike"] <= spot], default=spot-200)

    # ATM OI balance
    if atm_ce_oi + atm_pe_oi > 0:
        atm_bias = (atm_pe_oi - atm_ce_oi) / (atm_pe_oi + atm_ce_oi)
    else:
        atm_bias = 0

    # Score based on GEX direction and ATM OI
    if gex_ratio > 0.3:
        score -= 20  # High positive GEX = range bound, bad for directional
        reasons.append(f"High positive GEX — market makers will pin price, expect range")
    elif gex_ratio < -0.3:
        score += 0   # Negative GEX = trending, could go either way
        reasons.append(f"Negative GEX — market makers amplifying moves, trending likely")
    else:
        reasons.append(f"Neutral GEX — mixed market maker positioning")

    if atm_bias > 0.2:
        score += 25; reasons.append(f"ATM PUT OI > CALL OI — support at {atm} stronger than resistance")
    elif atm_bias < -0.2:
        score -= 25; reasons.append(f"ATM CALL OI > PUT OI — resistance at {atm} stronger than support")
    else:
        reasons.append(f"ATM OI balanced at {atm}")

    # Distance to key levels
    dist_to_res = nearest_resistance - spot
    dist_to_sup = spot - nearest_support
    if dist_to_res < dist_to_sup * 0.5:
        score -= 15; reasons.append(f"Resistance at {nearest_resistance} is close ({dist_to_res:.0f}pts) — limited upside")
    elif dist_to_sup < dist_to_res * 0.5:
        score += 15; reasons.append(f"Support at {nearest_support} is close ({dist_to_sup:.0f}pts) — strong floor")

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name": "Gamma Engine", "score": round(score,2),
        "signal": signal, "confidence": min(95, abs(score)),
        "reasons": reasons,
        "data": {
            "net_gex": round(net_gex,0), "gex_ratio": round(gex_ratio,3),
            "atm_bias": round(atm_bias,3), "atm_ce_oi": atm_ce_oi, "atm_pe_oi": atm_pe_oi,
            "nearest_resistance": nearest_resistance, "nearest_support": nearest_support,
        }
    }
