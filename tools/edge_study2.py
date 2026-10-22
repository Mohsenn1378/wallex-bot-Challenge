import sys, os, warnings
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")
import numpy as np
from wallex_agent.backtest import Ctx
ctx = Ctx(35); m = ctx.m
LIQ = {"BTCTMN", "PEPETMN", "PUMPTMN", "ETHTMN", "USOONTMN", "ARBTMN", "DOGETMN", "SOLTMN", "STMN", "BNBTMN", "ADATMN"}
def study(name, cards, real_entry=True):
    out = {"liquid": [], "thin": []}
    for c in cards:
        if c.idx < 200 * 60: continue
        j = c.idx + 1
        if real_entry:  # first minute with a real trade after the signal (what a market order would see)
            k = j
            while k < m.T - 800 and not m.tradable[c.sym, k]: k += 1
            if k - j > 30: continue
            j = k
        if j + 721 >= m.T: continue
        p0 = m.o[c.sym, j]
        f = [m.c[c.sym, j + h] / p0 - 1 for h in (60, 240, 720)]
        seg_h, seg_l = m.h[c.sym, j:j + 720], m.l[c.sym, j:j + 720]
        res = []
        for tp, sl in ((1.5, 1.5), (2, 1.5), (3, 2), (2, 2.5)):
            up = np.argmax(seg_h >= p0 * (1 + tp / 100)) if (seg_h >= p0 * (1 + tp / 100)).any() else 10**9
            dn = np.argmax(seg_l <= p0 * (1 - sl / 100)) if (seg_l <= p0 * (1 - sl / 100)).any() else 10**9
            res.append(1.0 if up < dn else (0.0 if dn < up else 0.5))
        out["liquid" if m.symbols[c.sym] in LIQ else "thin"].append(f + res)
    for k, rows in out.items():
        if not rows: continue
        a = np.array(rows); mu = a.mean(0)
        print(f"{name:3s} {k:6s} n={len(a):4d} fwd60={mu[0]*100:+.2f}% fwd240={mu[1]*100:+.2f}% fwd720={mu[2]*100:+.2f}%  "
              f"hit 1.5/1.5={mu[3]:.0%} 2/1.5={mu[4]:.0%} 3/2={mu[5]:.0%} 2/2.5={mu[6]:.0%}")
for k in ("M1", "M4", "M2", "M7", "MOM"):
    study(k, ctx.cards[k])
