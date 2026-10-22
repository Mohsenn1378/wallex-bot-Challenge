"""
Test C: live shadow test. Same engine as the backtest, fed by live Wallex candles; orders are simulated
(paper), nothing is sent to the exchange.
    python -m wallex_agent.shadow --llm sol_opencode --until 19:00 --accounts C1,C2,C3,C4,C5,C6
Every account runs twice: rule baseline and LLM. State is saved every cycle; restart resumes.
C7 (live run, docs/IMPROVEMENT_PROPOSAL.md P0-06) runs once (LLM if --llm, else rule baseline) and is steered by
config/runtime.yaml; with --live a LiveExecutor mirrors every open/close on Wallex.
    python -m wallex_agent.shadow --accounts C7 --llm box_sol61,box_sol6,box_sol56 --end "2026-10-23 22:50" --native-at-start
Ctrl+C / crash: hooks detached, state saved, NOTHING is sold.
"""
from __future__ import annotations
import argparse, json, os, pickle, sys, time, traceback, urllib.request, warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
from .data.store import build_market, load_frames, ROOT
from .data.fetch import core_symbols, EXTRA
from .features import Features
from .scanners import SCANNERS, Regime, Card
from .sim.fill_model import Fills
from .sim.position import Plan
from .combos import COMBOS, PROFILE_SCANNERS
from .engine import Account
from .policies import BaselinePolicy
from .llm_policy import LLMPolicy, Reflector, Orchestrator, Native, ContextBuilder, bands_of, contest_block
from .runtime import Control, TIME_KEYS, parse_tehran, fmt_tehran
from .version import code_version
from .backtest import momentum_cards, save_account
from . import metrics as MX

API = "https://api.wallex.ir"
TEHRAN = timezone(timedelta(hours=3, minutes=30))


