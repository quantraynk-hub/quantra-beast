"""
SIGNAL FUSION ENGINE — Stage 3
Combines all 7 engine scores with weighted logic.
Applies regime multiplier, validates confidence threshold.
Output: final trade signal with full metadata.
"""

import datetime, uuid
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines import (
    trend_engine, options_engine, gamma_engine,
    volatility_engine, regime_engine, sentiment_engine, flow_engine
)

# Default weights (will be overridden by learning engine after 10 trades)
DEFAULT_WEIGHTS = {
    "trend":      0.18,
    "options":    0.20,
    "gamma":      0.12,
    "volatility": 0.10,
    "regime":     0.08,
    "sentiment":  0.15,
    "flow":       0.17,
}

# Minimum composite score to generate a signal (avoid weak signals)
CONFIDENCE_THRESHOLD = 25


def generate_signal(snapshot: dict, weights: dict = None) -> dict:
    """
    Main function: takes full market snapshot, returns complete signal.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS.copy()

    indices     = snapshot.get("indices", {})
    option_data = snapshot.get("option_data", {})
    fii_dii     = snapshot.get("fii_dii", {})
    symbol      = snapshot.get("symbol", "NIFTY")

    # ── Run all engines
    e_trend   = trend_engine.run(indices, option_data)
    e_options = options_engine.run(option_data)
    e_gamma   = gamma_engine.run(option_data)
    e_vol     = volatility_engine.run(indices, option_data)
    e_regime  = regime_engine.run(indices, option_data, e_trend["score"], e_vol["data"].get("regime","NORMAL_VOL"))
    e_sent    = sentiment_engine.run(fii_dii, indices)
    e_flow    = flow_engine.run(option_data)

    engines = {
        "trend":      e_trend,
        "options":    e_options,
        "gamma":      e_gamma,
        "volatility": e_vol,
        "regime":     e_regime,
        "sentiment":  e_sent,
        "flow":       e_flow,
    }

    # ── Weighted composite score
    composite = sum(engines[k]["score"] * weights.get(k,0) for k in engines)

    # ── Regime multiplier on confidence (not score)
    regime_mult = e_regime["data"].get("confidence_multiplier", 1.0)

    # ── Confidence = abs(composite) × regime modifier
    raw_confidence = abs(composite) * regime_mult
    confidence     = round(min(97, max(35, raw_confidence)), 1)

    # ── Count bull vs bear engines
    bull_count = sum(1 for e in engines.values() if e["signal"] == "BULL")
    bear_count = sum(1 for e in engines.values() if e["signal"] == "BEAR")
    confluence = bull_count if composite > 0 else bear_count  # how many engines agree

    # ── Determine action
    if abs(composite) < CONFIDENCE_THRESHOLD or confluence < 3:
        action    = "WAIT"
        signal_cls = "wait"
        signal_sub = f"Low confluence ({confluence}/7 engines agree) — wait for clearer setup"
    elif composite > 50:
        action    = "BUY CE"
        signal_cls = "bull"
        signal_sub = f"STRONG BULL — {confluence}/7 engines aligned"
    elif composite > CONFIDENCE_THRESHOLD:
        action    = "BUY CE"
        signal_cls = "bull"
        signal_sub = f"BULL — {confluence}/7 engines aligned, moderate confidence"
    elif composite < -50:
        action    = "BUY PE"
        signal_cls = "bear"
        signal_sub = f"STRONG BEAR — {confluence}/7 engines aligned"
    elif composite < -CONFIDENCE_THRESHOLD:
        action    = "BUY PE"
        signal_cls = "bear"
        signal_sub = f"BEAR — {confluence}/7 engines aligned, moderate confidence"
    else:
        action    = "WAIT"
        signal_cls = "wait"
        signal_sub = "Borderline — wait for confirmation"

    # ── Top 5 reasons (from highest-weighted engines)
    all_reasons = []
    for k in ["options","sentiment","flow","trend","gamma","volatility","regime"]:
        for r in engines[k]["reasons"][:1]:  # top reason per engine
            all_reasons.append(r)

    # ── Key levels from option data
    spot      = option_data.get("spot", 0)
    max_pain  = option_data.get("max_pain", spot)
    atm       = option_data.get("atm_strike", round(spot/50)*50)
    resistances = [c["strike"] for c in option_data.get("resistance_strikes", [])][:3]
    supports    = [c["strike"] for c in option_data.get("support_strikes", [])][:3]

    return {
        "signal_id":   str(uuid.uuid4())[:8],
        "timestamp":   datetime.datetime.now().isoformat(),
        "symbol":      symbol,
        "action":      action,
        "signal_cls":  signal_cls,
        "signal_sub":  signal_sub,
        "composite":   round(composite, 2),
        "confidence":  confidence,
        "bull_count":  bull_count,
        "bear_count":  bear_count,
        "confluence":  confluence,
        "engines":     {k: {"score": v["score"], "signal": v["signal"], "confidence": v["confidence"]}
                        for k,v in engines.items()},
        "engine_reasons": {k: v["reasons"] for k,v in engines.items()},
        "top_reasons": all_reasons[:6],
        "weights_used": weights,
        "market_data": {
            "spot": spot, "atm": atm, "max_pain": max_pain,
            "pcr": option_data.get("pcr",0),
            "vix": indices.get("INDIA VIX",{}).get("last",0),
            "fii_net": fii_dii.get("fii_net",0),
            "expiry": option_data.get("expiry",""),
            "resistances": resistances,
            "supports": supports,
        },
        "chain": option_data.get("chain", []),
    }
