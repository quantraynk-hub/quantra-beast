"""
REGIME ENGINE
Classifies market environment: trending_bull, trending_bear, ranging, volatile.
This modifies confidence of all other engines, not the score.
"""

def run(indices: dict, option_data: dict, trend_score: float, vol_regime: str) -> dict:
    nifty   = indices.get("NIFTY 50", {})
    vix     = indices.get("INDIA VIX", {}).get("last", 15)
    pchange = nifty.get("pChange", 0)
    high    = nifty.get("high", 1)
    low     = nifty.get("low", 1)
    spot    = option_data.get("spot", 1)
    pcr     = option_data.get("pcr", 1)

    day_range_pct = (high - low) / low * 100 if low > 0 else 0

    score   = 0
    reasons = []

    # Classify regime
    if vix < 16 and abs(pchange) > 0.5:
        regime = "TRENDING"
        reasons.append(f"Low VIX + directional move — trending market, signals reliable")
        score = 30 if pchange > 0 else -30
    elif vix > 20 and day_range_pct > 1.5:
        regime = "VOLATILE"
        reasons.append(f"High VIX + wide range — volatile market, use caution")
        score = -20  # volatile = bad for buyers
    elif vix < 14 and day_range_pct < 0.5:
        regime = "RANGING"
        reasons.append(f"Low VIX + tight range — pinned/ranging market, sell premium preferred")
        score = 0
    elif abs(pchange) > 1.0:
        regime = "STRONG_TREND"
        score = 40 if pchange > 0 else -40
        reasons.append(f"Strong directional move {pchange:.2f}% — high conviction trend")
    else:
        regime = "MIXED"
        reasons.append(f"Mixed signals — proceed with caution, require confluence")
        score = 0

    # PCR regime confirmation
    if pcr > 1.3 and pchange > 0:
        score += 10; reasons.append("PCR + trend aligned — bull regime confirmed")
    elif pcr < 0.8 and pchange < 0:
        score -= 10; reasons.append("PCR + trend aligned — bear regime confirmed")
    elif pcr > 1.3 and pchange < 0:
        reasons.append("Conflicting: PCR bull but price falling — reversal possible")
    elif pcr < 0.8 and pchange > 0:
        reasons.append("Conflicting: PCR bear but price rising — exhaustion possible")

    confidence_multiplier = {
        "STRONG_TREND": 1.2,
        "TRENDING":     1.1,
        "RANGING":      0.8,
        "VOLATILE":     0.7,
        "MIXED":        0.9,
    }.get(regime, 1.0)

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name": "Regime Engine", "score": round(score,2),
        "signal": signal, "confidence": min(95, abs(score)),
        "reasons": reasons,
        "data": {
            "regime": regime, "vix": vix, "pchange": pchange,
            "day_range_pct": round(day_range_pct,2),
            "confidence_multiplier": confidence_multiplier,
        }
    }
