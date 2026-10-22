"""Evidence script for docs/IMPROVEMENT_PROPOSAL.md (section 3.2). Read-only; places no orders.
Run from repo root:  python tools/evidence/a6.py
"""
import sys, os, warnings, pickle
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np, pandas as pd
from wallex_agent.sim.position import Plan
from wallex_agent.sim.single import simulate
from wallex_agent.backtest import Ctx
ctx=Ctx(35)   # 35 days of 1m candles from data/candles (run wallex_agent.data.fetch first)
m,F,R,f=ctx.m,ctx.F,ctx.R,ctx.f
LIQ=["BTCTMN","PEPETMN","PUMPTMN","ETHTMN","ARBTMN","DOGETMN","SOLTMN","STMN","BNBTMN"]
liqset={m.symbols.index(s) for s in LIQ}
warm=200*60
atr=F.x[15]["atr"]; e15=F.tf[15].end_idx
def atrpct(c):
    b=int(np.searchsorted(e15,c.idx,side="right")-1); return float(atr[c.sym,b]/F.tf[15].c[c.sym,b]*100)
cards=[c for c in ctx.cards["M1"] if c.sym in liqset and warm<=c.idx<=m.T-1500 and R.at(c.idx,c.sym)["btc_ret15"]>-0.005]
print("n",len(cards), "ATR15% median", round(np.median([atrpct(c) for c in cards]),2), "p10/p90", np.percentile([atrpct(c) for c in cards],[10,90]).round(2))
def variant(k, lo, hi, tpm, horizon, risk_tmn=None, n=250000):
    out=[]
    for c in cards:
        sl=min(max(k*atrpct(c),lo),hi)
        tps=[(sl*a,b) for a,b in tpm] if tpm else []
        pl=Plan(sl_pct=sl,tps=tps,time_stop_min=horizon)
        notional=n if risk_tmn is None else min(max(risk_tmn/(sl/100),55000),600000)
        r=simulate(m,f,F,c,pl,notional,horizon_min=1440)
        if r["filled"]: out.append((r["pnl_pct"], r["pnl_tmn"], sl, notional))
    a=np.array(out)
    return dict(mean_pct=round(a[:,0].mean(),2), med=round(np.median(a[:,0]),2), win=round((a[:,0]>0).mean()*100), mean_tmn=round(a[:,1].mean()), sum_tmn=round(a[:,1].sum()), worst_tmn=round(a[:,1].min()), avg_sl=round(a[:,2].mean(),2), avg_size=round(a[:,3].mean()))
tests={
 "fixed SL5 TP6 24h (250k)":(0,5,5,[(1.2,1.0)],1440,None),
 "ATR k=3 [2.5,5] TP=1.2xSL 24h":(3,2.5,5,[(1.2,1.0)],1440,None),
 "ATR k=4 [2.5,5] TP=1.2xSL 24h":(4,2.5,5,[(1.2,1.0)],1440,None),
 "ATR k=4 [3,6] TP 1.0x/2.0x 24h":(4,3,6,[(1.0,0.5),(2.0,0.5)],1440,None),
 "ATR k=4 [2.5,5] noTP 24h":(4,2.5,5,[],1440,None),
 "ATR k=4 [2.5,5] TP=1.2xSL 12h":(4,2.5,5,[(1.2,1.0)],720,None),
 "ATR k=4 [2.5,5] TP1.2x risk 10k":(4,2.5,5,[(1.2,1.0)],1440,10000),
 "fixed SL3 TP3.6 24h (user 2-3%)":(0,3,3,[(1.2,1.0)],1440,None),
 "fixed SL2.5 TP3 24h":(0,2.5,2.5,[(1.2,1.0)],1440,None),
}
for k,v in tests.items(): print(f"{k:34s}", variant(*v))
