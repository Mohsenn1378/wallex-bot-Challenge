"""Simulate ONE trade from a card with a given plan (used by the per-card LLM evaluator)."""
from __future__ import annotations
import numpy as np
from .position import Plan, open_position


def simulate(m, f, F, card, plan: Plan, notional: float, horizon_min: int = 2880):
    s = card.sym
    j = card.idx + 1
    end = min(m.T - 1, j + horizon_min)
    pos = None
    if plan.entry == "limit":
        for k in range(j, min(end, j + plan.limit_valid_min)):
            if f.limit_buy_hit(s, k, card.price):
                pos = open_position(f, s, k, notional, plan, px=card.price, maker=True)
                break
        if pos is None:
            return {"filled": False, "pnl_pct": 0.0, "pnl_tmn": 0.0, "reason": "NOFILL", "hold_min": 0}
    else:
        pos = open_position(f, s, j, notional, plan)
        if pos is None:
            return {"filled": False, "pnl_pct": 0.0, "pnl_tmn": 0.0, "reason": "REJECT", "hold_min": 0}
    atr = F.x[15]["atr"]; e15 = F.tf[15].end_idx
    k = pos.entry_idx
    while pos.open and k < end:
        b = int(np.searchsorted(e15, k, side="right") - 1)
        pos.step(f, k, float(atr[s, b]) if b >= 0 and np.isfinite(atr[s, b]) else None)
        k += 1
    if pos.open:
        pos.exit_market(f, end, "HORIZON")
    pnl = pos.pnl()
    cost = pos.entry_px * pos.qty_initial
    return {"filled": True, "pnl_pct": pnl / cost * 100, "pnl_tmn": pnl, "reason": pos.exits[-1][3],
            "hold_min": pos.exits[-1][0] - pos.entry_idx}
