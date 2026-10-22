"""Position with stop / scaled take-profits / break-even / trailing / time-stop. Shared by engine and card evaluator."""
from __future__ import annotations
from dataclasses import dataclass, field
from .fill_model import Fills, MAKER, TAKER, MIN_NOTIONAL, STUCK_GUARD


@dataclass
class Plan:
    sl_pct: float                    # stop distance below entry, %
    tps: list                        # [(pct, portion)], portions sum <= 1 ; remaining -> trail/time
    time_stop_min: int = 0           # 0 = none
    time_stop_min_profit: float = 0  # exit at time stop only if pnl% < this (0 = always)
    be_trigger_pct: float = 0        # move stop to entry+fees once price >= entry*(1+x%)
    trail_atr_mult: float = 0        # after TP1, trail at mult*ATR(15m)
    entry: str = "market"            # market | limit
    limit_valid_min: int = 10


@dataclass
class Position:
    sym: int
    qty: float
    entry_px: float
    entry_idx: int
    plan: Plan
    stop: float
    fees: float = 0.0
    realized: float = 0.0            # TMN cash returned from partial exits (gross of fees)
    tp_done: int = 0
    exits: list = field(default_factory=list)   # (idx, px, qty, reason, fee)
    qty_initial: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def open(self):
        return self.qty > 0

    def _sell(self, f: Fills, j, px, qty, reason, maker):
        fee = px * qty * (MAKER if maker else TAKER)
        self.realized += px * qty
        self.fees += fee
        self.qty -= qty
        if self.qty < 1e-12:
            self.qty = 0.0
        self.exits.append((j, px, qty, reason, fee))

    def step(self, f: Fills, j: int, atr15: float | None = None) -> None:
        """Process bar j (1m). Pessimistic ordering: stop first, then take-profits."""
        if not self.open:
            return
        s, m = self.sym, f.m
        # 1) stop (incl. gap through stop)
        if f.stop_hit(s, j, self.stop):
            self._sell(f, j, f.stop_fill_px(s, j, self.stop), self.qty, "SL" if self.tp_done == 0 and self.stop < self.entry_px else "STOP_TRAIL", False)
            return
        # 2) take-profit limits
        for i in range(self.tp_done, len(self.plan.tps)):
            pct, portion = self.plan.tps[i]
            px = self.entry_px * (1 + pct / 100)
            if not f.limit_sell_hit(s, j, px):
                break
            q = f.round_qty(s, self.qty if i == len(self.plan.tps) - 1 and sum(p for _, p in self.plan.tps) >= 0.999
                            else self.qty_initial * portion)
            if (self.qty - q) * px < MIN_NOTIONAL or q * px < MIN_NOTIONAL:
                q = self.qty                          # cannot split -> exit all
            self._sell(f, j, px, q, f"TP{i + 1}", True)
            self.tp_done = i + 1
            if self.tp_done == 1:                     # after TP1 protect at break-even
                self.stop = max(self.stop, self.entry_px * 1.006)
            if not self.open:
                return
        c = m.c[s, j]
        # 3) break-even
        if self.plan.be_trigger_pct and c >= self.entry_px * (1 + self.plan.be_trigger_pct / 100):
            self.stop = max(self.stop, self.entry_px * 1.0055)
        # 4) trailing after TP1
        if self.plan.trail_atr_mult and self.tp_done >= 1 and atr15:
            self.stop = max(self.stop, c - self.plan.trail_atr_mult * atr15)
        # 5) time stop
        if self.plan.time_stop_min and j - self.entry_idx >= self.plan.time_stop_min:
            pnl = c / self.entry_px - 1
            if not self.plan.time_stop_min_profit or pnl * 100 < self.plan.time_stop_min_profit:
                self.exit_market(f, j + 1 if j + 1 < m.T else j, "TIME")
                return
        # 6) stuck guard (value too close to min notional)
        if self.qty * c <= STUCK_GUARD:
            self.exit_market(f, j + 1 if j + 1 < m.T else j, "STUCK_GUARD")

    def exit_market(self, f: Fills, j: int, reason: str):
        if self.open:
            self._sell(f, j, f.market_sell_px(self.sym, j), self.qty, reason, False)

    def pnl(self):
        """Net TMN P&L of a CLOSED position."""
        cost = self.entry_px * self.qty_initial
        return self.realized - cost - self.fees


def open_position(f: Fills, s: int, j: int, notional: float, plan: Plan, px: float | None = None, maker=False, meta=None):
    """Create a position filled at bar j. Returns None if rules (step/min notional) make it impossible."""
    px = px if px is not None else f.market_buy_px(s, j)
    qty = f.round_qty(s, notional / px)
    if qty * px < MIN_NOTIONAL * 1.02:
        return None
    pos = Position(s, qty, px, j, plan, px * (1 - plan.sl_pct / 100), meta=meta or {})
    pos.qty_initial = qty
    pos.fees = px * qty * (MAKER if maker else TAKER)
    return pos
