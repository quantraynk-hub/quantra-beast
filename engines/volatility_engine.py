"""
VOLATILITY ENGINE
VIX level + direction analysis. Affects strike selection and confidence, not just direction.
Score: modifier on signal quality (not pure direction)
"""

def run(indices: dict, option_data: dict) -> dict:
    vix_data   = indices.get("INDIA VIX", {})
    vix        = vix_data.get("last", 15)
    vix_change = vix_data.get("pChange", 0)
    atm_ce_iv  = option_data.get("atm_ce_iv", vix)
    atm_pe_iv  = option_data.get("atm_pe_iv", vix)

    score   = 0
    reasons = []

    # 1. VIX absolute level
    if vix < 12:
        score += 20
        reasons.append(f"VIX {vix} — Very low, cheap premium, BUY directional confidently")
        regime = "LOW_VOL"
    elif vix < 16:
        score += 10
        reasons.append(f"VIX {vix} — Normal range, standard option pricing")
        regime = "NORMAL_VOL"
    elif vix < 20:
        score -= 10
        reasons.append(f"VIX {vix} — Elevated, premium is expensive, buy sparingly")
        regime = "ELEVATED_VOL"
    elif vix < 25:
        score -= 25
        reasons.append(f"VIX {vix} — High fear, only buy if other signals very strong")
        regime = "HIGH_VOL"
    else:
        score -= 40
        reasons.append(f"VIX {vix} — Extreme fear spike, avoid buying premium")
        regime = "EXTREME_VOL"

    # 2. VIX direction (rising = bad for buyers, falling = good)
    if vix_change > 5:
        score -= 20; reasons.append(f"VIX rising {vix_change:.1f}% — fear increasing, BEARISH risk")
    elif vix_change > 2:
        score -= 10; reasons.append(f"VIX up {vix_change:.1f}% — mild fear increase")
    elif vix_change < -5:
        score += 20; reasons.append(f"VIX falling {vix_change:.1f}% — fear reducing, BULLISH for buyers")
    elif vix_change < -2:
        score += 10; reasons.append(f"VIX down {vix_change:.1f}% — calming market")

    # 3. IV vs VIX comparison (premium expensive or cheap)
    avg_iv = (atm_ce_iv + atm_pe_iv) / 2 if (atm_ce_iv + atm_pe_iv) > 0 else vix
    iv_vs_vix = avg_iv - vix
    if iv_vs_vix > 5:
        score -= 10; reasons.append(f"Option IV ({avg_iv:.1f}) >> VIX ({vix}) — premium expensive")
    elif iv_vs_vix < -5:
        score += 10; reasons.append(f"Option IV ({avg_iv:.1f}) << VIX ({vix}) — premium cheap, good to buy")

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name": "Volatility Engine", "score": round(score,2),
        "signal": signal, "confidence": min(95, abs(score)),
        "reasons": reasons,
        "data": {
            "vix": vix, "vix_change": vix_change, "regime": regime,
            "atm_ce_iv": atm_ce_iv, "atm_pe_iv": atm_pe_iv, "avg_iv": round(avg_iv,2)
        }
    }
