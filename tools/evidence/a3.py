"""Evidence script for docs/IMPROVEMENT_PROPOSAL.md (section 3.2). Read-only; places no orders.
Run from repo root:  python tools/evidence/a3.py
"""
import sys, os, time, warnings, json, pickle
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np
from wallex_agent.sim.position import Plan
from wallex_agent.sim.single import simulate
from wallex_agent.backtest import Ctx
ctx=Ctx(35)   # 35 days of 1m candles from data/candles (run wallex_agent.data.fetch first)
m,F,R,f=ctx.m,ctx.F,ctx.R,ctx.f
LIQ=["BTCTMN","PEPETMN","PUMPTMN","ETHTMN","ARBTMN","DOGETMN","SOLTMN","STMN","BNBTMN"]
liqset={m.symbols.index(s) for s in LIQ}
u=m.symbols.index("USDTTMN"); b=m.symbols.index("BTCUSDT")
warm=200*60
def feat(c):
    j=c.idx; s=c.sym
    b15=int(np.searchsorted(F.tf[15].end_idx,j,side="right")-1)
    b60=int(np.searchsorted(F.tf[60].end_idx,j,side="right")-1)
    reg=R.at(j,s)
    cc=m.c[s]
    return dict(liq=float(np.nan_to_num(F.x[15]["liq"][s,b15])), reg=reg["coin"], btc15=reg["btc_ret15"]*100,
                btcbull=reg["btc_bull"], r24=(cc[j]/cc[j-1440]-1)*100 if j>=1440 else np.nan,
                u24=(m.c[u,j]/m.c[u,j-1440]-1)*100 if j>=1440 else np.nan, u4=(m.c[u,j]/m.c[u,j-240]-1)*100,
                v200=(F.tf[60].c[s,b60]/F.x[60]["ema200"][s,b60]-1)*100 if b60>=0 else np.nan,
                hour=int(((m.t[j]+12600)//3600)%24))
plans={"C1like":(Plan(sl_pct=4.0,tps=[(3.0,0.5),(6.0,0.5)],time_stop_min=1440),137500),
       "wide24":(Plan(sl_pct=5.0,tps=[(4.0,0.5),(8.0,0.5)],time_stop_min=1440),137500),
       "C2like":(Plan(sl_pct=3.0,tps=[(2.0,1.0)],time_stop_min=720),137500)}
rows=[]
for sc in ("M1","M4","M7","M2"):
    for c in ctx.cards[sc]:
        if c.sym not in liqset or c.idx<warm or c.idx>m.T-1500: continue
        d=feat(c); d["sc"]=sc; d["sym"]=m.symbols[c.sym]
        j=c.idx+1
        p0=m.o[c.sym,j]
        for h in (240,720,1440):
            d[f"g{h}"]=(m.c[c.sym,min(j+h,m.T-1)]/p0-1)*100
        for k,(pl,n) in plans.items():
            r=simulate(m,f,F,c,pl,n,horizon_min=1440)
            d[k]=r["pnl_pct"] if r["filled"] else np.nan
        rows.append(d)
import pandas as pd
df=pd.DataFrame(rows)
pd.set_option("display.width",200)
print("N cards (liquid, warm):", len(df), " days:", round((m.T-warm-1500)/1440,1))
print(df.groupby("sc")[["g240","g720","g1440","C1like","wide24","C2like"]].agg(["mean","count"]).round(2))
for sc in ("M1","M4"):
    d=df[df.sc==sc]
    print(f"\n===== {sc}: by liquidity bucket (share of minutes traded)")
    d["liqb"]=pd.cut(d.liq,[0,0.3,0.45,0.6,1.0])
    print(d.groupby("liqb")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))
    print(f"--- {sc}: by coin regime"); print(d.groupby("reg")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))
    d["btcb"]=pd.cut(d.btc15,[-9,-0.5,-0.15,0.15,9])
    print(f"--- {sc}: by BTCUSD 15m ret"); print(d.groupby("btcb")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))
    d["r24b"]=pd.cut(d.r24,[-99,-6,-3,0,99])
    print(f"--- {sc}: by coin 24h ret"); print(d.groupby("r24b")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))
    d["u24b"]=pd.cut(d.u24,[-9,-0.5,0.5,9])
    print(f"--- {sc}: by USDT/TMN 24h ret"); print(d.groupby("u24b")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))
    print(f"--- {sc}: by btc_bull(1h)"); print(d.groupby("btcbull")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))
    print(f"--- {sc}: by symbol"); print(d.groupby("sym")[["g720","C1like","wide24"]].agg(["mean","count"]).round(2))

