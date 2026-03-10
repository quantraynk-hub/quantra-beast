# monitoring/trade_monitor.py
# QUANTRA BEAST v3.2 — Trade Monitor
# Full P&L tracking, SL/target/trailing logic, time-decay warning.
# Upgraded from skeleton: now handles live LTP from active_monitor cache.

import datetime

LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65}


class TradeMonitor:
    def __init__(self, trade: dict):
        self.trade        = trade
        self.entry_time   = datetime.datetime.now()
        self.trailing_sl  = False
        self.partial_done = False

    def check(self, current_chain: list, current_signal_score: float,
              live_ltp: dict = None) -> dict:
        """
        Evaluate open trade. Returns status dict.

        Parameters
        ----------
        current_chain         : latest option chain (list of strike dicts)
        current_signal_score  : composite score from signal engine (+/-)
        live_ltp              : dict with keys ce_ltp, pe_ltp, spot_ltp
                                (from Kite WS / active_monitor cache)
        """
        action     = self.trade.get("action", "BUY CE")
        symbol     = self.trade.get("symbol", "NIFTY")
        lots       = self.trade.get("lots", 1)
        lot_size   = LOT_SIZES.get(symbol, 75)
        ce_strike  = self.trade.get("ce_strike")
        pe_strike  = self.trade.get("pe_strike")
        ce_entry   = self.trade.get("ce_price", 0)
        pe_entry   = self.trade.get("pe_price", 0)
        ce_sl      = self.trade.get("ce_sl", 0)
        pe_sl      = self.trade.get("pe_sl", 0)
        ce_target  = self.trade.get("ce_target", 0)
        pe_target  = self.trade.get("pe_target", 0)
        max_loss   = self.trade.get("max_loss", 0)
        target_pnl = self.trade.get("target_pnl", 0)

        # ── Get current LTPs ──────────────────────────────────────────────
        ce_current = pe_current = 0

        # Priority 1: live LTP from Kite WS
        if live_ltp:
            ce_current = live_ltp.get("ce_ltp", 0)
            pe_current = live_ltp.get("pe_ltp", 0)

        # Priority 2: chain data
        if (ce_current == 0 or pe_current == 0) and current_chain:
            for c in current_chain:
                if ce_strike and c["strike"] == ce_strike and c.get("ce_ltp"):
                    ce_current = c["ce_ltp"]
                if pe_strike and c["strike"] == pe_strike and c.get("pe_ltp"):
                    pe_current = c["pe_ltp"]

        # Fallback: use entry prices (no change detected)
        if ce_current == 0 and ce_entry > 0: ce_current = ce_entry
        if pe_current == 0 and pe_entry > 0: pe_current = pe_entry

        # ── P&L ───────────────────────────────────────────────────────────
        pnl = 0.0
        if action == "BUY CE" and ce_entry > 0:
            pnl = (ce_current - ce_entry) * lots * lot_size
        elif action == "BUY PE" and pe_entry > 0:
            pnl = (pe_current - pe_entry) * lots * lot_size
        elif action == "BUY CE + PE":
            pnl = ((ce_current - ce_entry) + (pe_current - pe_entry)) * lots * lot_size

        pnl_pct = round(pnl / max_loss * 100, 1) if max_loss > 0 else 0
        elapsed = (datetime.datetime.now() - self.entry_time).seconds // 60

        # ── Status logic ──────────────────────────────────────────────────
        status = "HOLD"; exit_reason = ""; urgency = "normal"

        # Stop loss hit
        if (action == "BUY CE"  and ce_current > 0 and ce_current <= ce_sl) or \
           (action == "BUY PE"  and pe_current > 0 and pe_current <= pe_sl) or \
           (action == "BUY CE + PE" and pnl <= -max_loss * 0.95):
            status      = "EXIT_SL"
            exit_reason = f"SL HIT — P&L ₹{pnl:.0f}"
            urgency     = "critical"

        # Target hit
        elif (action == "BUY CE" and ce_current >= ce_target) or \
             (action == "BUY PE" and pe_current >= pe_target) or \
             (action == "BUY CE + PE" and pnl >= target_pnl):
            status      = "EXIT_TARGET"
            exit_reason = f"TARGET HIT ₹{pnl:.0f}"
            urgency     = "success"

        # Trailing SL (60% of target reached)
        elif pnl > target_pnl * 0.6 and not self.trailing_sl:
            self.trailing_sl = True
            # Move SL to entry
            self.trade["ce_sl"] = ce_entry
            self.trade["pe_sl"] = pe_entry
            status      = "TRAIL_SL"
            exit_reason = f"SL trailed to entry — ₹{pnl:.0f} protected"
            urgency     = "info"

        # Partial booking (50% of target, ≥2 lots)
        elif pnl > target_pnl * 0.5 and not self.partial_done and lots >= 2:
            self.partial_done = True
            status      = "BOOK_PARTIAL"
            exit_reason = f"Book {lots // 2} lots — ₹{pnl:.0f} profit"
            urgency     = "action"

        # Signal reversal (engine score flipped against trade)
        elif current_signal_score * (1 if "CE" in action else -1) < -30:
            status      = "SIGNAL_REVERSED"
            exit_reason = f"Signal reversed — consider exit ₹{pnl:.0f}"
            urgency     = "warning"

        # Time decay — stagnant > 3 hours
        elif elapsed > 180 and abs(pnl_pct) < 20:
            status      = "TIME_DECAY"
            exit_reason = f"Stagnant {elapsed}min — time decay warning"
            urgency     = "warning"

        return {
            "status":        status,
            "urgency":       urgency,
            "exit_reason":   exit_reason,
            "pnl":           round(pnl, 2),
            "pnl_pct":       pnl_pct,
            "elapsed_min":   elapsed,
            "ce_current":    ce_current,
            "pe_current":    pe_current,
            "spot":          live_ltp.get("spot_ltp", 0) if live_ltp else 0,
            "ce_sl":         self.trade.get("ce_sl", ce_sl),
            "pe_sl":         self.trade.get("pe_sl", pe_sl),
            "ce_target":     ce_target,
            "pe_target":     pe_target,
            "trailing_sl":   self.trailing_sl,
            "partial_done":  self.partial_done,
            "checked_at":    datetime.datetime.now().isoformat(),
        }
