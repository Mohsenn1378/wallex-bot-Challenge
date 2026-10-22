"""
Test C: live shadow test. Same engine as the backtest, fed by live Wallex candles; orders are simulated
(paper), nothing is sent to the exchange.
    python -m wallex_agent.shadow --llm sol_opencode --until 19:00 --accounts C1,C2,C3,C4,C5,C6
Every account runs twice: rule baseline and LLM. State is saved every cycle; restart resumes.
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
from .combos import COMBOS, PROFILE_SCANNERS
from .engine import Account
from .policies import BaselinePolicy
from .llm_policy import LLMPolicy, Reflector, Orchestrator, Native, ContextBuilder, bands_of
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

    # -------------------------------------------------- setup
    def make_accounts(self, m, F, R, f):
        for cid in self.a.accounts.split(","):
            self.accounts[f"{cid}_baseline"] = Account(cid, BaselinePolicy(cid, COMBOS[cid]), m, f, F, R)
            if self.a.llm:
                pol = LLMPolicy(cid, self.a.llm, anonymize=False, log_path=os.path.join(self.out, f"llm_{cid}.jsonl"))
                acc = Account(cid, pol, m, f, F, R)
                lp = os.path.join(self.out, f"llm_{cid}.jsonl")
                acc.extra = {"orch": Orchestrator(self.a.llm, log_path=lp) if cid == "C6" else None,
                             "native": Native(self.a.llm, log_path=lp) if cid == "C5" else None,
                             "reflect": Reflector(self.a.llm, log_path=lp) if cid == "C6" else None, "n_closed": 0}
                self.accounts[f"{cid}_llm"] = acc

    def rebind(self, m, F, R, f):
        for acc in self.accounts.values():
            acc.m, acc.F, acc.R, acc.f = m, F, R, f
            acc.atr15, acc.liq15, acc.end15 = F.x[15]["atr"], F.x[15]["liq"], F.tf[15].end_idx
            if isinstance(acc.policy, LLMPolicy):
                acc.policy.bind(m, F, R)

    # -------------------------------------------------- per-account step
    def step_account(self, key, acc, j0, j1, cards, m, F, R):
        cid = acc.cid
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

    def _direct(self, acc, card, dec, j):
        """Pass an already-made LLM decision (C5 native) through the same guardrails."""
        res, why = acc.guard(card, dec, j, "C5")
        if res is None:
            return acc._reject(card, dec, why, j)
        plan, notional = res
        acc.pending.append((j + 1, card, dec, "C5", j + 1, plan, notional))
        acc.decisions.append({"idx": j, "sym": acc.m.symbols[card.sym], "scanner": "LLM", "decision": dec,
                              "result": "accepted", "notional": notional, "profile": "C5"})

    # -------------------------------------------------- main loop
    def run(self):
        until = datetime.strptime(self.a.until, "%H:%M").time() if self.a.until else None
        end_ts = None
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
        while True:
            t_cycle = time.time()
            try:
                last_complete, ferrs = self.feed.refresh()
                m = self.feed.market(last_complete)
                F = Features(m); R = Regime(F); f = Fills(m)
                if not self.accounts:
                    self.make_accounts(m, F, R, f)
                    self.last_j = m.T - 2
                self.rebind(m, F, R, f)
                j1 = m.T - 1
                j0 = self.last_j + 1
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
                self.heartbeat(m, ferrs, time.time() - t_cycle)
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
        m = self.feed.market(self.feed.cursor if isinstance(self.feed, ReplayFeed) else int(time.time()) // 60 * 60 - 60)
        for acc in self.accounts.values():
            acc.m = m
            acc.close_all(m.T - 1)
            acc.equity.append(acc.cash)          # final equity = cash after closing everything to TMN
        self.save(m, final=True)
        print("shadow finished", self.out)

    def heartbeat(self, m, ferrs, dt):
        hb = {"t": time.time(), "last_bar_t": int(m.t[-1]), "cycle_s": round(dt, 1), "feed_errors": ferrs,
              "accounts": {k: {"equity": round(a.equity_at(m.T - 1)), "open": len(a.positions), "trades": len(a.trades),
                               "errors": len(a.errors)} for k, a in self.accounts.items()},
              "cycle_errors": len(self.cycle_errors)}
        json.dump(hb, open(os.path.join(self.out, "heartbeat.json"), "w"), indent=1)

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


def main():
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
    a = ap.parse_args()
    Shadow(a).run()


if __name__ == "__main__":
    main()
