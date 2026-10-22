"""Quick evidence sweep for the mean-reversion profile (C2) on liquid coins."""
import sys, os, warnings, itertools, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")
import numpy as np
from wallex_agent.backtest import Ctx, run_account
from wallex_agent.combos import COMBOS
from wallex_agent.policies import BaselinePolicy
from wallex_agent import metrics as MX
ctx = Ctx(35); T = ctx.m.T; warm = 200 * 60
LIQ = ["BTCTMN", "PEPETMN", "PUMPTMN", "ETHTMN", "ARBTMN", "DOGETMN", "SOLTMN", "STMN", "BNBTMN"]
orig = copy.deepcopy(COMBOS["C2"])
rows = []
for scan, tp, sl, ts, entry, reg in itertools.product((["M1"], ["M1", "M2"]), (1.5, 2.0, 3.0), (1.5, 2.5),
                                                      (240, 720), ("market", "limit"), (("range",), None)):
    COMBOS["C2"] = dict(orig, scanners=scan, coins=LIQ, base_tp=tp, base_sl=sl, time_stop=ts, entry=entry,
                        regime_ok=reg, tp_band=(0.9, 4), sl_band=(0.8, 3), max_pos=4, max_exposure=0.12)
    a = run_account(ctx, "C2", BaselinePolicy("C2", COMBOS["C2"]), warm, T)
    mx = MX.compute(a, warm, T, ctx.m.t)
    rows.append((mx["R1_net_return_pct"], mx["D1_maxdd_peak_pct"], mx["trades"], mx["win_rate_pct"], mx["R3_profit_factor"],
                 "+".join(scan), tp, sl, ts, entry, "range" if reg else "any"))
rows.sort(reverse=True)
print("ret%   dd%  trades win% pf    scan    tp  sl  time entry  regime")
for r in rows[:15] + [None] + rows[-5:]:
    if r is None: print("..."); continue
    print(f"{r[0]:+6.2f} {r[1]:5.2f} {r[2]:5d} {r[3]:5.1f} {r[4]:5.2f}  {r[5]:6s} {r[6]:4} {r[7]:4} {r[8]:4} {r[9]:6s} {r[10]}")
