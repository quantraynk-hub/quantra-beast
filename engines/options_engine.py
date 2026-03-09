"""
OPTIONS ENGINE
PCR analysis, Max Pain, OI buildup/unwind, IV skew.
Score: -100 (strong bear) to +100 (strong bull)
"""

def run(option_data: dict) -> dict:
    pcr      = option_data.get("pcr", 1.0)
    spot     = option_data.get("spot", 0)
    max_pain = option_data.get("max_pain", spot)
    iv_skew  = option_data.get("iv_skew", 0)      # positive = put IV > call IV
    ce_build = option_data.get("ce_buildup", [])
    pe_build = option_data.get("pe_buildup", [])
    ce_total = option_data.get("total_ce_oi", 1)
    pe_total = option_data.get("total_pe_oi", 1)

    score   = 0
    reasons = []

    # 1. PCR (40% weight)
    if pcr > 1.5:
        score += 40; reasons.append(f"PCR {pcr} > 1.5 — Extreme PUT writing, strong bull floor")
    elif pcr > 1.3:
        score += 30; reasons.append(f"PCR {pcr} > 1.3 — Heavy PUT writing, bullish bias")
    elif pcr > 1.1:
        score += 15; reasons.append(f"PCR {pcr} — Mild bullish (1.1–1.3)")
    elif pcr > 0.9:
        score += 0;  reasons.append(f"PCR {pcr} — Neutral zone (0.9–1.1)")
    elif pcr > 0.7:
        score -= 20; reasons.append(f"PCR {pcr} — CALL heavy, bearish cap building")
    else:
        score -= 40; reasons.append(f"PCR {pcr} < 0.7 — Extreme CALL writing, strong bear ceiling")

    # 2. Max Pain (30% weight)
    if max_pain > 0 and spot > 0:
        pain_diff_pct = (spot - max_pain) / max_pain * 100
        if pain_diff_pct < -1.0:
            score += 30; reasons.append(f"Spot {spot} well BELOW max pain {max_pain} — strong upward pull ({pain_diff_pct:.1f}%)")
        elif pain_diff_pct < -0.3:
            score += 15; reasons.append(f"Spot below max pain {max_pain} — moderate upward pull")
        elif pain_diff_pct > 1.0:
            score -= 30; reasons.append(f"Spot {spot} well ABOVE max pain {max_pain} — strong downward pull ({pain_diff_pct:.1f}%)")
        elif pain_diff_pct > 0.3:
            score -= 15; reasons.append(f"Spot above max pain {max_pain} — moderate downward pull")
        else:
            reasons.append(f"Spot near max pain {max_pain} — pinned, expect range")

    # 3. OI Buildup (20% weight)
    ce_add = sum(x.get("oi_change", 0) for x in ce_build)
    pe_add = sum(x.get("oi_change", 0) for x in pe_build)
    if pe_add > 0 and ce_add > 0:
        ratio = pe_add / ce_add
        if ratio > 2:
            score += 20; reasons.append(f"PE OI buildup {ratio:.1f}x > CE — PUT writing = bullish support")
        elif ratio > 1.2:
            score += 10; reasons.append(f"PE OI slightly > CE buildup — mild bullish")
        elif ratio < 0.5:
            score -= 20; reasons.append(f"CE OI buildup dominates — CALL writing = bearish resistance")
        elif ratio < 0.8:
            score -= 10; reasons.append(f"CE OI slightly > PE — mild bearish cap")
        else:
            reasons.append("CE/PE OI buildup balanced — sideways")
    elif pe_add > 0:
        score += 15; reasons.append(f"PE OI building up, no CE addition — bullish")
    elif ce_add > 0:
        score -= 15; reasons.append(f"CE OI building up, no PE addition — bearish cap")

    # 4. IV Skew (10% weight)
    if iv_skew > 3:
        score -= 10; reasons.append(f"IV skew {iv_skew} — PUT IV >> CALL IV, fear of downside")
    elif iv_skew < -3:
        score += 10; reasons.append(f"IV skew {iv_skew} — CALL IV >> PUT IV, fear of upside miss")
    else:
        reasons.append(f"IV skew {iv_skew} — balanced, no directional fear")

    score = max(-100, min(100, score))
    signal = "BULL" if score > 15 else "BEAR" if score < -15 else "NEUTRAL"

    return {
        "name":       "Options Engine",
        "score":      round(score, 2),
        "signal":     signal,
        "confidence": min(95, abs(score)),
        "reasons":    reasons,
        "data":       {"pcr": pcr, "max_pain": max_pain, "iv_skew": iv_skew,
                       "ce_buildup_oi": ce_add, "pe_buildup_oi": pe_add}
    }
