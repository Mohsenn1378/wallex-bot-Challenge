"""Evidence script for docs/IMPROVEMENT_PROPOSAL.md (section 3.2). Read-only; places no orders.
Run from repo root:  python tools/evidence/a4.py
"""
import sys, os, time, warnings, json, pickle
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np, pandas as pd
from wallex_agent.sim.position import Plan
from wallex_agent.sim.single import simulate
from wallex_agent.scanners import Card
from wallex_agent.backtest import Ctx
ctx=Ctx(35)   # 35 days of 1m candles from data/candles (run wallex_agent.data.fetch first)
m,F,R,f=ctx.m,ctx.F,ctx.R,ctx.f
LIQ=["BTCTMN","PEPETMN","PUMPTMN","ETHTMN","ARBTMN","DOGETMN","SOLTMN","STMN","BNBTMN"]
liqset={m.symbols.index(s) for s in LIQ}
warm=200*60
# random baseline drift on liquid coins
rng=np.random.default_rng(0); g={240:[],720:[],1440:[]}
for _ in range(4000):
    s=rng.choice(list(liqset)); j=int(rng.integers(warm, m.T-1500))
    p0=m.o[s,j]
    for h in g: g[h].append(m.c[s,j+h]/p0-1)
print("RANDOM liquid drift % 4h/12h/24h:", [round(np.mean(g[h])*100,2) for h in g])
plans={
 "SL4 TP3/6 24h (C1)":Plan(4.0,[(3.0,0.5),(6.0,0.5)],1440),
 "SL3 TP2 12h (C2)":Plan(3.0,[(2.0,1.0)],720),
 "SL6 noTP 12h":Plan(6.0,[],720),
 "SL8 noTP 24h":Plan(8.0,[],1440),
 "SL5 TP4 24h":Plan(5.0,[(4.0,1.0)],1440),
 "SL5 TP6 24h":Plan(5.0,[(6.0,1.0)],1440),
 "SL6 TP3/8 24h":Plan(6.0,[(3.0,0.5),(8.0,0.5)],1440),
 "SL5 TP2.5/6 BE1.5 24h":Plan(5.0,[(2.5,0.5),(6.0,0.5)],1440,be_trigger_pct=1.5),
 "SL4 TP3 trail2ATR 24h":Plan(4.0,[(3.0,0.5)],1440,trail_atr_mult=2.0),
 "SL3 TP3 12h":Plan(3.0,[(3.0,1.0)],720),
 "SL2.5 TP2.5 8h":Plan(2.5,[(2.5,1.0)],480),
}
def run(sc, filt=None, n=137500):
    out={}
    cards=[c for c in ctx.cards[sc] if c.sym in liqset and warm<=c.idx<=m.T-1500 and (filt is None or filt(c))]
    for k,pl in plans.items():
        r=[simulate(m,f,F,c,pl,n,horizon_min=1440) for c in cards]
        pp=np.array([x["pnl_pct"] for x in r if x["filled"]])
        reasons=pd.Series([x["reason"] for x in r if x["filled"]]).value_counts(normalize=True).round(2).to_dict()
        out[k]=(round(pp.mean(),2), round(np.median(pp),2), round((pp>0).mean()*100), round(pp.min(),1), len(pp), {k2:v for k2,v in list(reasons.items())[:3]})
    return out
def btc_ok(c):
    reg=R.at(c.idx,c.sym); return reg["btc_ret15"]>-0.005
for sc,fl,lab in (("M1",None,"all"),("M1",btc_ok,"btc15>-0.5%"),("M4",None,"all"),("M4",btc_ok,"btc15>-0.5%")):
    print(f"\n==== {sc} liquid [{lab}]  (mean%, median%, win%, worst%, n, exit mix)")
    for k,v in run(sc,fl).items(): print(f"  {k:24s} {v}")
