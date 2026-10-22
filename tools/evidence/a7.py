"""Evidence script for docs/IMPROVEMENT_PROPOSAL.md (section 3.2). Read-only; places no orders.
Run from repo root:  python tools/evidence/a7.py
"""
import sys, os, warnings, pickle, copy
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np, pandas as pd
from wallex_agent.sim.position import Plan
from wallex_agent.sim.single import simulate
from wallex_agent import combos as CB
from wallex_agent.backtest import run_account
from wallex_agent.policies import BaselinePolicy
from wallex_agent import metrics as MX
from wallex_agent.backtest import Ctx
ctx=Ctx(35)   # 35 days of 1m candles from data/candles (run wallex_agent.data.fetch first)
m,F,R,f=ctx.m,ctx.F,ctx.R,ctx.f
LIQ=["BTCTMN","PEPETMN","PUMPTMN","ETHTMN","ARBTMN","DOGETMN","SOLTMN","STMN","BNBTMN"]
liqset={m.symbols.index(s) for s in LIQ}
warm=200*60
cards=[c for c in ctx.cards["M1"] if c.sym in liqset and warm<=c.idx<=m.T-1500 and R.at(c.idx,c.sym)["btc_ret15"]>-0.005]
cut=np.quantile([c.idx for c in cards],0.6)
for hz in (1440,720,480,240):
    pl=Plan(5.0,[(6.0,1.0)],hz)
    res=[(c.idx<cut, simulate(m,f,F,c,pl,250000,horizon_min=hz)) for c in cards]
    a=np.array([r["pnl_pct"] for _,r in res if r["filled"]]); A=np.array([r["pnl_pct"] for h,r in res if r["filled"] and h]); B=np.array([r["pnl_pct"] for h,r in res if r["filled"] and not h])
    print(f"SL5 TP6 horizon {hz:4d}m: all {a.mean():+.2f}% (n={len(a)})  first60% {A.mean():+.2f}% (n={len(A)})  last40% {B.mean():+.2f}% (n={len(B)})")
# portfolio backtest with engine: inject combo C7
CB.COMBOS["C7"]=dict(name="unified test", scanners=["M1"], min_liq=0.0, coins=LIQ, size=250_000, size_band=(55_000,400_000),
    sl_band=(5.0,5.0), tp_band=(6.0,6.0), base_sl=5.0, base_tps=[(6.0,1.0)], max_pos=6, max_exposure=0.5, cluster_caps={"meme":3},
    daily_stop=0.04, entries_per_hour=4, sl_cooldown=(1,240), entry="market", time_stop=1440, regime_ok=None, need_btc_bull=False, dd_cap=0.05)
import wallex_agent.engine as EN; EN.COMBOS["C7"]=CB.COMBOS["C7"]
import wallex_agent.policies as PO
orig=ctx.cards["M1"]; ctx.cards["M1"]=[c for c in orig if R.at(c.idx,c.sym)["btc_ret15"]>-0.005]
T=m.T
acct=run_account(ctx,"C7",BaselinePolicy("C7",CB.COMBOS["C7"]),warm,T,start_cash=5_000_000)
mx=MX.compute(acct,warm,T,m.t)
print("FULL 25d C7 rule-only:", {k:mx[k] for k in ("R1_net_return_pct","D1_maxdd_peak_pct","D2_dd_from_start_pct","trades","win_rate_pct","R3_profit_factor","A1_trades_per_day","avg_hold_min","exit_reasons")})
# 1-day windows starting 22:30 Tehran each day (fresh account), 24.5h
starts=[j for j in range(warm,T-1470) if int((m.t[j]+12600)%86400)==22*3600+30*60]
rows=[]
for s0 in starts:
    a=run_account(ctx,"C7",BaselinePolicy("C7",CB.COMBOS["C7"]),s0,s0+1470,start_cash=5_000_000)
    w=MX.compute(a,s0,s0+1470,m.t); rows.append((w["R1_net_return_pct"],w["D2_dd_from_start_pct"],w["trades"]))
r=np.array(rows)
print("24.5h windows (22:30 start) n=",len(r)," ret% median",np.median(r[:,0]).round(2)," mean",r[:,0].mean().round(2)," worst",r[:,0].min().round(2)," best",r[:,0].max().round(2)," pos share",(r[:,0]>0).mean().round(2)," DD2 median",np.median(r[:,1]).round(2)," worst DD2",r[:,1].max().round(2)," trades median",np.median(r[:,2]))
print([round(x,2) for x in r[:,0]])
