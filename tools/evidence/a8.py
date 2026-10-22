"""Evidence script for docs/IMPROVEMENT_PROPOSAL.md (section 3.2). Read-only; places no orders.
Run from repo root:  python tools/evidence/a8.py
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
allm1=sorted([c for c in ctx.cards["M1"] if c.sym in liqset and warm<=c.idx<=m.T-1500], key=lambda c:c.idx)
last={}
rows=[]
pl=Plan(5.0,[(6.0,1.0)],1440)
for c in allm1:
    reg=R.at(c.idx,c.sym); j=c.idx; s=c.sym; cc=m.c[s]
    prev=[i for i in last.get(s,[]) if j-i<=360]
    first = len(prev)==0
    last.setdefault(s,[]).append(j)
    hi6=m.h[s,max(0,j-360):j+1].max()
    r=simulate(m,f,F,c,pl,250000,horizon_min=1440)
    p0=m.o[s,j+1]
    rows.append(dict(sym=m.symbols[s], btc15=reg["btc_ret15"]*100, btcbull=reg["btc_bull"], reg=reg["coin"],
        r24=(cc[j]/cc[j-1440]-1)*100, dd6=(cc[j]/hi6-1)*100, first=first, nprev=len(prev),
        hour=int(((m.t[j]+12600)//3600)%24), g720=(m.c[s,j+720]/p0-1)*100, net=r["pnl_pct"] if r["filled"] else np.nan, idx=j))
df=pd.DataFrame(rows)
cut=df.idx.quantile(0.6); df["half"]=np.where(df.idx<cut,"A","B")
d=df[df.btc15>-0.5]
def show(col, bins=None):
    x=d.copy()
    if bins is not None: x[col]=pd.cut(x[col],bins)
    g=x.groupby(col).agg(n=("net","size"), gross12h=("g720","mean"), net_SL5TP6=("net","mean"), win=("net",lambda v:(v>0).mean()*100))
    gh=x.groupby([col,"half"])["net"].mean().unstack().round(2)
    print(pd.concat([g.round(2),gh],axis=1).to_string()); print()
print("ALL (btc15>-0.5):", len(d), "net", round(d.net.mean(),2), "gross12h", round(d.g720.mean(),2)); print()
show("first"); show("nprev",[-1,0,1,3,20]); show("dd6",[-50,-8,-5,-3,0]); show("r24",[-99,-6,-3,0,99]); show("btcbull"); show("reg"); show("hour",[-1,5,11,17,23])
x=d.copy(); x["liqsym"]=x.sym; print(x.groupby("liqsym").agg(n=("net","size"),net=("net","mean"),g12=("g720","mean")).round(2).T.to_string())
print("excluded BTC-crash cards:", (df.btc15<=-0.5).sum(), "net", round(df[df.btc15<=-0.5].net.mean(),2))
