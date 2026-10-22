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
from .data.store import tehran_day, TEHRAN_OFFSET_S
from .sim.fill_model import Fills, MIN_NOTIONAL
from .sim.position import Plan, Position, open_position


TIER_ORDER = ("probe", "standard", "high")
FEE_ROUNDTRIP = 0.006          # C7 effective-risk distance: sl + stop-limit offset + both fees


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
        # C7 / live (all inert for C1..C6: None/empty/False and only read when the C7 config keys exist)
        self.window = None                       # {"probe_only_after_ts", "no_new_entries_after_ts", "flatten_start_ts"}
        self.control = None                      # runtime.Control (pause_entries, size_mult, max_positions, ...)
        self.hooks = None                        # live executor: on_open / on_close / mark_unknown
        self.equity_fn = None                    # live equity (Wallex balances); fallback = paper equity
        self.flatten_mode = False
        self.gov_state = "normal"                # drawdown governor: normal | soft | hard
        self.m1_hist = defaultdict(set)          # sym -> bar indices of M1 cards (set: same bar never counted twice)
        self.disabled_coins = set()              # symbol names disabled after repeated live rejections
        self._guard_info = {}
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

    def equity_now(self, j):
        """Live equity when an executor supplies it (C7), else paper equity. Never raises."""
        if self.equity_fn is not None:
            try:
                eq = float(self.equity_fn())
                if eq > 0 and math.isfinite(eq):
                    return eq
            except Exception as e:
                self.errors.append({"idx": j, "type": "equity_fn", "err": repr(e)})
        return self.equity_at(j)

    def _win(self, key):
        """Window timestamp (unix s) from self.window, else from the control file; None if unknown."""
        if self.window and self.window.get(key) is not None:
            return float(self.window[key])
        v = getattr(self.control, key, None) if self.control is not None else None
        return float(v) if v is not None else None

    def _ctl(self, key, default):
        return getattr(self.control, key, default) if self.control is not None else default

    def _tehran_hour(self, j):
        return int((int(self.m.t[j]) + TEHRAN_OFFSET_S) % 86400 // 3600)

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
        if sym in self.disabled_coins:
            return "coin_disabled"
        if "btc15_block" in acct and self.R.at(j, card.sym)["btc_ret15"] <= acct["btc15_block"]:
            return "btc_falling"
        if self._ctl("pause_entries", False):
            return "paused_by_control"
        if self.flatten_mode:
            return "flatten_mode"
        t_close = self._win("no_new_entries_after_ts")
        if t_close is not None and self.m.t[j] >= t_close:
            return "window_closed"
        if self.gov_state == "hard":
            return "dd_governor"
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
        if len(self.positions) + len(self.pending) >= self._ctl("max_positions", acct["max_pos"]):
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
        self._guard_info = {}
        # size
        if "size_tiers" in c:
            r = self._size_c7(c, dec, sl, j)
            if isinstance(r, str):
                return None, r
            notional = r
        elif "risk_tmn" in c:
            risk = c["risk_tmn"] * float(dec.get("risk_mult", 1.0))
            eq = self.equity_at(j)
            if eq < self.peak * 0.98:
                risk *= 0.5                                    # C3 drawdown rule
            notional = risk / (sl / 100)
        else:
            notional = float(dec.get("size_tmn") or c.get("size", 55_000))
        if "size_tiers" not in c:
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
        if "heat_cap_pct" in c:
            eff = sl / 100 + c["stop_limit_offset"] + FEE_ROUNDTRIP
            heat = sum(p.qty * p.entry_px * (p.plan.sl_pct / 100 + c["stop_limit_offset"] + FEE_ROUNDTRIP)
                       for p in self.positions.values())
            if heat + notional * eff > c["heat_cap_pct"] * self.equity_now(j):
                return None, "heat_cap"
        if "size_tiers" in c:
            return self._plan_c7(c, dec, sl, notional, j)
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

    def _size_c7(self, c, dec, sl, j):
        """Size tier chosen by the LLM -> TMN, then night / late-window caps, control multiplier, governor,
        and the per-trade risk cap on the effective distance (sl + stop-limit offset + fees). Returns size or reason."""
        tiers = c["size_tiers"]
        req = dec.get("size_tier") if dec.get("size_tier") in tiers else "standard"
        tier = req
        lo_h, hi_h = c.get("night_hours", (0, 0))
        if lo_h <= self._tehran_hour(j) < hi_h:
            cap = c.get("night_tier_cap", "standard")
            tier = TIER_ORDER[min(TIER_ORDER.index(tier), TIER_ORDER.index(cap))]
        t_probe = self._win("probe_only_after_ts")
        if t_probe is not None and self.m.t[j] >= t_probe:
            tier = "probe"
        floor, hi = c["size_band"]
        mult = float(self._ctl("size_mult", 1.0))
        if mult <= 0:
            return "size_mult_zero"
        size = max(tiers[tier] * mult, floor)
        if self.gov_state == "soft":
            size = max(size * 0.5, floor)
        elif self.gov_state == "hard":
            return "dd_governor"
        eff = sl / 100 + c["stop_limit_offset"] + FEE_ROUNDTRIP
        size = min(size, c["risk_cap_pct"] * self.equity_now(j) / eff, hi)
        if size < floor:
            return "size_below_min"
        self._guard_info = {"tier_requested": req, "tier_final": tier, "size": round(size), "risk_tmn": round(size * eff)}
        return size

    def _plan_c7(self, c, dec, sl, notional, j):
        """One take-profit inside the band, no break-even, no trailing; time stop ends before the flatten window."""
        tb = c["tp_band"]
        tp_plan = dec.get("tp_plan") or []
        pct = float(tp_plan[0]["pct"]) if tp_plan else c["base_tps"][0][0]
        tps = [(min(max(pct, tb[0]), tb[1]), 1.0)]
        ts = min(float(dec.get("time_stop_min") or 1440), float(c["time_stop"]))
        t_flat = self._win("flatten_start_ts")
        if t_flat is not None:
            ts = min(ts, (t_flat - self.m.t[j]) / 60 - 5)
        if ts < 60:
            return None, "too_close_to_end"
        plan = Plan(sl_pct=sl, tps=tps, time_stop_min=int(ts), time_stop_min_profit=0, be_trigger_pct=0,
                    trail_atr_mult=0, entry=c.get("entry", "market"), limit_valid_min=c.get("limit_valid", 10))
        return (plan, notional), None

    def _arm(self, card, j, c):
        """C7: the first M1 signal on a coin in arm_window_min only arms it, unless the coin is already
        arm_dd_pct below its high of that window. Returns a rejection reason or None."""
        s, w = card.sym, int(c["arm_window_min"])
        hist = self.m1_hist[s]
        hist.add(card.idx)
        for i in [i for i in hist if i < card.idx - w]:
            hist.discard(i)
        prev = sum(1 for i in hist if card.idx - w <= i < card.idx)
        hi = float(np.max(self.m.h[s, max(0, j - w):j + 1]))
        dd6 = float(self.m.c[s, j]) / hi - 1 if hi > 0 else 0.0
        card.info["m1_prev_6h"], card.info["dd6_pct"] = prev, round(dd6 * 100, 2)
        if prev == 0 and dd6 * 100 > c["arm_dd_pct"]:
            return "armed_first_signal"
        return None

    # ------------------------------------------------------------ main loop pieces
    def on_card(self, card, j: int, profile: str | None = None):
        profile = profile or self.cid
        c = COMBOS.get(profile, self.cfg)
        if "arm_window_min" in c and card.scanner == "M1":
            why = self._arm(card, j, c)
            if why:
                return self._reject(card, None, why, j)
        ctx = self.context(card, j, profile)
        # code-level combo filters (identical for baseline and LLM)
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
                               "decision": dec, "result": "accepted", "notional": notional, "profile": profile,
                               **self._guard_info})

    def context(self, card, j, profile):
        ctx = {"regime": self.R.at(j, card.sym), "profile": profile, "equity": self.equity_at(j),
               "open_positions": [self.m.symbols[s] for s in self.positions], "idx": j}
        if "arm_window_min" in COMBOS.get(profile, self.cfg):
            t_flat = self._win("flatten_start_ts")
            ctx.update(equity=self.equity_now(j), m1_prev_6h=card.info.get("m1_prev_6h"),
                       dd6_pct=card.info.get("dd6_pct"), tehran_hour=self._tehran_hour(j),
                       hours_to_flatten=round((t_flat - self.m.t[j]) / 3600, 2) if t_flat is not None else None,
                       trades_24h=self.trades_24h(j), gov_state=self.gov_state)
        return ctx

    def trades_24h(self, j):
        return (sum(1 for t in self.trades if t.entry_idx >= j - 1440)
                + sum(1 for p in self.positions.values() if p.entry_idx >= j - 1440))

    def _fill_pending(self, j):
        keep, opened = [], []
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
            opened.append(pos)
        self.pending = keep
        for pos in opened:                       # only now: a failing hook can never re-send the same entry
            self._hook("on_open", pos, j)

    def _hook(self, name, pos, j):
        if self.hooks is None:
            return
        try:
            getattr(self.hooks, name)(self, pos, j)
        except Exception as e:
            sym = self.m.symbols[pos.sym]
            self.errors.append({"idx": j, "type": "hook_error", "hook": name, "sym": sym, "err": repr(e)})
            try:
                self.hooks.mark_unknown(sym)
            except Exception as e2:
                self.errors.append({"idx": j, "type": "hook_error", "hook": "mark_unknown", "sym": sym, "err": repr(e2)})

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
        self._hook("on_close", pos, j)

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
        if "dd_soft" in self.cfg:
            self._governor(j)
        if self.flatten_mode and self.positions:
            self.close_all(j)

    def _governor(self, j):
        """Soft drawdown governor from the start value: >= dd_soft -> half size, >= dd_hard -> manage only,
        back below dd_resume -> soft, below 1% -> normal. No forced liquidation."""
        c = self.cfg
        dd = 1 - self.equity_now(j) / self.start_cash
        st = self.gov_state
        if dd >= c["dd_hard"]:
            new = "hard"
        elif st == "hard":
            new = "soft" if dd < c["dd_resume"] else "hard"
        elif dd >= c["dd_soft"]:
            new = "soft"
        elif st == "soft" and dd >= 0.01:
            new = "soft"
        else:
            new = "normal"
        if new != st:
            self.gov_state = new
            self.decisions.append({"idx": j, "result": "governor", "from": st, "to": new, "dd_pct": round(dd * 100, 3)})

    # ------------------------------------------------------------ live mirror helpers (called by the runner)
    def cancel_position(self, s, reason, j):
        """Live entry failed: drop the paper position and give its cash back (no trade record)."""
        pos = self.positions.pop(s, None)
        if pos is None:
            return
        self.cash += pos.entry_px * pos.qty + pos.fees
        try:
            self.entries_hist.remove(pos.entry_idx)
        except ValueError:
            pass
        self.decisions.append({"idx": j, "sym": self.m.symbols[s], "result": "live_entry_failed", "why": reason})

    def sync_position(self, s, entry_px, qty, fees):
        """Replace the simulated fill with the real one (price, quantity, fees)."""
        pos = self.positions.get(s)
        if pos is None:
            return
        self.cash += pos.entry_px * pos.qty + pos.fees - (entry_px * qty + fees)
        pos.entry_px, pos.qty, pos.qty_initial, pos.fees = float(entry_px), float(qty), float(qty), float(fees)
        pos.stop = pos.entry_px * (1 - pos.plan.sl_pct / 100)

    def adopt_position(self, s, qty, px, j, plan, meta=None):
        """Coin held on Wallex (bought by the bot) but unknown to the paper engine -> manage it (no live order)."""
        from .scanners import Card
        if s in self.positions:
            return self.positions[s]
        pos = Position(s, float(qty), float(px), j, plan, float(px) * (1 - plan.sl_pct / 100))
        pos.qty_initial = pos.qty
        card = Card(j, s, "ADOPT", 1, float(px), pos.stop, float("nan"), {})
        pos.meta = {"card": card, "dec": {"thesis": "adopted from Wallex balance", "size_tier": None},
                    "profile": self.cid, "regime": self.R.at(j, s), **(meta or {})}
        pos._n_exits = 0
        self.cash -= pos.entry_px * pos.qty
        self.positions[s] = pos
        self.decisions.append({"idx": j, "sym": self.m.symbols[s], "result": "adopted", "qty": pos.qty, "px": pos.entry_px})
        return pos

    def close_external(self, s, j, px, reason):
        """A close that already happened outside the engine (server stop / client stop / reconcile): book it.
        on_close still runs; the executor sees the closed status and only patches numbers."""
        pos = self.positions.get(s)
        if pos is None or not pos.open:
            return
        pos._sell(self.f, j, float(px), pos.qty, reason, False)
        self._book_exits(pos, j)

    def close_all(self, j):
        for s, pos in list(self.positions.items()):
            pos.exit_market(self.f, j, "END")
            self._book_exits(pos, j)
