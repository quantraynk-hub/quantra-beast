"""
QUANTRA BEAST v2 — Production Server
All 7 engines + signal fusion + capital management + DB in one reliable file.
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, json, time, datetime, threading, os, uuid, sqlite3

app  = Flask(__name__)
CORS(app)

SCRAPER_KEY = os.environ.get("SCRAPER_API_KEY","620fb119d0569d21a4303effdd303228")
DB_PATH     = os.environ.get("DB_PATH","/tmp/quantra.db")
OWN_URL     = os.environ.get("RENDER_EXTERNAL_URL","http://localhost:5000")

# ─── CACHE ───────────────────────────────────
_cache = {}
def _cget(k):
    e=_cache.get(k)
    if not e or time.time()-e["ts"]>60: return None
    return e["d"]
def _cset(k,d): _cache[k]={"ts":time.time(),"d":d}

# ─── SCRAPER API ─────────────────────────────
def nse_get(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get("http://api.scraperapi.com", params={
                "api_key":SCRAPER_KEY,"url":url,
                "country_code":"in","render":"false","keep_headers":"true"
            }, timeout=30, headers={"Accept":"application/json"})
            t = r.text.strip()
            if t.startswith("<") or "<!doctype" in t.lower():
                print(f"[NSE] HTML on attempt {i+1}"); time.sleep(2*(i+1)); continue
            return r.json()
        except Exception as e:
            print(f"[NSE] Error {i+1}: {e}"); time.sleep(2)
    return None

# ─── FETCH INDICES ────────────────────────────
def fetch_indices():
    c = _cget("idx")
    if c: return c
    d = nse_get("https://www.nseindia.com/api/allIndices")
    if not d: return {"error":"failed"}
    r={}
    for item in d.get("data",[]):
        n=item.get("indexSymbol","")
        if n in {"NIFTY 50","NIFTY BANK","INDIA VIX","NIFTY FIN SERVICE"}:
            r[n]={"last":float(item.get("last",0)),"open":float(item.get("open",0)),
                  "high":float(item.get("high",0)),"low":float(item.get("low",0)),
                  "previousClose":float(item.get("previousClose",0)),
                  "change":float(item.get("variation",0)),
                  "pChange":float(item.get("percentChange",0))}
    _cset("idx",r); return r

# ─── FETCH OPTION CHAIN ───────────────────────
def fetch_chain(symbol="NIFTY"):
    k=f"oc_{symbol}"; c=_cget(k)
    if c: return c
    raw=nse_get(f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}")
    if not raw: return {"error":"failed","chain":[],"spot":0,"pcr":1,"atm_strike":0}
    rec=raw.get("records",{}); spot=float(rec.get("underlyingValue",0))
    exp=rec.get("expiryDates",[]); nearest=exp[0] if exp else None
    step=100 if symbol=="BANKNIFTY" else 50; atm=round(spot/step)*step
    chain=[]; tco=tcp=tcov=tcpv=0
    for item in rec.get("data",[]):
        if item.get("expiryDate")!=nearest: continue
        s=item.get("strikePrice",0); ce=item.get("CE",{}); pe=item.get("PE",{})
        coi=float(ce.get("openInterest",0)); poi=float(pe.get("openInterest",0))
        cvol=float(ce.get("totalTradedVolume",0)); pvol=float(pe.get("totalTradedVolume",0))
        tco+=coi; tcp+=poi; tcov+=cvol; tcpv+=pvol
        chain.append({"strike":int(s),"distance":int(s-atm),
            "ce_oi":int(coi),"ce_oi_change":int(ce.get("changeinOpenInterest",0)),
            "ce_ltp":float(ce.get("lastPrice",0)),"ce_iv":float(ce.get("impliedVolatility",0)),
            "ce_volume":int(cvol),
            "pe_oi":int(poi),"pe_oi_change":int(pe.get("changeinOpenInterest",0)),
            "pe_ltp":float(pe.get("lastPrice",0)),"pe_iv":float(pe.get("impliedVolatility",0)),
            "pe_volume":int(pvol)})
    chain.sort(key=lambda x:x["strike"])
    pcr=round(tcp/tco,3) if tco>0 else 1.0
    mp=_max_pain(chain)
    atm_ce_iv=next((c["ce_iv"] for c in chain if c["strike"]==atm),0)
    atm_pe_iv=next((c["pe_iv"] for c in chain if c["strike"]==atm),0)
    res=sorted([c for c in chain if c["ce_oi"]>0],key=lambda x:x["ce_oi"],reverse=True)[:3]
    sup=sorted([c for c in chain if c["pe_oi"]>0],key=lambda x:x["pe_oi"],reverse=True)[:3]
    r={"symbol":symbol,"spot":spot,"atm_strike":int(atm),"expiry":nearest,
       "pcr":pcr,"max_pain":int(mp),"total_ce_oi":int(tco),"total_pe_oi":int(tcp),
       "iv_skew":round(atm_pe_iv-atm_ce_iv,2),"atm_ce_iv":atm_ce_iv,"atm_pe_iv":atm_pe_iv,
       "ce_buildup":_buildup(chain,"ce"),"pe_buildup":_buildup(chain,"pe"),
       "ce_unwind":_unwind(chain,"ce"),"pe_unwind":_unwind(chain,"pe"),
       "resistance_strikes":res,"support_strikes":sup,"chain":chain,
       "fetched_at":datetime.datetime.now().isoformat()}
    _cset(k,r); print(f"[DATA] {symbol} spot={spot} pcr={pcr} atm={atm}"); return r

def _max_pain(chain):
    if not chain: return 0
    best=float("inf"); result=chain[0]["strike"]
    for t in [c["strike"] for c in chain]:
        p=sum(max(0,t-c["strike"])*c["ce_oi"]+max(0,c["strike"]-t)*c["pe_oi"] for c in chain)
        if p<best: best=p; result=t
    return result

def _buildup(chain,side):
    k=f"{side}_oi_change"
    return [{"strike":c["strike"],"oi_change":c[k],"ltp":c[f"{side}_ltp"]}
            for c in sorted([x for x in chain if x.get(k,0)>0],key=lambda x:x[k],reverse=True)[:3]]

def _unwind(chain,side):
    k=f"{side}_oi_change"
    return [{"strike":c["strike"],"oi_change":c[k]}
            for c in sorted([x for x in chain if x.get(k,0)<0],key=lambda x:x[k])[:3]]

# ─── FETCH FII/DII ────────────────────────────
def fetch_fii():
    c=_cget("fii")
    if c: return c
    d=nse_get("https://www.nseindia.com/api/fiidiiTradeReact")
    if not d: return {"fii_net":0,"dii_net":0,"fii_buy":0,"fii_sell":0,"dii_buy":0,"dii_sell":0}
    r={"fii_net":0.0,"fii_buy":0.0,"fii_sell":0.0,"dii_net":0.0,"dii_buy":0.0,"dii_sell":0.0}
    for item in d:
        cat=item.get("category","")
        try:
            buy=float(str(item.get("buyValue","0")).replace(",",""))
            sell=float(str(item.get("sellValue","0")).replace(",",""))
            net=float(str(item.get("netValue","0")).replace(",",""))
        except: buy=sell=net=0.0
        if "FII" in cat or "FPI" in cat:
            r["fii_buy"]+=buy; r["fii_sell"]+=sell; r["fii_net"]+=net
        elif "DII" in cat:
            r["dii_buy"]+=buy; r["dii_sell"]+=sell; r["dii_net"]+=net
    for k2 in r: r[k2]=round(r[k2],2)
    _cset("fii",r); print(f"[DATA] FII={r['fii_net']}Cr DII={r['dii_net']}Cr"); return r

# ─── 7 ENGINES ───────────────────────────────
def engine_trend(indices, oc):
    n=indices.get("NIFTY 50",{}); spot=oc.get("spot",0)
    pc=n.get("pChange",0); h=n.get("high",spot); l=n.get("low",spot)
    op=n.get("open",spot); prev=n.get("previousClose",spot)
    sc=0; rs=[]
    if pc>1.0:   sc+=40; rs.append(f"Strong bull momentum: NIFTY +{pc:.2f}%")
    elif pc>0.5: sc+=25; rs.append(f"Bullish: NIFTY +{pc:.2f}%")
    elif pc>0.1: sc+=12; rs.append(f"Mild up: NIFTY +{pc:.2f}%")
    elif pc>-0.1:rs.append(f"Flat: NIFTY {pc:.2f}%")
    elif pc>-0.5:sc-=12; rs.append(f"Mild down: NIFTY {pc:.2f}%")
    elif pc>-1.0:sc-=25; rs.append(f"Bearish: NIFTY {pc:.2f}%")
    else:        sc-=40; rs.append(f"Strong bear: NIFTY {pc:.2f}%")
    hl=h-l; rp=50
    if hl>0:
        rp=(spot-l)/hl*100
        if rp>80:   sc+=30; rs.append(f"At {rp:.0f}% of day range — bull strength")
        elif rp>60: sc+=15; rs.append(f"At {rp:.0f}% of range — mild bull")
        elif rp>40: rs.append(f"Mid-range ({rp:.0f}%) — neutral")
        elif rp>20: sc-=15; rs.append(f"At {rp:.0f}% of range — mild bear")
        else:       sc-=30; rs.append(f"At {rp:.0f}% of range — bear weakness")
    if prev>0:
        gp=(op-prev)/prev*100
        if gp>0.5:   sc+=20; rs.append(f"Gap up {gp:.2f}% — bullish open")
        elif gp<-0.5:sc-=20; rs.append(f"Gap down {gp:.2f}% — bearish open")
    sc=max(-100,min(100,sc))
    return {"name":"Trend","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"pchange":pc,"range_pos":round(rp,1)}}

def engine_options(oc):
    pcr=oc.get("pcr",1.0); spot=oc.get("spot",0); mp=oc.get("max_pain",spot)
    ivs=oc.get("iv_skew",0); ceb=oc.get("ce_buildup",[]); peb=oc.get("pe_buildup",[])
    sc=0; rs=[]
    if pcr>1.5:   sc+=40; rs.append(f"PCR {pcr} > 1.5 — extreme PUT writing, strong bull")
    elif pcr>1.3: sc+=30; rs.append(f"PCR {pcr} > 1.3 — heavy PUT writing, bullish")
    elif pcr>1.1: sc+=15; rs.append(f"PCR {pcr} — mild bullish (1.1–1.3)")
    elif pcr>0.9: rs.append(f"PCR {pcr} — neutral zone")
    elif pcr>0.7: sc-=20; rs.append(f"PCR {pcr} — CALL heavy, bearish cap")
    else:         sc-=40; rs.append(f"PCR {pcr} < 0.7 — extreme CALL writing, bearish")
    if mp>0 and spot>0:
        pd=(spot-mp)/mp*100
        if pd<-1:   sc+=30; rs.append(f"Spot {spot:.0f} well below max pain {mp} — strong upward pull")
        elif pd<-0.3:sc+=15; rs.append(f"Spot below max pain {mp} — upward pull")
        elif pd>1:  sc-=30; rs.append(f"Spot {spot:.0f} well above max pain {mp} — strong downward pull")
        elif pd>0.3:sc-=15; rs.append(f"Spot above max pain {mp} — downward pull")
        else:       rs.append(f"Spot near max pain {mp} — range bound")
    ca=sum(x.get("oi_change",0) for x in ceb); pa=sum(x.get("oi_change",0) for x in peb)
    if pa>0 and ca>0:
        ratio=pa/ca
        if ratio>2:   sc+=20; rs.append(f"PE OI {ratio:.1f}x > CE — PUT writing = bullish")
        elif ratio>1.2:sc+=10; rs.append(f"PE OI slightly > CE — mild bullish")
        elif ratio<0.5:sc-=20; rs.append(f"CE OI dominates — CALL writing = bearish cap")
        elif ratio<0.8:sc-=10; rs.append(f"CE OI slightly > PE — mild cap")
    if ivs>3:  sc-=10; rs.append(f"IV skew {ivs} — PUT IV high, fear of downside")
    elif ivs<-3:sc+=10; rs.append(f"IV skew {ivs} — CALL IV high, upside chase")
    sc=max(-100,min(100,sc))
    return {"name":"Options","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"pcr":pcr,"max_pain":mp,"iv_skew":ivs}}

def engine_gamma(oc):
    chain=oc.get("chain",[]); spot=oc.get("spot",0); atm=oc.get("atm_strike",0)
    if not chain or spot==0: return {"name":"Gamma","score":0,"signal":"NEUTRAL","confidence":0,"reasons":["No data"],"data":{}}
    sc=0; rs=[]; net_gex=0
    atm_ce=atm_pe=0
    for c in chain:
        dist=abs(c["strike"]-spot); w=max(0,1-dist/(spot*0.03))
        net_gex+=c["ce_oi"]*w - c["pe_oi"]*w
        if c["strike"]==atm: atm_ce=c["ce_oi"]; atm_pe=c["pe_oi"]
    if atm_ce+atm_pe>0:
        bias=(atm_pe-atm_ce)/(atm_pe+atm_ce)
        if bias>0.2:   sc+=25; rs.append(f"ATM PUT OI > CALL OI — support stronger than resistance")
        elif bias<-0.2:sc-=25; rs.append(f"ATM CALL OI > PUT OI — resistance stronger than support")
        else:          rs.append(f"ATM OI balanced at {atm}")
    top_ce=[c["strike"] for c in sorted(chain,key=lambda x:x["ce_oi"],reverse=True)[:2] if c["strike"]>=spot]
    top_pe=[c["strike"] for c in sorted(chain,key=lambda x:x["pe_oi"],reverse=True)[:2] if c["strike"]<=spot]
    nr=min(top_ce) if top_ce else spot+200
    ns=max(top_pe) if top_pe else spot-200
    dr=nr-spot; ds=spot-ns
    if dr<ds*0.5: sc-=15; rs.append(f"Resistance at {nr} close ({dr:.0f}pts) — limited upside")
    elif ds<dr*0.5:sc+=15; rs.append(f"Support at {ns} close ({ds:.0f}pts) — strong floor")
    sc=max(-100,min(100,sc))
    return {"name":"Gamma","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"net_gex":round(net_gex),"atm_ce":atm_ce,"atm_pe":atm_pe}}

def engine_volatility(indices, oc):
    vd=indices.get("INDIA VIX",{}); vix=vd.get("last",15); vc=vd.get("pChange",0)
    aiv=(oc.get("atm_ce_iv",vix)+oc.get("atm_pe_iv",vix))/2
    sc=0; rs=[]; regime="NORMAL"
    if vix<12:   sc+=20; rs.append(f"VIX {vix:.1f} — very low, cheap options, buy directional"); regime="LOW"
    elif vix<16: sc+=10; rs.append(f"VIX {vix:.1f} — normal, standard premiums"); regime="NORMAL"
    elif vix<20: sc-=10; rs.append(f"VIX {vix:.1f} — elevated, expensive premium"); regime="HIGH"
    elif vix<25: sc-=25; rs.append(f"VIX {vix:.1f} — high fear, caution"); regime="FEAR"
    else:        sc-=40; rs.append(f"VIX {vix:.1f} — extreme fear, avoid buying"); regime="PANIC"
    if vc>5:    sc-=20; rs.append(f"VIX rising {vc:.1f}% — increasing fear")
    elif vc<-5: sc+=20; rs.append(f"VIX falling {vc:.1f}% — fear reducing")
    sc=max(-100,min(100,sc))
    return {"name":"Volatility","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"vix":vix,"vix_change":vc,"regime":regime,"avg_iv":round(aiv,1)}}

def engine_regime(indices, oc):
    n=indices.get("NIFTY 50",{}); vix=indices.get("INDIA VIX",{}).get("last",15)
    pc=n.get("pChange",0); h=n.get("high",1); l=n.get("low",1); pcr=oc.get("pcr",1)
    dr=(h-l)/l*100 if l>0 else 0; sc=0; rs=[]; mult=1.0
    if vix<16 and abs(pc)>0.5:
        regime="TRENDING"; sc=30 if pc>0 else -30; mult=1.1
        rs.append(f"Low VIX + directional move — trending, signals reliable")
    elif vix>20 and dr>1.5:
        regime="VOLATILE"; sc=-20; mult=0.7
        rs.append(f"High VIX + wide range — volatile, use caution")
    elif vix<14 and dr<0.5:
        regime="RANGING"; sc=0; mult=0.8
        rs.append(f"Low VIX + tight range — pinned, range bound")
    elif abs(pc)>1.0:
        regime="STRONG_TREND"; sc=40 if pc>0 else -40; mult=1.2
        rs.append(f"Strong {'+' if pc>0 else ''}{pc:.2f}% move — high conviction trend")
    else:
        regime="MIXED"; sc=0; mult=0.9
        rs.append(f"Mixed signals — require confluence before trading")
    if pcr>1.3 and pc>0:  rs.append("PCR + trend both bullish — bull regime confirmed")
    elif pcr<0.8 and pc<0:rs.append("PCR + trend both bearish — bear regime confirmed")
    sc=max(-100,min(100,sc))
    return {"name":"Regime","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"regime":regime,"mult":mult,"vix":vix,"day_range":round(dr,2)}}

def engine_sentiment(fii):
    fn=fii.get("fii_net",0); dn=fii.get("dii_net",0)
    fb=fii.get("fii_buy",0); fs=fii.get("fii_sell",0)
    sc=0; rs=[]
    if fn>2000:   sc+=60; rs.append(f"FII NET BUY ₹{fn:.0f}Cr — very strong institutional buying")
    elif fn>1000: sc+=45; rs.append(f"FII NET BUY ₹{fn:.0f}Cr — strong buying")
    elif fn>500:  sc+=25; rs.append(f"FII buying ₹{fn:.0f}Cr — moderate support")
    elif fn>0:    sc+=10; rs.append(f"FII mildly positive ₹{fn:.0f}Cr")
    elif fn>-500: sc-=10; rs.append(f"FII mild selling ₹{abs(fn):.0f}Cr")
    elif fn>-1000:sc-=25; rs.append(f"FII SELLING ₹{abs(fn):.0f}Cr")
    elif fn>-2000:sc-=45; rs.append(f"FII HEAVY SELLING ₹{abs(fn):.0f}Cr")
    else:         sc-=60; rs.append(f"FII EXTREME SELLING ₹{abs(fn):.0f}Cr")
    if dn>1000:  sc+=30; rs.append(f"DII buying ₹{dn:.0f}Cr — domestic support")
    elif dn>500: sc+=15; rs.append(f"DII buying ₹{dn:.0f}Cr")
    elif dn<-500:sc-=15; rs.append(f"DII selling ₹{abs(dn):.0f}Cr")
    if fn>0 and dn>0: sc+=10; rs.append("Both FII + DII buying — strong bull")
    elif fn<0 and dn<0:sc-=10; rs.append("Both FII + DII selling — strong bear")
    if fb+abs(fs)>0: rs.append(f"FII buy ratio: {fb/(fb+abs(fs))*100:.0f}%")
    sc=max(-100,min(100,sc))
    return {"name":"Sentiment","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"fii_net":fn,"dii_net":dn}}

def engine_flow(oc):
    chain=oc.get("chain",[]); spot=oc.get("spot",0)
    ceb=oc.get("ce_buildup",[]); peb=oc.get("pe_buildup",[])
    ceu=oc.get("ce_unwind",[]); peu=oc.get("pe_unwind",[])
    if not chain or spot==0: return {"name":"Flow","score":0,"signal":"NEUTRAL","confidence":0,"reasons":["No data"],"data":{}}
    sc=0; rs=[]
    ca=sum(x.get("oi_change",0) for x in ceb); pa=sum(x.get("oi_change",0) for x in peb)
    tot=pa+ca
    if tot>0:
        ppct=pa/tot*100
        if ppct>70:    sc+=40; rs.append(f"Smart money writing PUTs ({ppct:.0f}%) — bullish flow")
        elif ppct>55:  sc+=20; rs.append(f"PE OI addition dominates ({ppct:.0f}%) — mild bullish")
        elif ppct<30:  sc-=40; rs.append(f"Smart money writing CALLs ({100-ppct:.0f}%) — bearish flow")
        elif ppct<45:  sc-=20; rs.append(f"CE OI dominates ({100-ppct:.0f}%) — mild bearish")
        else:          rs.append(f"Balanced OI addition — no directional flow")
    cu=abs(sum(x.get("oi_change",0) for x in ceu)); pu=abs(sum(x.get("oi_change",0) for x in peu))
    if pu>cu*1.5:  sc-=25; rs.append(f"PUTs unwinding — put writers exiting, bearish")
    elif cu>pu*1.5:sc+=25; rs.append(f"CALLs unwinding — call writers exiting, bullish")
    otc=[c for c in chain if c["strike"]>spot*1.02]; otp=[c for c in chain if c["strike"]<spot*0.98]
    ocv=sum(c["ce_volume"] for c in otc); opv=sum(c["pe_volume"] for c in otp)
    if ocv+opv>0:
        cpct=ocv/(ocv+opv)*100
        if cpct>65:  sc+=20; rs.append(f"High OTM CE buying ({cpct:.0f}%) — upside chase")
        elif cpct<35:sc-=20; rs.append(f"High OTM PE buying ({100-cpct:.0f}%) — downside hedge")
    sc=max(-100,min(100,sc))
    return {"name":"Flow","score":round(sc,1),"signal":"BULL" if sc>15 else "BEAR" if sc<-15 else "NEUTRAL","confidence":min(95,abs(sc)),"reasons":rs,"data":{"ce_add":ca,"pe_add":pa}}

# ─── SIGNAL FUSION ────────────────────────────
WEIGHTS={"trend":0.18,"options":0.20,"gamma":0.12,"volatility":0.10,"regime":0.08,"sentiment":0.15,"flow":0.17}

def generate_signal(snapshot):
    idx=snapshot["indices"]; oc=snapshot["option_data"]; fii=snapshot["fii_dii"]
    e={
        "trend":     engine_trend(idx,oc),
        "options":   engine_options(oc),
        "gamma":     engine_gamma(oc),
        "volatility":engine_volatility(idx,oc),
        "regime":    engine_regime(idx,oc),
        "sentiment": engine_sentiment(fii),
        "flow":      engine_flow(oc),
    }
    composite=sum(e[k]["score"]*WEIGHTS.get(k,0) for k in e)
    mult=e["regime"]["data"].get("mult",1.0)
    confidence=round(min(97,max(35,abs(composite)*mult)),1)
    bull=sum(1 for v in e.values() if v["signal"]=="BULL")
    bear=sum(1 for v in e.values() if v["signal"]=="BEAR")
    conf=bull if composite>0 else bear
    if abs(composite)<25 or conf<3:
        action,cls="WAIT","wait"
        sub=f"Low confluence ({conf}/7) — wait for clearer setup"
    elif composite>50:  action,cls="BUY CE","bull"; sub=f"STRONG BULL — {conf}/7 engines aligned"
    elif composite>25:  action,cls="BUY CE","bull"; sub=f"BULL — {conf}/7 engines, moderate confidence"
    elif composite<-50: action,cls="BUY PE","bear"; sub=f"STRONG BEAR — {conf}/7 engines aligned"
    elif composite<-25: action,cls="BUY PE","bear"; sub=f"BEAR — {conf}/7 engines, moderate confidence"
    else:               action,cls="WAIT","wait"; sub="Borderline — wait for confirmation"
    reasons=[]
    for k in ["options","sentiment","flow","trend","gamma","volatility","regime"]:
        if e[k]["reasons"]: reasons.append(e[k]["reasons"][0])
    spot=oc.get("spot",0); mp=oc.get("max_pain",spot); atm=oc.get("atm_strike",0)
    return {
        "signal_id":str(uuid.uuid4())[:8],"timestamp":datetime.datetime.now().isoformat(),
        "symbol":snapshot.get("symbol","NIFTY"),
        "action":action,"signal_cls":cls,"signal_sub":sub,
        "composite":round(composite,2),"confidence":confidence,
        "bull_count":bull,"bear_count":bear,"confluence":conf,
        "engines":{k:{"score":v["score"],"signal":v["signal"],"confidence":v["confidence"]} for k,v in e.items()},
        "engine_reasons":{k:v["reasons"] for k,v in e.items()},
        "top_reasons":reasons[:6],
        "market_data":{"spot":spot,"atm":atm,"max_pain":mp,
                       "pcr":oc.get("pcr",0),"vix":idx.get("INDIA VIX",{}).get("last",0),
                       "fii_net":fii.get("fii_net",0),"expiry":oc.get("expiry",""),
                       "resistances":[c["strike"] for c in oc.get("resistance_strikes",[])],
                       "supports":[c["strike"] for c in oc.get("support_strikes",[])]},
        "chain":oc.get("chain",[]),
    }

# ─── CAPITAL MANAGER ─────────────────────────
LOT_SIZES={"NIFTY":75,"BANKNIFTY":30,"FINNIFTY":65}
MARGINS={"NIFTY":6500,"BANKNIFTY":11000,"FINNIFTY":6000}
SL_PCT=0.40

class CapMgr:
    def __init__(self,cfg):
        self.total=cfg.get("total_capital",50000); self.risk_pct=cfg.get("risk_per_trade",2.0)
        self.rr=cfg.get("rr_ratio",2.0); self.max_trades=cfg.get("max_trades_day",3)
        self.avail=self.total; self.in_trade=0.0; self.peak=self.total
        self.ses_trades=0; self.ses_pnl=0.0; self.ses_wins=0; self.ses_loss=0; self.consec=0
        self.date=datetime.date.today(); self.log=[]
    def _chk_day(self):
        if datetime.date.today()!=self.date:
            self.ses_trades=self.ses_pnl=self.ses_wins=self.ses_loss=self.consec=0
            self.date=datetime.date.today()
    def can_trade(self):
        self._chk_day()
        if self.ses_trades>=self.max_trades: return {"allowed":False,"reason":f"Max {self.max_trades} trades reached"}
        dlim=self.total*0.03
        if self.ses_pnl<=-dlim: return {"allowed":False,"reason":f"Daily loss limit ₹{dlim:.0f} hit"}
        dd=(self.peak-self.avail)/self.peak*100 if self.peak>0 else 0
        if dd>=10: return {"allowed":False,"reason":f"Drawdown {dd:.1f}% — system paused"}
        return {"allowed":True,"reason":"OK"}
    def calc_position(self,sig,oc):
        sym=sig.get("symbol","NIFTY"); action=sig.get("action","BUY CE")
        spot=sig.get("market_data",{}).get("spot",0); atm=sig.get("market_data",{}).get("atm",round(spot/50)*50)
        chain=sig.get("chain",[]); exp=sig.get("market_data",{}).get("expiry","")
        ls=LOT_SIZES.get(sym,75); mg=MARGINS.get(sym,6500)
        cp=pp=0
        for c in chain:
            if c["strike"]==atm: cp=c.get("ce_ltp",0); pp=c.get("pe_ltp",0); break
        if cp==0: cp=round(spot*0.0055,1)
        if pp==0: pp=round(spot*0.0050,1)
        rm=0.5 if self.consec>=2 else 1.0
        risk=self.avail*(self.risk_pct/100)*rm
        pr=cp if action=="BUY CE" else pp if action=="BUY PE" else (cp+pp)/2
        pls=pr*SL_PCT*ls; lots=max(1,int(risk/pls)) if pls>0 else 1
        mult=2 if "+" in action else 1
        return {"action":action,"symbol":sym,"expiry":exp,
                "ce_strike":atm if "CE" in action else None,"pe_strike":atm if "PE" in action else None,
                "ce_price":cp,"pe_price":pp,
                "ce_sl":round(cp*(1-SL_PCT),1),"ce_target":round(cp*(1+self.rr*SL_PCT),1),
                "pe_sl":round(pp*(1-SL_PCT),1),"pe_target":round(pp*(1+self.rr*SL_PCT),1),
                "lots":lots,"lot_size":ls,"capital_used":int(lots*mg*mult),
                "max_loss":int(lots*pr*ls*SL_PCT*mult),"target_pnl":int(lots*pr*ls*SL_PCT*mult*self.rr),
                "risk_reduced":rm<1.0,"spot":spot,"atm":atm}
    def record(self,pnl):
        self.ses_trades+=1; self.ses_pnl+=pnl; self.avail+=pnl; self.in_trade=0
        if pnl>0: self.ses_wins+=1; self.consec=0
        else: self.ses_loss+=1; self.consec+=1
        if self.avail>self.peak: self.peak=self.avail
        self.log.append({"ts":datetime.datetime.now().isoformat(),"pnl":pnl,"bal":round(self.avail,2),"r":"WIN" if pnl>0 else "LOSS"})
    def status(self):
        wr=round(self.ses_wins/self.ses_trades*100,1) if self.ses_trades>0 else 0
        dlp=round(abs(min(0,self.ses_pnl))/(self.total*0.03)*100,1) if self.ses_pnl<0 else 0
        dd=round((self.peak-self.avail)/self.peak*100,2) if self.peak>0 else 0
        return {"total_capital":self.total,"available_capital":round(self.avail,2),
                "in_trade_capital":round(self.in_trade,2),"session_pnl":round(self.ses_pnl,2),
                "session_trades":self.ses_trades,"session_wins":self.ses_wins,"session_losses":self.ses_loss,
                "win_rate":wr,"consecutive_loss":self.consec,"drawdown_pct":dd,
                "daily_loss_used_pct":dlp,"trades_left_today":max(0,self.max_trades-self.ses_trades)}

# ─── DB ───────────────────────────────────────
def init_db():
    c=sqlite3.connect(DB_PATH); cur=c.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,signal_id TEXT,symbol TEXT,action TEXT,
        ce_strike INTEGER,pe_strike INTEGER,lots INTEGER,
        ce_entry REAL,pe_entry REAL,ce_exit REAL,pe_exit REAL,
        capital_used INTEGER,max_loss INTEGER,target_pnl INTEGER,actual_pnl REAL,
        exit_type TEXT,entry_time TEXT,exit_time TEXT,capital_before REAL,capital_after REAL);
    """); c.commit(); c.close()