def http_json(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wallex-agent/1.0"}), timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def ts_to_idx(t, ts) -> int:
    """Timestamp -> index on THIS process's minute grid (LiveFeed.lo differs after every restart)."""
    if ts <= t[-1]:
        return int(np.searchsorted(t, ts))
    return len(t) - 1 + int(round((ts - t[-1]) / 60))


def restore_counters(acc, st):
    """Resume: engine counters saved as timestamps by the executor -> indices of the new grid."""
    t, syms = acc.m.t, acc.m.symbols
    acc.gov_state = st.get("gov_state") or acc.gov_state
    for sym, ts in (st.get("cooldowns") or {}).items():
        if sym in syms:
            acc.cooldown_until[syms.index(sym)] = ts_to_idx(t, float(ts))
    acc.entries_hist.clear()
    for ts in sorted(st.get("entries_hist") or []):
        if float(ts) >= float(t[-1]) - 3600:                 # only the last hour matters (entries_per_hour)
            acc.entries_hist.append(ts_to_idx(t, float(ts)))
    acc.disabled_coins |= set(st.get("disabled_coins") or [])


def drain_executor(acc, ex, j):
    """Closes / adoptions that happened outside the engine, applied on the main thread only. After resume
    adoptions the paper cash is re-based on the live account value (realised P&L of earlier runs included)."""
    m = acc.m
    resumed = False
    for (sym, qty, px, plan) in ex.pop_adoptions():
        if sym not in m.symbols:
            continue
        pos = acc.adopt_position(m.symbols.index(sym), qty, px, j,
                                 Plan(sl_pct=float(plan["sl_pct"]), tps=[tuple(x) for x in plan["tps"]],
                                      time_stop_min=int(plan["time_stop_min"])))
        if plan.get("stop_px"):
            pos.stop = float(plan["stop_px"])
        resumed |= plan.get("source") == "resume"
    if resumed:
        try:
            acc.cash = float(ex.live_equity()) - sum(p.qty * p.entry_px for p in acc.positions.values())
        except Exception as e:
            acc.errors.append({"idx": j, "type": "resume_cash", "err": repr(e)})
    for item in ex.pop_paper_closes():
        sym, px, qty, reason = item[:4]
        if sym in m.symbols:
            acc.close_external(m.symbols.index(sym), j, px, reason)


def client_equity(client) -> float:
    """Σ total balance × bid + TMN (same definition as LiveExecutor.live_equity), before the executor starts."""
    bals, mk = client.balances(), client.markets()
    eq = 0.0
    for asset, (total, _free, _locked) in bals.items():
        eq += total if asset == "TMN" else total * float((mk.get(asset + "TMN") or {}).get("bid") or 0.0)
    return eq


class LiveFeed:
    """Keeps per-symbol frames: history from disk + live 1m candles appended every minute."""

    def __init__(self, symbols, hist_days=4):
        self.symbols = symbols
        now = int(time.time())
        self.lo = (now - hist_days * 86400) // 60 * 60
        self.frames = load_frames(symbols, self.lo, None)
        self.spreads = {}

    def refresh(self):
        now = int(time.time())
        last_complete = now // 60 * 60 - 60
        errs = 0
        for s in self.symbols:
            try:
                j = http_json(f"{API}/v1/udf/history?symbol={s}&resolution=1&from={now - 3 * 3600}&to={now}")
                if j.get("s") != "ok":
                    continue
                df = pd.DataFrame({"t": j["t"], "o": j["o"], "h": j["h"], "l": j["l"], "c": j["c"], "v": j["v"]}).astype(float)
                df["t"] = df["t"].astype(np.int64)
                df = df[df.t <= last_complete]
                old = self.frames[s]
                self.frames[s] = pd.concat([old[old.t < df.t.min()] if len(df) else old, df]).drop_duplicates("t").sort_values("t")
            except Exception:
                errs += 1
        try:
            mk = http_json(f"{API}/v1/markets")["result"]["symbols"]
            for s in self.symbols:
                st = (mk.get(s) or {}).get("stats") or {}
                b, a = float(st.get("bidPrice") or 0), float(st.get("askPrice") or 0)
                if b > 0 and a > 0:
                    self.spreads[s] = (a - b) / ((a + b) / 2) * 100
        except Exception:
            errs += 1
        return last_complete, errs

    def market(self, hi):
        m = build_market(self.frames, lo=self.lo, hi=hi)
        for s, sp in self.spreads.items():           # live spread overrides the morning snapshot
            m.rules[s]["spread_pct"] = sp
        return m


class ReplayFeed(LiveFeed):
    """Offline test of the shadow loop: replays the last N hours from disk, `step_min` minutes per cycle."""
    def __init__(self, symbols, hours, step_min=30):
        frames_all = load_frames(symbols)
        self.end = min(int(f.t.max()) for f in frames_all.values())
        self.cursor = self.end - int(hours * 3600)
        self.lo = self.cursor - 4 * 86400
        self.all = frames_all
        self.symbols, self.spreads, self.step = symbols, {}, step_min * 60
        self.frames = {s: f[(f.t >= self.lo) & (f.t <= self.cursor)] for s, f in frames_all.items()}

    def refresh(self):
        self.cursor = min(self.cursor + self.step, self.end)
        self.frames = {s: f[(f.t >= self.lo) & (f.t <= self.cursor)] for s, f in self.all.items()}
        return self.cursor, 0

    @property
    def done(self):
        return self.cursor >= self.end


class Shadow:
    def __init__(self, a):
        self.a = a
        self.syms = core_symbols() + EXTRA
        self.feed = ReplayFeed(self.syms, a.replay_hours) if a.replay_hours else LiveFeed(self.syms)
        self.out = os.path.join(ROOT, "runs", a.run_id)
        os.makedirs(self.out, exist_ok=True)
        self.accounts = {}
        self.last_j = None
        self.state_path = os.path.join(self.out, "state.pkl")
        self.cycle_errors = []
        cids = a.accounts.split(",")
        self.ctl = Control(a.control, log_dir=self.out) if "C7" in cids else None
        self.ctl_llm = None                      # llm_profile seen in the control file at start (CLI --llm wins at start)
        self.executor = None                     # LiveExecutor (--live)
        self.last_reconcile = 0.0
        self.last_status = 0.0
        self.flatten_started = False
        self.t_start = None                      # bar time of the first processed bar
        self.m_last = None

    # -------------------------------------------------- setup
    def make_accounts(self, m, F, R, f):
        for cid in self.a.accounts.split(","):
            if cid == "C7":
                self.make_c7(m, F, R, f)
                continue
            self.accounts[f"{cid}_baseline"] = Account(cid, BaselinePolicy(cid, COMBOS[cid]), m, f, F, R)
            if self.a.llm:
                pol = LLMPolicy(cid, self.a.llm, anonymize=False, log_path=os.path.join(self.out, f"llm_{cid}.jsonl"))
                acc = Account(cid, pol, m, f, F, R)
                lp = os.path.join(self.out, f"llm_{cid}.jsonl")
                acc.extra = {"orch": Orchestrator(self.a.llm, log_path=lp) if cid == "C6" else None,
                             "native": Native(self.a.llm, log_path=lp) if cid == "C5" else None,
                             "reflect": Reflector(self.a.llm, log_path=lp) if cid == "C6" else None, "n_closed": 0}
                self.accounts[f"{cid}_llm"] = acc

    def make_c7(self, m, F, R, f):
        """One C7 account on the full capital (LLM if --llm, else the rule baseline). The no-LLM twin is P1-01."""
        lp = os.path.join(self.out, "llm_C7.jsonl")
        if self.a.llm:
            pol, key = LLMPolicy("C7", self.a.llm, log_path=lp), "C7_llm"
        else:
            pol, key = BaselinePolicy("C7", COMBOS["C7"]), "C7_baseline"
        acc = Account("C7", pol, m, f, F, R, start_cash=self.a.start_cash or 5_000_000)
        acc.extra = {"native": Native(self.a.llm, log_path=lp, prompt_version="v2") if self.a.llm else None,
                     "reflect": Reflector(self.a.llm, log_path=lp) if self.a.llm else None, "n_closed": 0,
                     "last_native": None, "native_calls": 0, "reflect_calls": 0, "aux_cost_usd": 0.0}
        self.accounts[key] = acc
        if self.a.live:
            self.start_live(acc)
        return acc

    def start_live(self, acc):
        """Attach the live mirror executor (wallex_agent/live). Wallex is the source of truth: start_cash is the
        real account value at the FIRST start and is kept in live_state.json across restarts."""
        from .live.wallex_client import WallexClient, load_key
        from .live.executor import LiveExecutor, load_broker_cfg
        from .version import code_version
        key = load_key(ROOT)
        if not key:
            raise SystemExit("WALLEX_API_KEY missing (.env) - refusing to start --live")
        v = self.ctl.load()
        cfg = load_broker_cfg(os.path.join(ROOT, "config", "broker.yaml"),
                              {"fast_tick_s": self.a.fast_tick, "coins": list(COMBOS["C7"]["coins"]),
                               "stop_limit_offset": COMBOS["C7"]["stop_limit_offset"], "code_version": code_version(),
                               "flatten_start_ts": v["flatten_start_ts"], "hard_deadline_ts": v["hard_deadline_ts"]})
        client = WallexClient(key, cfg, log_path=os.path.join(self.out, "orders.jsonl"))
        ex = LiveExecutor(acc, client, cfg, self.out)
        resumed = os.path.exists(ex.state_path)
        if not resumed:
            acc.start_cash = float(client_equity(client))       # recorded by start() into live_state.json
        ex.start()                                               # resume: restores acc.start_cash from the state
        acc.cash = acc.peak = acc.day_start_eq = acc.start_cash
        if resumed:
            restore_counters(acc, ex.state)
        elif self.a.resume:
            print("note: --resume given but no live_state.json yet -> fresh start")
        acc.hooks, acc.equity_fn = ex, ex.live_equity
        self.executor, self.last_reconcile = ex, time.time()
        print(f"live executor started ({'resumed' if resumed else 'fresh'}): start_cash={acc.start_cash:,.0f} "
              f"stop_supported={ex.stop_supported}")

    def drain_executor(self, acc, j):
        if self.executor is None:
            return
        if time.time() - self.last_reconcile >= 300:
            self.last_reconcile = time.time()
            self.executor.reconcile()
        drain_executor(acc, self.executor, j)

    def apply_control(self, acc, now):
        """Re-read config/runtime.yaml (mtime cached) and apply it: window, control, LLM hot-swap, flatten."""
        if self.ctl is None:
            return
        v = self.ctl.load()
        acc.control = self.ctl
        win = {k + "_ts": v[k + "_ts"] for k in TIME_KEYS}
        if self.a.flatten_start_in is not None and self.t_start is not None:      # dry-run rehearsal of the flatten
            fs = self.t_start + self.a.flatten_start_in * 60
            win["flatten_start_ts"] = fs
            win["no_new_entries_after_ts"] = min(win["no_new_entries_after_ts"], fs)
            win["probe_only_after_ts"] = min(win["probe_only_after_ts"], fs)
        acc.window = win
        if self.a.llm and isinstance(acc.policy, LLMPolicy):
            if self.ctl_llm is None:
                self.ctl_llm = v["llm_profile"]
            elif v["llm_profile"] != self.ctl_llm:
                self.ctl_llm = v["llm_profile"]
                acc.policy.set_profile(v["llm_profile"])
                for k in ("native", "reflect"):
                    if acc.extra.get(k):
                        acc.extra[k].set_profile(v["llm_profile"])
                print(f"[control] LLM switched to {v['llm_profile']}")
        if not acc.flatten_mode and (v["flatten_now"] or now >= win["flatten_start_ts"]):
            acc.flatten_mode = True
            print(f"[control] flatten mode ON at {fmt_tehran(now)} ({'flatten_now' if v['flatten_now'] else 'window'})")
        if acc.flatten_mode and self.executor is not None and not self.flatten_started:
            self.flatten_started = True
            self.executor.start_flatten_loop()

    def native_block(self, acc, j, R):
        """Reason the C7 free scan must not run now (no LLM call is spent), or None."""
        c = COMBOS["C7"]
        m = acc.m
        if len(acc.positions) + len(acc.pending) >= acc._ctl("max_positions", c["max_pos"]):
            return "max_positions"
        if acc._ctl("pause_entries", False) or acc.flatten_mode or acc.gov_state == "hard":
            return "paused"
        t_close = acc._win("no_new_entries_after_ts")
        if t_close is not None and m.t[j] >= t_close:
            return "window_closed"
        if acc.cash - c["size_band"][0] * 1.01 < 0.5 * acc.equity_at(j):
            return "cash_reserve"
        if R.at(j, m.symbols.index("BTCTMN"))["btc_ret15"] <= c["btc15_block"]:
            return "btc_falling"
        return None

    def run_native(self, acc, j, m, R):
        ex = acc.extra
        coins = [m.symbols.index(s) for s in COMBOS["C7"]["coins"] if s in m.symbols]
        ctx = acc.context(Card(j, coins[0], "LLM", 15, 0.0, 0.0, float("nan"), {}), j, "C7")
        state = {"equity_tmn": round(ctx["equity"]), "positions": ctx["positions"], "trades_24h": ctx["trades_24h"],
                 "gov_state": acc.gov_state}
        extra = {s: {"m1_signals_6h": sum(1 for i in acc.m1_hist.get(s, ()) if j - 360 <= i <= j),
                     "held": s in acc.positions} for s in coins}
        bands = dict(bands_of("C7"), max_time_stop_min=max(0, min(1440, int((ctx["hours_to_flatten"] or 24) * 60 - 5))))
        r, picks = ex["native"].pick(acc.policy.cb, j, coins, state, bands, extra=extra, contest=contest_block(ctx))
        ex["native_calls"] += 1
        ex["aux_cost_usd"] += r.cost_usd
        if not r.ok:
            acc.errors.append({"idx": j, "type": "native", "err": r.error})
        for s, b in picks:
            px = float(m.c[s, j])
            card = Card(j, s, "LLM", 15, px, px * (1 - float(b.get("sl_pct") or 5) / 100), float("nan"), {})
            self._direct(acc, card, dict(b, action="enter", profile="C7"), j, "C7")

    def stamp(self, acc) -> dict:
        """Version stamp written on decisions / orders / live trades (GitHub = the code that traded)."""
        return {"code_version": code_version(), "prompt_version": "decide:v2,native:v2,reflect:v1",
                "llm_profile": getattr(acc.policy, "llm_profile", None) or "baseline"}

    def step_c7(self, acc, j0, j1, cards, m, F, R):
        n_dec = len(acc.decisions)
        try:
            self._step_c7(acc, j0, j1, cards, m, F, R)
        finally:
            tag = self.stamp(acc)
            for d in acc.decisions[n_dec:]:
                d.update(tag)
            if self.executor is not None:
                self.executor.tags = tag
                self.executor.client.log_tags = tag

    def _step_c7(self, acc, j0, j1, cards, m, F, R):
        ex = acc.extra
        coins = {m.symbols.index(s) for s in COMBOS["C7"]["coins"] if s in m.symbols}
        for j in range(j0, j1 + 1):
            try:
                self.drain_executor(acc, j)
                acc.on_bar(j)
                for card in cards.get(j, ()):
                    if card.scanner == "M1" and card.sym in coins:
                        acc.on_card(card, j)
                # free LLM scan: elapsed-time schedule, latest bar only, and only when an entry is possible
                if j == j1 and ex.get("native"):
                    now = float(m.t[j])
                    every = 60 * acc._ctl("native_every_min", 30)
                    if ex["last_native"] is None:
                        ex["last_native"] = now - every if self.a.native_at_start else now
                    if now - ex["last_native"] >= every:
                        why = self.native_block(acc, j, R)
                        if why:
                            acc.decisions.append({"idx": j, "result": "native_skipped", "why": why})
                        else:
                            ex["last_native"] = now
                            self.run_native(acc, j, m, R)
                if ex.get("reflect") and len(acc.trades) > ex["n_closed"]:
                    for tr in acc.trades[ex["n_closed"]:]:
                        try:
                            lesson, tag = ex["reflect"].reflect(tr, tr.sym)
                            ex["reflect_calls"] += 1
                            if lesson:
                                acc.policy.lessons.append((tr.sym, tr.scanner, tag, lesson))
                        except Exception as e:
                            acc.errors.append({"idx": j, "type": "reflect", "err": repr(e)})
                    ex["n_closed"] = len(acc.trades)
            except Exception as e:
                acc.errors.append({"idx": j, "type": "cycle", "err": repr(e), "tb": traceback.format_exc()[-800:]})

    def seed_m1_hist(self, acc, F, j0):
        """Start/resume: M1 cards of the last arm window, so a restart does not re-arm coins (set -> no duplicates)."""
        w = COMBOS["C7"]["arm_window_min"]
        coins = {acc.m.symbols.index(s) for s in COMBOS["C7"]["coins"] if s in acc.m.symbols}
        for c in SCANNERS["M1"](F):
            if c.sym in coins and j0 - w <= c.idx < j0:
                acc.m1_hist[c.sym].add(c.idx)

    def rebind(self, m, F, R, f):
        for acc in self.accounts.values():
            acc.m, acc.F, acc.R, acc.f = m, F, R, f
            acc.atr15, acc.liq15, acc.end15 = F.x[15]["atr"], F.x[15]["liq"], F.tf[15].end_idx
            if isinstance(acc.policy, LLMPolicy):
                acc.policy.bind(m, F, R)

    # -------------------------------------------------- per-account step
    def step_account(self, key, acc, j0, j1, cards, m, F, R):
        cid = acc.cid
        if cid == "C7":
            return self.step_c7(acc, j0, j1, cards, m, F, R)
        is_llm = isinstance(acc.policy, LLMPolicy)
        ex = getattr(acc, "extra", {}) or {}
        for j in range(j0, j1 + 1):
            try:
                acc.on_bar(j)
                # C6 LLM orchestrator every 30 min
                if is_llm and cid == "C6" and int(m.t[j]) % (self.a.orch_every * 60) == self.a.orch_every * 60 - 60:
                    coins = [m.symbols.index(s) for s in COMBOS["C6"]["coins"]]
                    ex["orch"].update(acc.policy.cb, j, coins, acc.policy.lessons)
                # C5 LLM native every 15 min
                if is_llm and cid == "C5" and int(m.t[j]) % (self.a.native_every * 60) == self.a.native_every * 60 - 60:
                    coins = [m.symbols.index(s) for s in COMBOS["C5"]["coins"]]
                    state = {"equity_tmn": round(acc.equity_at(j)), "open_positions": [m.symbols[s] for s in acc.positions]}
                    r, picks = ex["native"].pick(acc.policy.cb, j, coins, state, bands_of("C5"))
                    for s, b in picks:
                        card = Card(j, s, "LLM", 15, float(m.c[s, j]), float(m.c[s, j] * (1 - float(b.get("sl_pct", 2)) / 100)), float("nan"))
                        dec = dict(b, action="enter", profile="C5")
                        acc.on_card(card, j, "C5") if False else self._direct(acc, card, dec, j)
                for card in cards.get(j, ()):
                    if card.scanner not in COMBOS[cid]["scanners"]:
                        continue
                    if cid == "C5":
                        if is_llm:
                            continue                         # LLM C5 uses its own picks
                        acc.on_card(card, j); continue
                    if cid == "C6":
                        reg = R.at(j, card.sym)["coin"]
                        prof = COMBOS["C6"]["profile_by_regime"].get(reg)
                        if is_llm and ex.get("orch") and ex["orch"].assign:
                            prof = ex["orch"].assign.get(m.symbols[card.sym], prof)
                            prof = None if prof == "off" else prof
                        if prof is None or card.scanner not in PROFILE_SCANNERS[prof]:
                            acc._reject(card, None, f"router_{reg}_{prof}", j); continue
                        acc.on_card(card, j, prof)
                    elif cid == "C4":
                        b = acc._b15(j)
                        if b < 0 or not (F.x[15]["rsi"][card.sym, b] < 45):
                            acc._reject(card, None, "gate", j); continue
                        acc.on_card(card, j)
                    else:
                        acc.on_card(card, j)
                # C6 reflection on newly closed trades
                if is_llm and cid == "C6" and len(acc.trades) > ex.get("n_closed", 0):
                    for tr in acc.trades[ex["n_closed"]:]:
                        try:
                            lesson, tag = ex["reflect"].reflect(tr, tr.sym)
                            if lesson:
                                acc.policy.lessons.append((tr.sym, tr.scanner, tag, lesson))
                        except Exception as e:
                            acc.errors.append({"idx": j, "type": "reflect", "err": repr(e)})
                    ex["n_closed"] = len(acc.trades)
            except Exception as e:
                acc.errors.append({"idx": j, "type": "cycle", "err": repr(e), "tb": traceback.format_exc()[-800:]})

    def _direct(self, acc, card, dec, j, profile="C5"):
        """Pass an already-made LLM decision (C5 / C7 native) through the same guardrails."""
        res, why = acc.guard(card, dec, j, profile)
        if res is None:
            return acc._reject(card, dec, why, j)
        plan, notional = res
        acc.pending.append((j + 1, card, dec, profile, j + 1, plan, notional))
        acc.decisions.append({"idx": j, "sym": acc.m.symbols[card.sym], "scanner": "LLM", "decision": dec,
                              "result": "accepted", "notional": notional, "profile": profile, **acc._guard_info})

    # -------------------------------------------------- main loop
    def run(self):
        until = datetime.strptime(self.a.until, "%H:%M").time() if self.a.until else None
        end_ts = None
        if self.a.end:
            end_ts, until = parse_tehran(self.a.end), None
        if until:
            now = datetime.now(TEHRAN)
            end_dt = now.replace(hour=until.hour, minute=until.minute, second=0, microsecond=0)
            if end_dt <= now:
                end_dt += timedelta(days=1)
            end_ts = end_dt.timestamp()
        if self.a.hours:
            end_ts = time.time() + self.a.hours * 3600
        print(f"shadow {self.a.run_id}: accounts={self.a.accounts} llm={self.a.llm} until={datetime.fromtimestamp(end_ts, TEHRAN) if end_ts else '-'}")
        last_report = 0
        try:
            self.loop(end_ts, last_report)
        except KeyboardInterrupt:
            return self.safe_stop("KeyboardInterrupt")
        except Exception as e:
            self.cycle_errors.append({"t": time.time(), "err": repr(e), "tb": traceback.format_exc()[-1500:]})
            self.safe_stop(f"main loop exception: {e!r}")
            sys.exit(1)                              # supervisor restarts with --resume
        if self.executor is not None:
            self.executor.final_flatten()            # balance-based, independent of the paper engine
        for acc in self.accounts.values():
            acc.hooks = None                         # paper bookkeeping below must not send orders again
        if self.executor is not None:
            self.executor.stop()
        m = self.feed.market(self.feed.cursor if isinstance(self.feed, ReplayFeed) else int(time.time()) // 60 * 60 - 60)
        for acc in self.accounts.values():
            acc.m = m
            acc.close_all(m.T - 1)
            acc.equity.append(acc.cash)          # final equity = cash after closing everything to TMN
        self.save(m, final=True)
        print("shadow finished", self.out)

    def safe_stop(self, why):
        """Ctrl+C / crash: detach hooks, stop the executor thread, save. Never sells, never closes positions."""
        print(f"stopping ({why}): hooks detached, state saved, no orders sent")
        for acc in self.accounts.values():
            acc.hooks = None
        if self.executor is not None:
            try:
                self.executor.stop()
            except Exception as e:
                print("executor stop error:", repr(e))
        if self.m_last is not None:
            self.save(self.m_last)

    def loop(self, end_ts, last_report):
        while True:
            t_cycle = time.time()
            try:
                last_complete, ferrs = self.feed.refresh()
                m = self.feed.market(last_complete)
                F = Features(m); R = Regime(F); f = Fills(m)
                first = not self.accounts
                if first:
                    self.make_accounts(m, F, R, f)
                    self.last_j = m.T - 2
                self.rebind(m, F, R, f)
                self.m_last = m
                j1 = m.T - 1
                j0 = self.last_j + 1
                if self.t_start is None:
                    self.t_start = float(m.t[j0])
                for acc in self.accounts.values():
                    if acc.cid == "C7":
                        if first:
                            self.seed_m1_hist(acc, F, j0)
                        self.apply_control(acc, float(m.t[j1]))
                if j1 >= j0:
                    cards = defaultdict(list)
                    for k, fn in SCANNERS.items():
                        for c in fn(F):
                            if j0 <= c.idx <= j1:
                                cards[c.idx].append(c)
                    for c in momentum_cards(F):
                        if j0 <= c.idx <= j1:
                            cards[c.idx].append(c)
                    with ThreadPoolExecutor(len(self.accounts)) as ex:
                        list(ex.map(lambda kv: self.step_account(kv[0], kv[1], j0, j1, cards, m, F, R), self.accounts.items()))
                    self.last_j = j1
                if self.executor is not None:
                    self.executor.save_state()
                self.heartbeat(m, ferrs, time.time() - t_cycle)
                if time.time() - self.last_status >= 300 or isinstance(self.feed, ReplayFeed):
                    self.last_status = time.time()
                    self.write_status(m)
                if time.time() - last_report > self.a.report_every * 60:
                    self.save(m); last_report = time.time()
            except Exception as e:
                self.cycle_errors.append({"t": time.time(), "err": repr(e), "tb": traceback.format_exc()[-1500:]})
                print("cycle error:", repr(e))
            if isinstance(self.feed, ReplayFeed):
                if self.feed.done:
                    break
                continue
            if end_ts and time.time() >= end_ts:
                break
            time.sleep(max(5, 65 - (time.time() % 60)))      # run ~5s after each minute closes

    def heartbeat(self, m, ferrs, dt):
        hb = {"t": time.time(), "last_bar_t": int(m.t[-1]), "cycle_s": round(dt, 1), "feed_errors": ferrs,
              "accounts": {k: {"equity": round(a.equity_at(m.T - 1)), "open": len(a.positions), "trades": len(a.trades),
                               "errors": len(a.errors)} for k, a in self.accounts.items()},
              "cycle_errors": len(self.cycle_errors)}
        for k, a in self.accounts.items():
            if a.cid != "C7":
                continue
            ex = a.extra or {}
            calls = getattr(a.policy, "calls", [])
            h = hb["accounts"][k]
            h.update(equity_live=round(a.equity_now(m.T - 1)), gov_state=a.gov_state, flatten_mode=a.flatten_mode,
                     pending=len(a.pending), llm_calls=len(calls) + ex.get("native_calls", 0) + ex.get("reflect_calls", 0),
                     llm_cost_usd=round(sum(c.get("cost_usd") or 0 for c in calls) + ex.get("aux_cost_usd", 0.0), 4))
            if self.executor is not None:
                try:
                    h["open_orders"] = len(self.executor.client.open_orders())
                except Exception as e:
                    h["open_orders"] = f"error: {e!r}"
        json.dump(hb, open(os.path.join(self.out, "heartbeat.json"), "w"), indent=1)

    def write_status(self, m):
        """runs/<run>/status.json every 5 minutes: live value, drawdown, positions, orders, trades, LLM spend."""
        for key, acc in self.accounts.items():
            if acc.cid != "C7":
                continue
            j = m.T - 1
            eq = acc.equity_now(j)
            ex = self.executor
            live = getattr(ex, "live", {}) if ex is not None else {}
            pos = []
            for s, p in acc.positions.items():
                sym = m.symbols[s]
                r = live.get(sym) or {}
                avg = float(r.get("avg_px") or p.entry_px)
                last = float(m.c[s, j])
                pos.append({"sym": sym, "qty": float(r.get("qty") or p.qty), "avg_px": avg, "last": last,
                            "pnl_pct": round((last / avg - 1) * 100, 2) if avg else None,
                            "stop_px": float(r.get("stop_px") or p.stop), "server_stop": bool(r.get("server_stop")),
                            "size_tier": ((p.meta or {}).get("dec") or {}).get("size_tier")})
            if ex is not None:
                try:
                    open_orders = len(ex.client.open_orders())
                except Exception as e:
                    open_orders = f"error: {e!r}"
                rows = []
                if os.path.exists(ex.trades_path):
                    import csv
                    rows = list(csv.DictReader(open(ex.trades_path, encoding="utf-8")))
                pnl = [float(r.get("pnl_tmn") or 0) for r in rows]
            else:
                open_orders, pnl = len(acc.pending), [t.pnl for t in acc.trades]
            hb = json.load(open(os.path.join(self.out, "heartbeat.json"), encoding="utf-8"))["accounts"].get(key, {})
            errs = acc.errors[-5:] + self.cycle_errors[-3:]
            st = {"t": time.time(), "tehran": fmt_tehran(time.time()), "account": key, "live": ex is not None,
                  "equity_live": round(eq), "start_cash": round(acc.start_cash),
                  "dd_pct": round((1 - eq / acc.start_cash) * 100, 3), "gov_state": acc.gov_state,
                  "flatten_mode": acc.flatten_mode, "positions": pos, "open_orders": open_orders,
                  "trades": {"n": len(pnl), "wins": sum(1 for x in pnl if x > 0), "pnl_tmn": round(sum(pnl))},
                  "llm": {"calls": hb.get("llm_calls", 0), "cost_usd": hb.get("llm_cost_usd", 0.0),
                          "profile": getattr(acc.policy, "llm_profile", None)},
                  "errors_last": [{k: e.get(k) for k in ("t", "idx", "type", "err") if k in e} for e in errs],
                  **self.stamp(acc)}
            text = json.dumps(st, ensure_ascii=False, indent=1, default=str)
            for i in range(5):                                 # a file open in Excel must not stop the bot
                try:
                    with open(os.path.join(self.out, "status.json"), "w", encoding="utf-8") as f:
                        f.write(text)
                    break
                except OSError:
                    time.sleep(0.2 * (i + 1))

    def save(self, m, final=False):
        for key, acc in self.accounts.items():
            start = self.first_idx(acc)
            if not acc.equity:
                continue
            extra = {"mode": "shadow", "llm": self.a.llm if "llm" in key else None,
                     "llm_calls": getattr(acc.policy, "calls", [])[-2000:],
                     "lessons": [l[3] for l in getattr(acc.policy, "lessons", [])]}
            save_account(acc, type("C", (), {"m": m})(), os.path.join(self.out, key, "shadow"), start, start + len(acc.equity), extra)
            self.save_positions(acc, m, os.path.join(self.out, key, "shadow"))
        json.dump(self.cycle_errors[-200:], open(os.path.join(self.out, "cycle_errors.json"), "w"), indent=1)

    def save_positions(self, acc, m, folder):
        """Open positions with real simulated fills (read by the report)."""
        out = []
        for s, p in acc.positions.items():
            dec = (p.meta or {}).get("dec") or {}
            card = (p.meta or {}).get("card")
            out.append({"sym": m.symbols[s], "scanner": getattr(card, "scanner", None), "profile": (p.meta or {}).get("profile"),
                        "entry_t": int(m.t[p.entry_idx]), "entry_px": float(p.entry_px), "qty": float(p.qty),
                        "notional": float(p.entry_px * p.qty), "stop_px": float(p.stop), "sl_pct": float(p.plan.sl_pct),
                        "tp_plan": [{"pct": float(a), "portion": float(b)} for a, b in p.plan.tps], "tp_done": int(p.tp_done),
                        "time_stop_min": int(p.plan.time_stop_min or 0), "thesis": dec.get("thesis", ""), "approx": False})
        json.dump(out, open(os.path.join(folder, "positions.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    def first_idx(self, acc):
        return (self.last_j + 1 - len(acc.equity)) if acc.equity else 0


def _stop_signals():
    """Ctrl+C and SIGTERM (docker stop / supervisor) both mean: safe_stop - hooks detached, state saved, no orders.
    SIGINT is re-armed explicitly because a process started in the background inherits 'ignore SIGINT'."""
    import signal

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, on_term)


def main():
    _stop_signals()
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default=None, help="LLM profile (omit = baselines only)")
    ap.add_argument("--accounts", default="C1,C2,C3,C4,C5,C6")
    ap.add_argument("--until", default=None, help="HH:MM Tehran time")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--report-every", type=int, default=30, help="minutes")
    ap.add_argument("--run-id", default=time.strftime("shadow_%Y%m%d_%H%M"))
    ap.add_argument("--native-every", type=int, default=15, help="C5 LLM-native pick period, minutes (15/30/60/120)")
    ap.add_argument("--orch-every", type=int, default=30, help="C6 orchestrator period, minutes")
    ap.add_argument("--replay-hours", type=float, default=None, help="offline test: replay last N hours from disk")
    # C7 / live run
    ap.add_argument("--end", default=None, help='"YYYY-MM-DD HH:MM" Tehran; wins over --until')
    ap.add_argument("--live", action="store_true", help="mirror C7 opens/closes on Wallex (real orders)")
    ap.add_argument("--resume", action="store_true", help="continue from runs/<run-id>/live_state.json")
    ap.add_argument("--control", default=os.path.join(ROOT, "config", "runtime.yaml"))
    ap.add_argument("--fast-tick", type=int, default=15, help="seconds between executor stop/order checks")
    ap.add_argument("--native-at-start", action="store_true", help="C7: run the free LLM scan on the first cycle")
    ap.add_argument("--flatten-start-in", type=int, default=None, help="dry run only: start the flatten after N minutes")
    ap.add_argument("--start-cash", type=float, default=None, help="paper start value for C7 (default 5,000,000)")
    a = ap.parse_args()
    if a.flatten_start_in is not None and a.live:
        ap.error("--flatten-start-in is a dry-run rehearsal; not allowed with --live")
    Shadow(a).run()


if __name__ == "__main__":
    main()
