# capital/risk_manager.py
# QUANTRA BEAST v3.2 — Capital & Risk Manager
# Hard rules: max loss/day, drawdown, position sizing, consecutive loss protection.

import datetime

LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65}
MARGINS   = {"NIFTY": 6500, "BANKNIFTY": 11000, "FINNIFTY": 6000}
SL_PCT    = 0.40   # 40% of premium as stop loss


class RiskManager:
    def __init__(self, config: dict):
        self.total_capital    = config.get("capital", config.get("total_capital", 50000))
        self.risk_per_trade   = config.get("risk_pct", config.get("risk_per_trade", 2.0))
        self.rr_ratio         = config.get("rr_ratio", 2.0)
        self.max_trades_day   = config.get("max_trades", config.get("max_trades_day", 3))
        self.max_daily_loss   = config.get("max_daily_loss", 3.0)    # % of capital
        self.max_drawdown     = config.get("max_drawdown", 10.0)     # % of capital

        # ── Adapter aliases so server.py v3.2 attr refs work unchanged ──
        # server.py uses: risk_mgr.total, risk_mgr.risk_pct,
        #                 risk_mgr.rr, risk_mgr.max_trades
        self._last_cfg = config

        self.session_trades    = 0
        self.session_pnl       = 0.0
        self.consecutive_loss  = 0
        self.peak_capital      = self.total_capital
        self.available_capital = self.total_capital
        self.in_trade_capital  = 0.0
        self.session_wins      = 0
        self.session_losses    = 0
        self.trade_log         = []
        self.session_date      = datetime.date.today()

    # ── Adapter properties (server.py v3.2 shorthand) ───────────────────
    @property
    def total(self) -> float:
        return self.total_capital

    @property
    def risk_pct(self) -> float:
        return self.risk_per_trade

    @property
    def rr(self) -> float:
        return self.rr_ratio

    @property
    def max_trades(self) -> int:
        return self.max_trades_day

    # ────────────────────────────────────────────────────────────────────

    def _reset_if_new_day(self):
        today = datetime.date.today()
        if today != self.session_date:
            self.session_trades   = 0
            self.session_pnl      = 0.0
            self.consecutive_loss = 0
            self.session_wins     = 0
            self.session_losses   = 0
            self.session_date     = today

    def can_trade(self) -> dict:
        """Returns whether a new trade is allowed and why not if blocked."""
        self._reset_if_new_day()

        if self.session_trades >= self.max_trades_day:
            return {"allowed": False,
                    "reason": f"Max trades ({self.max_trades_day}) reached for today"}

        daily_loss_limit = self.total_capital * (self.max_daily_loss / 100)
        if self.session_pnl <= -daily_loss_limit:
            return {"allowed": False,
                    "reason": f"Daily loss limit ₹{daily_loss_limit:.0f} hit — no more trades today"}

        current_drawdown = (
            (self.peak_capital - self.available_capital) / self.peak_capital * 100
        )
        if current_drawdown >= self.max_drawdown:
            return {"allowed": False,
                    "reason": f"Drawdown {current_drawdown:.1f}% exceeds {self.max_drawdown}% limit — system paused"}

        return {"allowed": True, "reason": "OK"}

    def calculate_position(self, signal: dict, option_data: dict) -> dict:
        """Calculate lots, capital used, SL, target for given signal."""
        self._reset_if_new_day()

        symbol = signal.get("symbol", "NIFTY")
        action = signal.get("action", "BUY CE")
        spot   = signal.get("market_data", {}).get("spot", 0)
        atm    = signal.get("market_data", {}).get("atm", round(spot / 50) * 50)
        chain  = signal.get("chain", [])
        expiry = signal.get("market_data", {}).get("expiry", "")

        lot_size = LOT_SIZES.get(symbol, 75)
        margin   = MARGINS.get(symbol, 6500)

        ce_price = pe_price = 0
        for c in chain:
            if c["strike"] == atm:
                ce_price = c.get("ce_ltp", 0)
                pe_price = c.get("pe_ltp", 0)
                break
        if ce_price == 0: ce_price = round(spot * 0.0055, 1)
        if pe_price == 0: pe_price = round(spot * 0.0050, 1)

        risk_multiplier = 0.5 if self.consecutive_loss >= 2 else 1.0
        risk_amount = self.available_capital * (self.risk_per_trade / 100) * risk_multiplier

        price_for_calc = (
            ce_price if action == "BUY CE" else
            pe_price if action == "BUY PE" else
            (ce_price + pe_price) / 2
        )

        per_lot_sl   = price_for_calc * SL_PCT * lot_size
        lots         = max(1, int(risk_amount / per_lot_sl)) if per_lot_sl > 0 else 1
        multiplier   = 2 if action == "BUY CE + PE" else 1
        capital_used = lots * margin * multiplier
        max_loss     = round(lots * price_for_calc * lot_size * SL_PCT * multiplier)
        target_pnl   = round(max_loss * self.rr_ratio)

        return {
            "action":       action,
            "symbol":       symbol,
            "expiry":       expiry,
            "ce_strike":    atm if action in ["BUY CE", "BUY CE + PE"] else None,
            "pe_strike":    atm if action in ["BUY PE", "BUY CE + PE"] else None,
            "ce_price":     ce_price,
            "pe_price":     pe_price,
            "ce_sl":        round(ce_price * (1 - SL_PCT), 1),
            "ce_target":    round(ce_price * (1 + self.rr_ratio * SL_PCT), 1),
            "pe_sl":        round(pe_price * (1 - SL_PCT), 1),
            "pe_target":    round(pe_price * (1 + self.rr_ratio * SL_PCT), 1),
            "lots":         lots,
            "lot_size":     lot_size,
            "capital_used": int(capital_used),
            "max_loss":     int(max_loss),
            "target_pnl":   int(target_pnl),
            "risk_reduced": risk_multiplier < 1.0,
            "risk_note":    "50% size — 2 consecutive losses" if risk_multiplier < 1.0 else "",
            "spot":         spot,
            "atm":          atm,
        }

    def record_trade_result(self, pnl: float):
        """Call after every trade exit."""
        self.session_trades    += 1
        self.session_pnl       += pnl
        self.available_capital += pnl
        self.in_trade_capital   = 0

        if pnl > 0:
            self.session_wins     += 1
            self.consecutive_loss  = 0
        else:
            self.session_losses   += 1
            self.consecutive_loss += 1

        if self.available_capital > self.peak_capital:
            self.peak_capital = self.available_capital

        self.trade_log.append({
            "ts":      datetime.datetime.now().isoformat(),
            "pnl":     pnl,
            "balance": round(self.available_capital, 2),
            "result":  "WIN" if pnl > 0 else "LOSS",
        })

    def get_status(self) -> dict:
        win_rate = (
            round(self.session_wins / self.session_trades * 100, 1)
            if self.session_trades > 0 else 0
        )
        daily_loss_used = round(
            abs(min(0, self.session_pnl)) /
            (self.total_capital * self.max_daily_loss / 100) * 100, 1
        ) if self.session_pnl < 0 else 0
        drawdown = round(
            (self.peak_capital - self.available_capital) / self.peak_capital * 100, 2
        )
        return {
            "total_capital":       self.total_capital,
            "available_capital":   round(self.available_capital, 2),
            "in_trade_capital":    round(self.in_trade_capital, 2),
            "session_pnl":         round(self.session_pnl, 2),
            "session_trades":      self.session_trades,
            "session_wins":        self.session_wins,
            "session_losses":      self.session_losses,
            "win_rate":            win_rate,
            "consecutive_loss":    self.consecutive_loss,
            "drawdown_pct":        drawdown,
            "daily_loss_used_pct": daily_loss_used,
            "trades_left_today":   max(0, self.max_trades_day - self.session_trades),
        }