def save_trade_db(t):
    c=sqlite3.connect(DB_PATH); cur=c.cursor()
    cur.execute("INSERT INTO trades(signal_id,symbol,action,ce_strike,pe_strike,lots,ce_entry,pe_entry,capital_used,max_loss,target_pnl,entry_time,capital_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t.get("signal_id",""),t.get("symbol"),t.get("action"),t.get("ce_strike"),t.get("pe_strike"),
         t.get("lots"),t.get("ce_price"),t.get("pe_price"),t.get("capital_used"),t.get("max_loss"),
         t.get("target_pnl"),datetime.datetime.now().isoformat(),t.get("capital_before",0)))
    tid=cur.lastrowid; c.commit(); c.close(); return tid

def close_trade_db(tid,ex):
    c=sqlite3.connect(DB_PATH); cur=c.cursor()
    cur.execute("UPDATE trades SET ce_exit=?,pe_exit=?,actual_pnl=?,exit_type=?,exit_time=?,capital_after=? WHERE id=?",
        (ex.get("ce_exit"),ex.get("pe_exit"),ex.get("pnl"),ex.get("exit_type"),
         datetime.datetime.now().isoformat(),ex.get("capital_after"),tid))
    c.commit(); c.close()

def get_history():
    try:
        c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; cur=c.cursor()
        rows=cur.execute("SELECT * FROM trades ORDER BY entry_time DESC LIMIT 50").fetchall()
        perf=cur.execute("SELECT COUNT(*) t,SUM(CASE WHEN actual_pnl>0 THEN 1 ELSE 0 END) w,SUM(actual_pnl) p FROM trades WHERE actual_pnl IS NOT NULL").fetchone()
        c.close()
        trades=[dict(r) for r in rows]
        pd=dict(perf) if perf else {}
        if pd.get("t",0)>0: pd["win_rate"]=round(pd["w"]/pd["t"]*100,1)
        return trades,pd
    except: return [],{}

