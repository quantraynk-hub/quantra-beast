"""
FLOW ENGINE
Detects unusual OI activity — smart money movement.
Identifies sudden large OI additions at key strikes.
Score: -100 (smart money bearish) to +100 (smart money bullish)
"""

def run(option_data: dict) -> dict:
    chain        = option_data.get("chain", [])
    spot         = option_data.get("spot", 0)
    ce_buildup   = option_data.get("ce_buildup", [])
    pe_buildup   = option_data.get("pe_buildup", [])
    ce_unwind    = option_data.get("ce_unwind", [])
    pe_unwind    = option_data.get("pe_unwind", [])
    total_ce_oi  = option_data.get("total_ce_oi", 1)
    total_pe_oi  = option_data.get("total_pe_oi", 1)

    if not chain or spot == 0:
        return {"name":"Flow Engine","score":0,"signal":"NEUTRAL",
                "confidence":0,"reasons":["No data"],"data":{}}

    score   = 0
    reasons = []

    # 1. PE buildup vs CE buildup (net flow direction) — 40% weight
    pe_add = sum(x.get("oi_change",0) for x in pe_buildup)
    ce_add = sum(x.get("oi_change",0) for x in ce_buildup)
    total_add = pe_add + ce_add
    if total_add > 0:
        pe_flow_pct = pe_add / total_add * 100
        if pe_flow_pct > 70:
            score += 40; reasons.append(f"Smart money writing PUTs ({pe_flow_pct:.0f}% of new OI) — bullish flow")
        elif pe_flow_pct > 55:
            score += 20; reasons.append(f"PE OI addition dominates ({pe_flow_pct:.0f}%) — mild bullish flow")
        elif pe_flow_pct < 30:
            score -= 40; reasons.append(f"Smart money writing CALLs ({100-pe_flow_pct:.0f}% of new OI) — bearish flow")
        elif pe_flow_pct < 45:
            score -= 20; reasons.append(f"CE OI addition dominates ({100-pe_flow_pct:.0f}%) — mild bearish flow")
        else:
            reasons.append(f"Balanced OI addition — no clear flow signal")

    # 2. Unwinding direction (smart money covering) — 30% weight
    pe_unwound = abs(sum(x.get("oi_change",0) for x in pe_unwind))
    ce_unwound = abs(sum(x.get("oi_change",0) for x in ce_unwind))
    if pe_unwound > ce_unwound * 1.5:
        score -= 25; reasons.append(f"PUTs being unwound (covered) — put writers exiting, bearish")
    elif ce_unwound > pe_unwound * 1.5:
        score += 25; reasons.append(f"CALLs being unwound (covered) — call writers exiting, bullish")

    # 3. OTM activity (speculation indicator) — 20% weight
    otm_ce = [c for c in chain if c["strike"] > spot * 1.02]
    otm_pe = [c for c in chain if c["strike"] < spot * 0.98]
    otm_ce_vol = sum(c["ce_volume"] for c in otm_ce)
    otm_pe_vol = sum(c["pe_volume"] for c in otm_pe)
    if otm_ce_vol + otm_pe_vol > 0:
        ce_vol_pct = otm_ce_vol / (otm_ce_vol + otm_pe_vol) * 100
        if ce_vol_pct > 65:
            score += 20; reasons.append(f"High OTM CE buying ({ce_vol_pct:.0f}%) — traders buying upside calls, bullish")
        elif ce_vol_pct < 35:
            score -= 20; reasons.append(f"High OTM PE buying ({100-ce_vol_pct:.0f}%) — traders buying downside puts, bearish hedge")

    # 4. Concentration at ATM ± 1 strike (10% weight)
    near_strikes = [c for c in chain if abs(c["strike"] - spot) <= 100]
    near_ce_oi   = sum(c["ce_oi"] for c in near_strikes)
    near_pe_oi   = sum(c["pe_oi"] for c in near_strikes)
    if near_ce_oi + near_pe_oi > 0:
        near_pcr = near_pe_oi / near_ce_oi if near_ce_oi > 0 else 1
        if near_pcr > 1.5:
            score += 10; reasons.append(f"Near-ATM OI skewed to PE (PCR={near_pcr:.2f}) — local support")
        elif near_pcr < 0.7:
            score -= 10; reasons.append(f"Near-ATM OI skewed to CE (PCR={near_pcr:.2f}) — local resistance")

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name": "Flow Engine", "score": round(score,2),
        "signal": signal, "confidence": min(95, abs(score)),
        "reasons": reasons,
        "data": {
            "pe_add": pe_add, "ce_add": ce_add,
            "pe_unwound": pe_unwound, "ce_unwound": ce_unwound,
        }
    }
