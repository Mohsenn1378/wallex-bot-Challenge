"""Evidence script for docs/IMPROVEMENT_PROPOSAL.md (section 3.2). Read-only; places no orders.
Run from repo root:  python tools/evidence/a9.py
"""
import sys, os, warnings, pickle
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np, pandas as pd
from wallex_agent import combos as CB
import wallex_agent.engine as EN
from wallex_agent.backtest import run_account
from wallex_agent.policies import BaselinePolicy
from wallex_agent import metrics as MX
from wallex_agent.backtest import Ctx
ctx=Ctx(35)   # 35 days of 1m candles from data/candles (run wallex_agent.data.fetch first)
m,F,R,f=ctx.m,ctx.F,ctx.R,ctx.f
LIQ=["BTCTMN","PEPETMN","PUMPTMN","ETHTMN","ARBTMN","DOGETMN","SOLTMN","STMN","BNBTMN"]
liqset={m.symbols.index(s) for s in LIQ}
warm=200*60; T=m.T
orig=sorted(ctx.cards["M1"], key=lambda c:c.idx)
def filt(require_repeat=True, dd_alt=-8.0, btc=-0.5, window=360):
    out=[]; seen={}
    for c in orig:
        s=c.sym; j=c.idx
        prev=[i for i in seen.get(s,[]) if j-i<=window]
        seen.setdefault(s,[]).append(j)
        if c.sym not in liqset: continue
        if R.at(j,s)["btc_ret15"]*100<=btc: continue
        dd6=(m.c[s,j]/m.h[s,max(0,j-window):j+1].max()-1)*100
        if require_repeat and not (len(prev)>=1 or dd6<=dd_alt): continue
        out.append(c)
    return out
def combo(sl=5.0,tp=6.0,ts=1440,size=250_000,maxpos=6):
    return dict(name="C7 test", scanners=["M1"], min_liq=0.0, coins=LIQ, size=size, size_band=(55_000,400_000),
        sl_band=(sl,sl), tp_band=(tp,tp), base_sl=sl, base_tps=[(tp,1.0)], max_pos=maxpos, max_exposure=0.5, cluster_caps={"meme":3},
        daily_stop=0.06, entries_per_hour=4, sl_cooldown=(1,240), entry="market", time_stop=ts, regime_ok=None, need_btc_bull=False, dd_cap=0.05)
def evaluate(label, cards, **kw):
    CB.COMBOS["C7"]=combo(**kw); EN.COMBOS["C7"]=CB.COMBOS["C7"]
    ctx.cards["M1"]=cards
    a=run_account(ctx,"C7",BaselinePolicy("C7",CB.COMBOS["C7"]),warm,T,start_cash=5_000_000)
    mx=MX.compute(a,warm,T,m.t)
    starts=[j for j in range(warm,T-1470) if int((m.t[j]+12600)%86400)==22*3600+30*60]
    rr=[]
    for s0 in starts:
        w=run_account(ctx,"C7",BaselinePolicy("C7",CB.COMBOS["C7"]),s0,s0+1470,start_cash=5_000_000)
        wm=MX.compute(w,s0,s0+1470,m.t); rr.append((wm["R1_net_return_pct"],wm["D2_dd_from_start_pct"],wm["trades"]))
    r=np.array(rr)
    # halves of full run by entry time
    tr=a.trades; cut=np.quantile([t.entry_idx for t in tr],0.6) if tr else 0
    hA=np.mean([t.pnl_pct for t in tr if t.entry_idx<cut]) if tr else 0; hB=np.mean([t.pnl_pct for t in tr if t.entry_idx>=cut]) if tr else 0
    print(f"{label:38s} 25d ret {mx['R1_net_return_pct']:+6.2f}% maxDD {mx['D1_maxdd_peak_pct']:.2f}% trades {mx['trades']:3d} ({mx['A1_trades_per_day']}/d) PF {mx['R3_profit_factor']} win {mx['win_rate_pct']}% avg/trade A {hA:+.2f}% B {hB:+.2f}% | 24.5h: med {np.median(r[:,0]):+.2f} mean {r[:,0].mean():+.2f} worst {r[:,0].min():+.2f} pos {np.mean(r[:,0]>0):.0%} worstDD {r[:,1].max():.2f} trades/d {np.median(r[:,2])}")
evaluate("first-card (current logic)", filt(require_repeat=False))
evaluate("repeat-or-dd8 (new)", filt(True))
evaluate("repeat-or-dd8, SL4.5 TP6", filt(True), sl=4.5)
evaluate("repeat-or-dd8, SL5 TP5", filt(True), tp=5.0)
evaluate("repeat-or-dd8, SL5 TP8", filt(True), tp=8.0)
evaluate("repeat-or-dd8, SL5 TP6 ts720", filt(True), ts=720)
evaluate("repeat-only (no dd alt)", filt(True, dd_alt=-99))
evaluate("repeat-or-dd8, win 4h", filt(True, window=240))