# ─── GLOBAL STATE ─────────────────────────────
risk_mgr=None; active_monitor=None; active_tid=None; last_signal=None; last_pos=None

init_db()

# ─── SELF PING ────────────────────────────────
def self_ping():
    time.sleep(30)
    while True:
        try: requests.get(f"{OWN_URL}/ping",timeout=10); print(f"[PING] {datetime.datetime.now().strftime('%H:%M')}")
        except: pass
        time.sleep(600)
threading.Thread(target=self_ping,daemon=True).start()

# ─── ROUTES ──────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status":"alive","version":"2.0","time":str(datetime.datetime.now())})

@app.route("/generate-signal",methods=["POST"])
def gen_signal():
    global risk_mgr,last_signal,last_pos
    b=request.get_json(force=True) or {}
    sym=b.get("symbol","NIFTY").upper()
    cfg={"total_capital":b.get("capital",50000),"risk_per_trade":b.get("risk_pct",2.0),
         "rr_ratio":b.get("rr_ratio",2.0),"max_trades_day":b.get("max_trades",3)}
    if risk_mgr is None: risk_mgr=CapMgr(cfg)
    ct=risk_mgr.can_trade()
    idx=fetch_indices(); oc=fetch_chain(sym); fii=fetch_fii()
    if "error" in oc: return jsonify({"error":f"Data fetch failed: {oc['error']}"}),503
    snap={"symbol":sym,"indices":idx,"option_data":oc,"fii_dii":fii}
    sig=generate_signal(snap); last_signal=sig
    pos=None
    if sig["action"]!="WAIT" and ct["allowed"]:
        pos=risk_mgr.calc_position(sig,oc); last_pos=pos
    return jsonify({"signal":sig,"position":pos,"capital":risk_mgr.status(),
                    "can_trade":ct,"indices":idx,"fii_dii":fii,
                    "chain":oc.get("chain",[]),
                    "option_meta":{k:v for k,v in oc.items() if k!="chain"}})

