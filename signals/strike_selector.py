"""STRIKE SELECTOR — picks best strike and expiry based on signal and market conditions"""

def select_strike(signal, chain_data, action, vix=15):
    """
    Returns optimal CE/PE strike + LTP based on:
    - Signal confidence (high confidence = closer to ATM)
    - VIX level (high VIX = further OTM to reduce cost)
    - Available liquidity (skip illiquid strikes)
    """
    spot       = chain_data.get("spot", 0)
    atm        = chain_data.get("atm", 0)
    chain      = chain_data.get("chain", [])
    confidence = signal.get("confidence", 50)

    if not chain or spot == 0:
        return {"ce_strike": atm, "pe_strike": atm, "ce_ltp": 0, "pe_ltp": 0}

    STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}
    sym  = chain_data.get("symbol", "NIFTY")
    step = STEP.get(sym, 50)

    # High confidence → ATM, low confidence → 1 strike OTM
    if confidence >= 70:
        otm_steps = 0      # ATM
    elif confidence >= 55:
        otm_steps = 1      # 1 step OTM
    else:
        otm_steps = 2      # 2 steps OTM (cheaper, safer)

    # High VIX → go further OTM (reduce premium cost)
    if vix > 20:
        otm_steps += 1

    ce_target = atm + (otm_steps * step)
    pe_target = atm - (otm_steps * step)

    # Find actual prices in chain
    ce_ltp = pe_ltp = 0
    ce_iv  = pe_iv  = 0
    for row in chain:
        if row["strike"] == ce_target:
            ce_ltp = row.get("ce_ltp", 0)
            ce_iv  = row.get("ce_iv", 0)
        if row["strike"] == pe_target:
            pe_ltp = row.get("pe_ltp", 0)
            pe_iv  = row.get("pe_iv", 0)

    # Fallback to ATM if OTM has no liquidity
    if ce_ltp == 0:
        ce_target = atm
        atm_row = next((r for r in chain if r["strike"] == atm), {})
        ce_ltp = atm_row.get("ce_ltp", 0)
    if pe_ltp == 0:
        pe_target = atm
        atm_row = next((r for r in chain if r["strike"] == atm), {})
        pe_ltp = atm_row.get("pe_ltp", 0)

    return {
        "ce_strike": ce_target, "ce_ltp": ce_ltp, "ce_iv": ce_iv,
        "pe_strike": pe_target, "pe_ltp": pe_ltp, "pe_iv": pe_iv,
        "otm_steps": otm_steps,
    }
