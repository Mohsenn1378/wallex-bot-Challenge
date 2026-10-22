"""
One event loop for every mode. Backtest = historical Market + simulated fills.
(The live/shadow feed reuses Account.on_bar/on_card with a live Market buffer.)
"""
from __future__ import annotations
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
import numpy as np

from .combos import COMBOS, PROFILE_SCANNERS, cluster
from .data.store import tehran_day
from .sim.fill_model import Fills, MIN_NOTIONAL
from .sim.position import Plan, Position, open_position


@dataclass
class Trade:
    combo: str
    policy: str
    sym: str
    scanner: str
    profile: str
    entry_idx: int
    entry_t: int
    entry_px: float
    qty: float
    notional: float
    sl_pct: float
    tp_plan: list
    exit_t: int
    exit_px: float
    exit_reason: str
    fees: float
    pnl: float
    pnl_pct: float
    r_mult: float
    decision: dict
    card: dict
    regime: dict


class Account:
    def __init__(self, cid: str, policy, m, fills: Fills, F, regime, start_cash=2_500_000, log=None):
        self.cid, self.cfg, self.policy = cid, COMBOS[cid], policy
        self.m, self.f, self.F, self.R = m, fills, F, regime
        self.cash = float(start_cash)
        self.start_cash = float(start_cash)
        self.positions: dict[int, Position] = {}
        self.pending: list = []                  # (exec_idx, card, decision, profile, expire_idx)
        self.trades: list[Trade] = []
        self.decisions: list = []                # every card -> decision/guardrail outcome
        self.errors: list = []
        self.equity = []                         # (idx, equity)
        self.peak = float(start_cash)
        self.day, self.day_start_eq, self.day_halt = None, float(start_cash), False
        self.entries_hist = deque()
        self.sl_streak = defaultdict(int)
        self.cooldown_until = defaultdict(int)
        self.loss_streak = 0
        self.pause_until = 0
        self.cb_until = 0
        self.log = log
        coins = self.cfg.get("coins")
        self.allowed = set(m.symbols) - {"BTCUSDT", "USDTTMN"} if coins in (None, "core") else set(coins)
        self.atr15 = F.x[15]["atr"]
        self.liq15 = F.x[15]["liq"]
        self.end15 = F.tf[15].end_idx

    # ------------------------------------------------------------ helpers
    def _b15(self, j):
        return int(np.searchsorted(self.end15, j, side="right") - 1)

    def equity_at(self, j):
        return self.cash + sum(p.qty * self.m.c[s, j] for s, p in self.positions.items())

    def exposure(self, j):
        return sum(p.qty * self.m.c[s, j] for s, p in self.positions.items())

    def _reject(self, card, dec, why, j):
        self.decisions.append({"idx": j, "sym": self.m.symbols[card.sym], "scanner": card.scanner,
                               "decision": dec, "result": "rejected", "why": why})

    # ------------------------------------------------------------ guardrails
    def precheck(self, card, j: int):
        """Cheap code-only checks that do not depend on the decision. Run BEFORE the policy so that
        no LLM call (tokens) is spent on a card the guardrails would reject anyway. Returns reason or None."""
        acct = self.cfg
        sym = self.m.symbols[card.sym]
        if sym not in self.allowed:
            return "coin_not_allowed"
        b = self._b15(j)
        liq = float(np.nan_to_num(self.liq15[card.sym, b])) if b >= 0 else 0
        if liq < acct.get("min_liq", 0):
            return f"illiquid({liq:.2f})"
        if card.sym in self.positions:
            return "already_in_position"
        if self.day_halt:
            return "daily_stop"
        if j < self.pause_until or j < self.cb_until:
            return "paused"
        if j < self.cooldown_until[card.sym]:
            return "coin_cooldown"
        while self.entries_hist and j - self.entries_hist[0] > 60:
            self.entries_hist.popleft()
        if len(self.entries_hist) >= acct.get("entries_per_hour", 99):
            return "rate_limit_entries"
        if len(self.positions) + len(self.pending) >= acct["max_pos"]:
            return "max_positions"
        cl = cluster(sym)
        caps = acct.get("cluster_caps", {})
        if cl in caps and sum(1 for s in self.positions if cluster(self.m.symbols[s]) == cl) >= caps[cl]:
            return f"cluster_cap_{cl}"
        eq = self.equity_at(j)
        if self.cash - 55_000 * 1.01 < 0.5 * eq:
            return "cash_reserve_50pct"
        return None

    def guard(self, card, dec: dict, j: int, profile: str):
        """Clamp/validate a decision. Returns (plan, notional) or (None, reason)."""
        c = COMBOS[profile] if profile in COMBOS else self.cfg
        acct = self.cfg
        sym = self.m.symbols[card.sym]
        if dec is None or dec.get("action") != "enter":
            return None, "skip"
        why = self.precheck(card, j)
        if why:
            return None, why
        # stop distance
        band = c.get("sl_band", (0.8, 2.5))
        sl = float(dec.get("sl_pct") or sum(band) / 2)
        sl = min(max(sl, band[0]), band[1])
        # size
        if "risk_tmn" in c:
            risk = c["risk_tmn"] * float(dec.get("risk_mult", 1.0))
            eq = self.equity_at(j)
            if eq < self.peak * 0.98:
                risk *= 0.5                                    # C3 drawdown rule
            notional = risk / (sl / 100)
        else:
            notional = float(dec.get("size_tmn") or c.get("size", 55_000))
        lo, hi = c.get("size_band", (55_000, 200_000))
        notional = min(max(notional, lo), hi)
        eq = self.equity_at(j)
        if self.exposure(j) + notional > acct["max_exposure"] * eq:
            return None, "max_exposure"
        if self.cash - notional * 1.01 < 0.5 * eq:
            return None, "cash_reserve_50pct"
        if "heat_cap" in c:
            heat = sum(p.qty * p.entry_px * p.plan.sl_pct / 100 for p in self.positions.values())
            if heat + notional * sl / 100 > c["heat_cap"]:
                return None, "heat_cap"
        # take-profits within band
        tps = []
        tb = c.get("tp_band")
        for tp in (dec.get("tp_plan") or [])[:3]:
            pct = float(tp["pct"])
            if tb:
                pct = min(max(pct, tb[0]), tb[1])
            tps.append((pct, float(tp["portion"])))
        tps.sort()
        if not tps and "trail_atr" not in c:
            tps = [(max(sl * 1.5, 1.0), 1.0)]
        ts = int(dec.get("time_stop_min") or c.get("time_stop", 0))
        plan = Plan(sl_pct=sl, tps=tps, time_stop_min=ts, time_stop_min_profit=c.get("time_stop_min_profit", 0),
                    be_trigger_pct=c.get("be_pct", 0) or (sl * c["be_at_r"] if "be_at_r" in c else 0),
                    trail_atr_mult=c.get("trail_atr", 0), entry=c.get("entry", "market"),
                    limit_valid_min=c.get("limit_valid", 10))
        return (plan, notional), None

    # ------------------------------------------------------------ main loop pieces
    def on_card(self, card, j: int, profile: str | None = None):
        profile = profile or self.cid
        ctx = self.context(card, j, profile)
        # code-level combo filters (identical for baseline and LLM)
        c = COMBOS.get(profile, self.cfg)
        reg = ctx["regime"]
        if c.get("regime_ok") and reg["coin"] not in c["regime_ok"]:
            return self._reject(card, None, f"regime_{reg['coin']}", j)
        if c.get("need_btc_bull") and not reg["btc_bull"]:
            return self._reject(card, None, "btc_not_bull", j)
        why = self.precheck(card, j)
        if why:                                   # rejected by code before any LLM call
            return self._reject(card, None, why, j)
        try:
            dec = self.policy.decide(card, ctx)
        except Exception as e:  # policy failure never kills the account
            self.errors.append({"idx": j, "type": "policy", "err": repr(e)})
            dec = None
        res, why = self.guard(card, dec, j, profile)
        if res is None:
            return self._reject(card, dec, why, j)
        plan, notional = res
        lat = int(dec.get("_latency_min", 0)) if dec else 0
        exec_idx = j + 1 + lat
        expire = exec_idx + (plan.limit_valid_min if plan.entry == "limit" else 0)
        self.pending.append((exec_idx, card, dec, profile, expire, plan, notional))
        self.decisions.append({"idx": j, "sym": self.m.symbols[card.sym], "scanner": card.scanner,
                               "decision": dec, "result": "accepted", "notional": notional, "profile": profile})

    def context(self, card, j, profile):
        return {"regime": self.R.at(j, card.sym), "profile": profile, "equity": self.equity_at(j),
                "open_positions": [self.m.symbols[s] for s in self.positions], "idx": j}

    def _fill_pending(self, j):
        keep = []
        for item in self.pending:
            exec_idx, card, dec, profile, expire, plan, notional = item
            if j < exec_idx:
                keep.append(item); continue
            if card.sym in self.positions:
                continue
            s = card.sym
            pos = None
            if plan.entry == "limit":
                px = card.price
                if self.f.limit_buy_hit(s, j, px):
                    pos = open_position(self.f, s, j, notional, plan, px=px, maker=True)
                elif j < expire:
                    keep.append(item); continue
                else:
                    self.decisions.append({"idx": j, "sym": self.m.symbols[s], "scanner": card.scanner,
                                           "result": "limit_expired"})
                    continue
            else:
                pos = open_position(self.f, s, j, notional, plan)
            if pos is None:
                self.errors.append({"idx": j, "type": "order_rejected", "sym": self.m.symbols[s], "why": "min_notional/step"})
                continue
            pos.meta = {"card": card, "dec": dec, "profile": profile, "regime": self.R.at(card.idx, s)}
            self.cash -= pos.entry_px * pos.qty + pos.fees
            pos._n_exits = 0
            self.positions[s] = pos
            self.entries_hist.append(j)
        self.pending = keep

    def _book_exits(self, pos, j):
        for (ji, px, q, reason, fee) in pos.exits[pos._n_exits:]:
            self.cash += px * q - fee
        pos._n_exits = len(pos.exits)
        if not pos.open:
            self._close(pos, j)

    def _close(self, pos, j):
        s = pos.sym
        del self.positions[s]
        pnl = pos.pnl()
        cost = pos.entry_px * pos.qty_initial
        last = pos.exits[-1]
        sl_amt = cost * pos.plan.sl_pct / 100
        card, dec = pos.meta["card"], pos.meta["dec"] or {}
        self.trades.append(Trade(self.cid, self.policy.name, self.m.symbols[s], card.scanner, pos.meta["profile"],
                                 pos.entry_idx, int(self.m.t[pos.entry_idx]), pos.entry_px, pos.qty_initial, cost,
                                 pos.plan.sl_pct, pos.plan.tps, int(self.m.t[min(last[0], self.m.T - 1)]), last[1], last[3],
                                 pos.fees, pnl, pnl / cost * 100, pnl / sl_amt if sl_amt else 0,
                                 {k: v for k, v in dec.items() if not k.startswith("_")},
                                 {"scanner": card.scanner, "tf": card.tf, "price": card.price, "stop_hint": card.stop_hint,
                                  "idx": card.idx, **card.info}, pos.meta["regime"]))
        if pnl < 0:
            self.sl_streak[s] += 1
            self.loss_streak += 1
            n, mins = self.cfg.get("sl_cooldown", (99, 0))
            if self.sl_streak[s] >= n:
                self.cooldown_until[s] = j + mins
                self.sl_streak[s] = 0
            if "loss_pause" in self.cfg and self.loss_streak >= self.cfg["loss_pause"][0]:
                self.pause_until = j + self.cfg["loss_pause"][1]
                self.loss_streak = 0
        else:
            self.sl_streak[s] = 0
            self.loss_streak = 0

    def on_bar(self, j: int):
        m = self.m
        # new Tehran day -> reset daily stop
        d = int(tehran_day(m.t[j]))
        if d != self.day:
            self.day, self.day_start_eq, self.day_halt = d, self.equity_at(j), False
        self._fill_pending(j)
        b = self._b15(j)
        for s, pos in list(self.positions.items()):
            atr = float(self.atr15[s, b]) if b >= 0 and np.isfinite(self.atr15[s, b]) else None
            pos.step(self.f, j, atr)
            self._book_exits(pos, j)
        eq = self.equity_at(j)
        self.peak = max(self.peak, eq)
        self.equity.append(eq)
        if eq < self.day_start_eq * (1 - self.cfg["daily_stop"]):
            self.day_halt = True
        if self.cfg.get("circuit_btc15") and b >= 0 and j == self.end15[b]:
            if self.R.btc_ret15[b] < self.cfg["circuit_btc15"]:
                for s, pos in list(self.positions.items()):
                    pos.exit_market(self.f, min(j + 1, m.T - 1), "CIRCUIT")
                    self._book_exits(pos, j)
                self.cb_until = j + 60
        if self.cfg.get("cid_dd_pause"):
            pass

    def close_all(self, j):
        for s, pos in list(self.positions.items()):
            pos.exit_market(self.f, j, "END")
            self._book_exits(pos, j)