@app.route("/trade/enter",methods=["POST"])
def trade_enter():
    global active_monitor,active_tid
    b=request.get_json(force=True) or {}
    pos=b.get("position",{}); sig=b.get("signal",last_signal or {})
    if not pos: return jsonify({"error":"No position"}),400
    if risk_mgr is None: return jsonify({"error":"Not initialized"}),400
    pos["signal_id"]=sig.get("signal_id",""); pos["capital_before"]=risk_mgr.avail
    risk_mgr.in_trade=pos.get("capital_used",0)
    tid=save_trade_db(pos); active_tid=tid
    active_monitor={"trade":pos,"entry":datetime.datetime.now(),"trailing":False,"partial":False}
    return jsonify({"trade_id":tid,"message":"Trade recorded"})

@app.route("/trade/monitor")
def trade_monitor():
    if not active_monitor: return jsonify({"status":"NO_ACTIVE_TRADE"})
    pos=active_monitor["trade"]; action=pos.get("action","BUY CE")
    sym=pos.get("symbol","NIFTY"); ls=LOT_SIZES.get(sym,75)
    lots=pos.get("lots",1); ce_e=pos.get("ce_price",0); pe_e=pos.get("pe_price",0)
    ce_sl=pos.get("ce_sl",0); pe_sl=pos.get("pe_sl",0)
    ce_tgt=pos.get("ce_target",0); pe_tgt=pos.get("pe_target",0)
    max_loss=pos.get("max_loss",0); tpnl=pos.get("target_pnl",0)
    oc=fetch_chain(sym); chain=oc.get("chain",[])
    ce_s=ce_e; pe_s=pe_e
    cs=pos.get("ce_strike"); ps=pos.get("pe_strike")
    for c in chain:
        if cs and c["strike"]==cs: ce_s=c.get("ce_ltp",ce_e)
        if ps and c["strike"]==ps: pe_s=c.get("pe_ltp",pe_e)
    pnl=0
    if action=="BUY CE": pnl=(ce_s-ce_e)*lots*ls
    elif action=="BUY PE": pnl=(pe_s-pe_e)*lots*ls
    else: pnl=((ce_s-ce_e)+(pe_s-pe_e))*lots*ls
    elapsed=int((datetime.datetime.now()-active_monitor["entry"]).total_seconds()//60)
    pct=round(pnl/max_loss*100,1) if max_loss else 0
    status="HOLD"; reason=""; urgency="normal"
    if (action=="BUY CE" and ce_s>0 and ce_s<=ce_sl) or (action=="BUY PE" and pe_s>0 and pe_s<=pe_sl):
        status="EXIT_SL"; reason=f"SL HIT — P&L ₹{pnl:.0f}"; urgency="critical"
    elif (action=="BUY CE" and ce_s>=ce_tgt) or (action=="BUY PE" and pe_s>=pe_tgt) or (action=="BUY CE + PE" and pnl>=tpnl):
        status="EXIT_TARGET"; reason=f"TARGET HIT ₹{pnl:.0f}"; urgency="success"
    elif pnl>tpnl*0.6 and not active_monitor["trailing"]:
        active_monitor["trailing"]=True; pos["ce_sl"]=ce_e; pos["pe_sl"]=pe_e
        status="TRAIL_SL"; reason=f"SL trailed to entry — ₹{pnl:.0f} protected"; urgency="info"
    elif pnl>tpnl*0.5 and not active_monitor["partial"] and lots>=2:
        active_monitor["partial"]=True; status="BOOK_PARTIAL"
        reason=f"Book {lots//2} lots — ₹{pnl:.0f} profit"; urgency="action"
    elif elapsed>180 and abs(pct)<20:
        status="TIME_DECAY"; reason=f"Stagnant {elapsed}min — time decay warning"; urgency="warning"
    return jsonify({"status":status,"urgency":urgency,"exit_reason":reason,"pnl":round(pnl,2),
                    "pnl_pct":pct,"elapsed_min":elapsed,"ce_current":ce_s,"pe_current":pe_s,
                    "ce_sl":pos.get("ce_sl",ce_sl),"pe_sl":pos.get("pe_sl",pe_sl),
                    "ce_target":ce_tgt,"pe_target":pe_tgt})

@app.route("/trade/exit",methods=["POST"])
def trade_exit():
    global active_monitor,active_tid
    b=request.get_json(force=True) or {}
    pnl=float(b.get("pnl",0)); etype=b.get("exit_type","MANUAL")
    if risk_mgr is None: return jsonify({"error":"Not initialized"}),400
    risk_mgr.record(pnl)
    if active_tid:
        close_trade_db(active_tid,{"pnl":pnl,"exit_type":etype,"capital_after":risk_mgr.avail})
    active_monitor=None; active_tid=None
    return jsonify({"message":f"Closed ₹{pnl:.0f}","capital":risk_mgr.status(),"next_can_trade":risk_mgr.can_trade()})

@app.route("/capital")
def capital():
    if not risk_mgr: return jsonify({"error":"Not initialized"})
    return jsonify(risk_mgr.status())

@app.route("/capital/reset",methods=["POST"])
def cap_reset():
    global risk_mgr
    b=request.get_json(force=True) or {}
    risk_mgr=CapMgr({"total_capital":b.get("capital",50000),"risk_per_trade":b.get("risk_pct",2.0),
                      "rr_ratio":b.get("rr_ratio",2.0),"max_trades_day":b.get("max_trades",3)})
    return jsonify({"message":"Reset","capital":risk_mgr.status()})

@app.route("/history")
def history():
    trades,perf=get_history()
    return jsonify({"trades":trades,"performance":perf})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=5000,debug=False)
