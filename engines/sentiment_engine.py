"""
SENTIMENT ENGINE
FII/DII flows, market breadth (advance/decline).
Score: -100 (heavy institutional selling) to +100 (heavy institutional buying)
"""

def run(fii_dii: dict, indices: dict) -> dict:
    fii_net  = fii_dii.get("fii_net", 0)
    fii_buy  = fii_dii.get("fii_buy", 0)
    fii_sell = fii_dii.get("fii_sell", 0)
    dii_net  = fii_dii.get("dii_net", 0)

    score   = 0
    reasons = []

    # 1. FII net flow (60% weight)
    if fii_net > 2000:
        score += 60; reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr — very strong institutional buying")
    elif fii_net > 1000:
        score += 45; reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr — strong FII buying")
    elif fii_net > 500:
        score += 25; reasons.append(f"FII NET BUY ₹{fii_net:.0f}Cr — moderate support")
    elif fii_net > 0:
        score += 10; reasons.append(f"FII mildly positive ₹{fii_net:.0f}Cr")
    elif fii_net > -500:
        score -= 10; reasons.append(f"FII mild selling ₹{abs(fii_net):.0f}Cr")
    elif fii_net > -1000:
        score -= 25; reasons.append(f"FII SELLING ₹{abs(fii_net):.0f}Cr — notable outflow")
    elif fii_net > -2000:
        score -= 45; reasons.append(f"FII HEAVY SELLING ₹{abs(fii_net):.0f}Cr — bearish")
    else:
        score -= 60; reasons.append(f"FII EXTREME SELLING ₹{abs(fii_net):.0f}Cr — very bearish")

    # 2. DII (30% weight — they counter FII often)
    if dii_net > 1000:
        score += 30; reasons.append(f"DII NET BUY ₹{dii_net:.0f}Cr — domestic institutions supporting")
    elif dii_net > 500:
        score += 15; reasons.append(f"DII buying ₹{dii_net:.0f}Cr — domestic support")
    elif dii_net > 0:
        score += 5;  reasons.append(f"DII mildly positive ₹{dii_net:.0f}Cr")
    elif dii_net < -500:
        score -= 15; reasons.append(f"DII selling ₹{abs(dii_net):.0f}Cr — domestic outflow")
    else:
        score -= 5

    # 3. Combined institutional direction (10% weight)
    combined = fii_net + dii_net
    if fii_net > 0 and dii_net > 0:
        score += 10; reasons.append(f"Both FII + DII buying — strong bull confirmation")
    elif fii_net < 0 and dii_net < 0:
        score -= 10; reasons.append(f"Both FII + DII selling — strong bear confirmation")
    elif fii_net > 0 and dii_net < 0:
        reasons.append("FII buying, DII selling — mixed institutional signal")
    else:
        reasons.append("FII selling, DII buying — typical counter-flow")

    # FII buy/sell ratio
    if fii_buy + abs(fii_sell) > 0:
        buy_ratio = fii_buy / (fii_buy + abs(fii_sell)) * 100
        reasons.append(f"FII buy/sell ratio: {buy_ratio:.1f}% buying")

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name": "Sentiment Engine", "score": round(score,2),
        "signal": signal, "confidence": min(95, abs(score)),
        "reasons": reasons,
        "data": {
            "fii_net": fii_net, "dii_net": dii_net,
            "combined_net": round(fii_net+dii_net,2),
        }
    }
