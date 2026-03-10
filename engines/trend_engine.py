"""
TREND ENGINE
Analyses price action: momentum, range position, gap, opening range.
Score: -100 (strong bear) to +100 (strong bull)
"""

def run(indices: dict, option_data: dict) -> dict:
    nifty   = indices.get("NIFTY 50", {})
    spot    = option_data.get("spot", 0)
    open_p  = nifty.get("open", spot)
    high    = nifty.get("high", spot)
    low     = nifty.get("low", spot)
    prev    = nifty.get("previousClose", spot)
    pchange = nifty.get("pChange", 0)

    score   = 0
    reasons = []

    # 1. Day change momentum (40% weight in this engine)
    if pchange > 1.0:
        score += 40; reasons.append(f"Strong bull momentum: NIFTY +{pchange:.2f}%")
    elif pchange > 0.5:
        score += 25; reasons.append(f"Bullish: NIFTY +{pchange:.2f}%")
    elif pchange > 0.1:
        score += 12; reasons.append(f"Mild up: NIFTY +{pchange:.2f}%")
    elif pchange > -0.1:
        score += 0;  reasons.append(f"Flat: NIFTY {pchange:.2f}%")
    elif pchange > -0.5:
        score -= 12; reasons.append(f"Mild down: NIFTY {pchange:.2f}%")
    elif pchange > -1.0:
        score -= 25; reasons.append(f"Bearish: NIFTY {pchange:.2f}%")
    else:
        score -= 40; reasons.append(f"Strong bear momentum: NIFTY {pchange:.2f}%")

    # 2. Position in day's range (30% weight)
    hl = high - low
    if hl > 0:
        range_pos = (spot - low) / hl * 100
        if range_pos > 80:
            score += 30; reasons.append(f"At {range_pos:.0f}% of day range — upper end, bull strength")
        elif range_pos > 60:
            score += 15; reasons.append(f"At {range_pos:.0f}% of range — mild bull")
        elif range_pos > 40:
            score += 0;  reasons.append(f"Mid-range ({range_pos:.0f}%) — neutral")
        elif range_pos > 20:
            score -= 15; reasons.append(f"At {range_pos:.0f}% of range — mild bear")
        else:
            score -= 30; reasons.append(f"At {range_pos:.0f}% of day range — lower end, bear weakness")
    else:
        range_pos = 50

    # 3. Gap analysis (20% weight)
    if prev > 0:
        gap_pct = (open_p - prev) / prev * 100
        if gap_pct > 0.5:
            score += 20; reasons.append(f"Gap up open {gap_pct:.2f}% — bullish sentiment")
        elif gap_pct > 0.1:
            score += 8;  reasons.append(f"Slight gap up {gap_pct:.2f}%")
        elif gap_pct < -0.5:
            score -= 20; reasons.append(f"Gap down open {gap_pct:.2f}% — bearish sentiment")
        elif gap_pct < -0.1:
            score -= 8;  reasons.append(f"Slight gap down {gap_pct:.2f}%")
        else:
            reasons.append("Flat open — no gap signal")
    else:
        gap_pct = 0

    # 4. Current price vs open (10% weight)
    if open_p > 0:
        vs_open = (spot - open_p) / open_p * 100
        if vs_open > 0.3:
            score += 10; reasons.append(f"Trading above open by {vs_open:.2f}% — intraday strength")
        elif vs_open < -0.3:
            score -= 10; reasons.append(f"Trading below open by {abs(vs_open):.2f}% — intraday weakness")

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name":       "Trend Engine",
        "score":      round(score, 2),
        "signal":     signal,
        "confidence": min(95, abs(score)),
        "reasons":    reasons,
        "data": {
            "pchange":   pchange,
            "range_pos": round(range_pos, 1) if hl > 0 else 50,
            "gap_pct":   round(gap_pct, 2) if prev > 0 else 0,
            "spot":      spot,
            "high":      high,
            "low":       low,
        }
    }
