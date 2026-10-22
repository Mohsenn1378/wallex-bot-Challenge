"""Gross forward returns after each scanner's cards (no costs) -> is there any edge before fees?"""
import sys, os, warnings, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")
import numpy as np
from wallex_agent.backtest import Ctx

ctx = Ctx(35)
m = ctx.m
H = [15, 60, 240, 720]
print(f"{'scan':5s} {'n':>5s} " + " ".join(f"{'fwd'+str(h):>8s}" for h in H) + "   mfe240  mae240  hit(+2%<-1.5%)")
res = {}
for k, cards in ctx.cards.items():
    rows = []
    for c in cards:
        j = c.idx + 1
        if j + max(H) >= m.T or c.idx < 200 * 60:
            continue
        p0 = m.o[c.sym, j]
        f = [m.c[c.sym, j + h] / p0 - 1 for h in H]
        hi = m.h[c.sym, j:j + 240].max() / p0 - 1
        lo = m.l[c.sym, j:j + 240].min() / p0 - 1
        # first-touch +2% vs -1.5%
        up = np.argmax(m.h[c.sym, j:j + 720] >= p0 * 1.02) if (m.h[c.sym, j:j + 720] >= p0 * 1.02).any() else 10**9
        dn = np.argmax(m.l[c.sym, j:j + 720] <= p0 * 0.985) if (m.l[c.sym, j:j + 720] <= p0 * 0.985).any() else 10**9
        rows.append(f + [hi, lo, 1.0 if up < dn else 0.0])
    if not rows:
        continue
    a = np.array(rows)
    mu = a.mean(0) * 100
    res[k] = mu.tolist()
    print(f"{k:5s} {len(a):5d} " + " ".join(f"{x:+8.3f}" for x in mu[:4]) + f"   {mu[4]:+6.2f}  {mu[5]:+6.2f}   {a[:,6].mean():.0%}")
# unconditional benchmark: random minutes on same coins
rng = np.random.default_rng(0)
rows = []
for _ in range(3000):
    s = rng.integers(0, len(m.symbols) - 2); j = rng.integers(200 * 60, m.T - 721)
    p0 = m.o[s, j]
    rows.append([m.c[s, j + h] / p0 - 1 for h in H])
print("RANDOM", " ".join(f"{x:+8.3f}" for x in np.mean(rows, 0) * 100))
