# signals/fusion_engine.py
# QUANTRA BEAST v3.2 — Signal Fusion Engine
# Combines all 8 engine scores with weighted logic.
# Applies regime multiplier, validates confidence threshold.

import datetime, uuid

from engines import (
    trend_engine, options_engine, gamma_engine,
    volatility_engine, regime_engine, sentiment_engine, flow_engine,
)

# ── Weights matching server.py v3.2 ─────────────────────────────────────────
DEFAULT_WEIGHTS = {
    "trend":      0.16,
    "options":    0.18,
    "gamma":      0.11,
    "volatility": 0.09,
    "regime":     0.07,
    "sentiment":  0.14,
    "flow":       0.15,
    "news":       0.10,
}

CONFIDENCE_THRESHOLD = 25


def generate_signal(snapshot: dict, weights: dict = None,
                    news_score: float = 0) -> dict:
    """
    Main entry point: takes full market snapshot, returns complete signal dict.

    Parameters
    ----------
    snapshot   : dict with keys indices, option_data, fii_dii, symbol
    weights    : override DEFAULT_WEIGHTS (e.g. from learning engine)
    news_score : pre-computed news engine score (-100..100)
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS.copy()

    indices     = snapshot.get("indices", {})
    option_data = snapshot.get("option_data", {})
    fii_dii     = snapshot.get("fii_dii", {})
    symbol      = snapshot.get("symbol", "NIFTY")

    # ── Run all engines ─────────────────────────────────────────────────────
    e_trend = trend_engine.run(indices, option_data)
    e_opts  = options_engine.run(option_data)
    e_gamma = gamma_engine.run(option_data)
    e_vol   = volatility_engine.run(indices, option_data)

    # regime_engine.run() needs trend_score and vol_regime (v3.2 signature)
    vol_regime = e_vol["data"].get("regime", "NORMAL_VOL")
    e_regime   = regime_engine.run(indices, option_data,
                                   e_trend["score"], vol_regime)
    e_sent     = sentiment_engine.run(fii_dii, indices)
    e_flow     = flow_engine.run(option_data)

    # ── News engine (score passed in or zero) ────────────────────────────────
    e_news = {
        "name":       "News Engine",
        "score":      news_score,
        "signal":     "BULL" if news_score > 15 else "BEAR" if news_score < -15 else "NEUTRAL",
        "confidence": min(95, abs(news_score)),
        "reasons":    [f"News sentiment score: {news_score}"],
        "data":       {},
    }

    engines = {
        "trend":      e_trend,
        "options":    e_opts,
        "gamma":      e_gamma,
        "volatility": e_vol,
        "regime":     e_regime,
        "sentiment":  e_sent,
        "flow":       e_flow,
        "news":       e_news,
    }

    # ── Weighted composite score ─────────────────────────────────────────────
    composite = sum(engines[k]["score"] * weights.get(k, 0) for k in engines)

    # ── Regime confidence multiplier ─────────────────────────────────────────
    regime_mult    = e_regime["data"].get("confidence_multiplier", 1.0)
    raw_confidence = abs(composite) * regime_mult
    confidence     = round(min(97, max(35, raw_confidence)), 1)

    # ── Confluence count ─────────────────────────────────────────────────────
    bull_count = sum(1 for e in engines.values() if e["signal"] == "BULL")
    bear_count = sum(1 for e in engines.values() if e["signal"] == "BEAR")
    confluence = bull_count if composite > 0 else bear_count

    # ── Determine action ─────────────────────────────────────────────────────
    n_engines = len(engines)
    min_confluence = 3  # at least 3 of 8 engines must agree

    if abs(composite) < CONFIDENCE_THRESHOLD or confluence < min_confluence:
        action     = "WAIT"
        signal_cls = "wait"
        signal_sub = f"Low confluence ({confluence}/{n_engines}) — wait for clearer setup"
    elif composite > 50:
        action     = "BUY CE"
        signal_cls = "bull"
        signal_sub = f"STRONG BULL — {confluence}/{n_engines} engines aligned"
    elif composite > CONFIDENCE_THRESHOLD:
        action     = "BUY CE"
        signal_cls = "bull"
        signal_sub = f"BULL — {confluence}/{n_engines} engines aligned, moderate confidence"
    elif composite < -50:
        action     = "BUY PE"
        signal_cls = "bear"
        signal_sub = f"STRONG BEAR — {confluence}/{n_engines} engines aligned"
    elif composite < -CONFIDENCE_THRESHOLD:
        action     = "BUY PE"
        signal_cls = "bear"
        signal_sub = f"BEAR — {confluence}/{n_engines} engines aligned, moderate confidence"
    else:
        action     = "WAIT"
        signal_cls = "wait"
        signal_sub = "Borderline — wait for confirmation"

    # ── Top reasons from highest-weighted engines ────────────────────────────
    priority_order = ["options", "sentiment", "flow", "trend", "gamma",
                      "volatility", "regime", "news"]
    all_reasons = []
    for k in priority_order:
        if engines.get(k, {}).get("reasons"):
            all_reasons.append(engines[k]["reasons"][0])

    # ── Key levels ───────────────────────────────────────────────────────────
    spot        = option_data.get("spot", 0)
    max_pain    = option_data.get("max_pain", spot)
    atm         = option_data.get("atm_strike", round(spot / 50) * 50)
    resistances = [c["strike"] for c in option_data.get("resistance_strikes", [])][:3]
    supports    = [c["strike"] for c in option_data.get("support_strikes",    [])][:3]

    return {
        "signal_id":    str(uuid.uuid4())[:8],
        "timestamp":    datetime.datetime.now().isoformat(),
        "symbol":       symbol,
        "action":       action,
        "signal_cls":   signal_cls,
        "signal_sub":   signal_sub,
        "composite":    round(composite, 2),
        "confidence":   confidence,
        "bull_count":   bull_count,
        "bear_count":   bear_count,
        "confluence":   confluence,
        "engines": {
            k: {"score": v["score"], "signal": v["signal"],
                "confidence": v["confidence"]}
            for k, v in engines.items()
        },
        "engine_reasons": {k: v["reasons"] for k, v in engines.items()},
        "top_reasons":    all_reasons[:6],
        "weights_used":   weights,
        "market_data": {
            "spot":        spot,
            "atm":         atm,
            "max_pain":    max_pain,
            "pcr":         option_data.get("pcr", 0),
            "vix":         indices.get("INDIA VIX", {}).get("last", 0),
            "fii_net":     fii_dii.get("fii_net", 0),
            "expiry":      option_data.get("expiry", ""),
            "resistances": resistances,
            "supports":    supports,
        },
        "chain": option_data.get("chain", []),
    }
