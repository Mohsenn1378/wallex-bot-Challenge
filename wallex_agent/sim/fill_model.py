"""
Pessimistic fill model (same rules for backtest and the per-card evaluator).

Market : next bar open (or last price if no trade) +/- (spread*SPREAD_MULT/2 + SLIP + stale penalty)
Limit  : fills only if a REAL trade goes strictly through the limit price (queue assumed at the back)
Stop   : triggers only on a real trade at/below the stop; fill = min(stop, open) - costs (gaps honoured)
SL+TP in the same bar -> SL first.
"""
from __future__ import annotations
import math
import numpy as np

MAKER, TAKER = 0.0025, 0.0030
SLIP = 0.0005
STALE_PER_MIN, STALE_CAP = 0.0002, 0.005
SPREAD_MULT = 1.5
MIN_NOTIONAL = 50_000
STUCK_GUARD = 52_000


class Fills:
    def __init__(self, m, spread_mult: float = SPREAD_MULT):
        self.m = m
        S, T = m.c.shape
        # minutes since last real trade, per symbol
        stale = np.zeros((S, T), dtype=np.int32)
        for s in range(S):
            run = 0
            tr = m.tradable[s]
            col = stale[s]
            for j in range(T):
                run = 0 if tr[j] else run + 1
                col[j] = run
        self.stale = stale
        self.half_spread = np.array([m.rules[s].get("spread_pct", 0.3) / 100 * spread_mult / 2 for s in m.symbols])
        self.step_dec = np.array([m.rules[s].get("step_dec", 8) for s in m.symbols])

    def cost(self, s, j):
        return self.half_spread[s] + SLIP + min(STALE_PER_MIN * self.stale[s, j], STALE_CAP)

    def ref(self, s, j):
        m = self.m
        return m.o[s, j] if m.tradable[s, j] else m.c[s, j - 1 if j > 0 else 0]

    def market_buy_px(self, s, j):
        return self.ref(s, j) * (1 + self.cost(s, j))

    def market_sell_px(self, s, j):
        return self.ref(s, j) * (1 - self.cost(s, j))

    def limit_buy_hit(self, s, j, px):
        return bool(self.m.tradable[s, j] and self.m.l[s, j] < px)

    def limit_sell_hit(self, s, j, px):
        return bool(self.m.tradable[s, j] and self.m.h[s, j] > px)

    def stop_hit(self, s, j, stop):
        return bool(self.m.tradable[s, j] and self.m.l[s, j] <= stop)

    def stop_fill_px(self, s, j, stop):
        return min(stop, self.m.o[s, j]) * (1 - self.half_spread[s] - SLIP)

    def round_qty(self, s, qty):
        f = 10 ** int(self.step_dec[s])
        return math.floor(qty * f + 1e-9) / f
