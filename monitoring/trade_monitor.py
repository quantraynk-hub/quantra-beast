import datetime
LOT_SIZES = {"NIFTY":75,"BANKNIFTY":30,"FINNIFTY":65}
class TradeMonitor:
    def __init__(self, trade):
        self.trade=trade; self.entry_time=datetime.datetime.now()
        self.trailing_sl=False; self.partial_done=False
    def check(self, current_chain, current_signal_score):
        action=self.trade.get("action","BUY CE"); symbol=self.trade.get("symbol","NIFTY")
        lots=self.trade.get("lots",1); lot_size=LOT_SIZES.get(symbol,75)
        ce_strike=self.trade.get("ce_strike"); pe_strike=self.trade.get("pe_strike")
        ce_entry=self.trade.get("ce_price",0); pe_entry=self.trade.get("pe_price",0)
        ce_sl=self.trade.get("ce_sl",0); pe_sl=self.trade.get("pe_sl",0)
        ce_target=self.trade.get("ce_target",0); pe_target=self.trade.get("pe_target",0)
        max_loss=self.trade.get("max_loss",0); target_pnl=self.trade.get("target_pnl",0)
        ce_current=pe_current=0
        for c in (current_chain or []):
            if ce_strike and c["strike"]==ce_strike: ce_current=c.get("ce_ltp",0)
            if pe_strike and c["strike"]==pe_strike: pe_current=c.get("pe_ltp",0)
        if ce_current==0 and ce_entry>0: ce_current=ce_entry
        if pe_current==0 and pe_entry>0: pe_current=pe_entry
        pnl=0
        if action=="BUY CE" and ce_entry>0: pnl=(ce_current-ce_entry)*lots*lot_size
        elif action=="BUY PE" and pe_entry>0: pnl=(pe_current-pe_entry)*lots*lot_size
        elif action=="BUY CE + PE": pnl=((ce_current-ce_entry)+(pe_current-pe_entry))*lots*lot_size
        pnl_pct=round(pnl/max_loss*100,1) if max_loss>0 else 0
        elapsed=(datetime.datetime.now()-self.entry_time).seconds//60
        status="HOLD"; exit_reason=""; urgency="normal"
        if (action=="BUY CE" and ce_current>0 and ce_current<=ce_sl) or \
           (action=="BUY PE" and pe_current>0 and pe_current<=pe_sl):
            status="EXIT_SL"; exit_reason=f"SL HIT — P&L Rs.{pnl:.0f}"; urgency="critical"
        elif (action=="BUY CE" and ce_current>=ce_target) or \
             (action=="BUY PE" and pe_current>=pe_target) or \
             (action=="BUY CE + PE" and pnl>=target_pnl):
            status="EXIT_TARGET"; exit_reason=f"TARGET HIT Rs.{pnl:.0f}"; urgency="success"
        elif pnl>target_pnl*0.6 and not self.trailing_sl:
            self.trailing_sl=True; self.trade["ce_sl"]=ce_entry; self.trade["pe_sl"]=pe_entry
            status="TRAIL_SL"; exit_reason=f"SL trailed to entry — Rs.{pnl:.0f} protected"; urgency="info"
        elif pnl>target_pnl*0.5 and not self.partial_done and lots>=2:
            self.partial_done=True; status="BOOK_PARTIAL"
            exit_reason=f"Book {lots//2} lots — Rs.{pnl:.0f} profit"; urgency="action"
        elif current_signal_score*(1 if "CE" in action else -1)<-30:
            status="SIGNAL_REVERSED"; exit_reason=f"Signal reversed — consider exit Rs.{pnl:.0f}"; urgency="warning"
        elif elapsed>180 and abs(pnl_pct)<20:
            status="TIME_DECAY"; exit_reason=f"Stagnant {elapsed}min — time decay warning"; urgency="warning"
        return {"status":status,"urgency":urgency,"exit_reason":exit_reason,
                "pnl":round(pnl,2),"pnl_pct":pnl_pct,"elapsed_min":elapsed,
                "ce_current":ce_current,"pe_current":pe_current,
                "ce_sl":self.trade.get("ce_sl",ce_sl),"pe_sl":self.trade.get("pe_sl",pe_sl),
                "ce_target":ce_target,"pe_target":pe_target,
                "trailing_sl":self.trailing_sl,"partial_done":self.partial_done,
                "checked_at":datetime.datetime.now().isoformat()}
