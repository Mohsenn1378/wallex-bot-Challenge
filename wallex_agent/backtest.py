"""
Backtest runner (baseline policies; LLM policies plug in the same way).
    python -m wallex_agent.backtest --days 35 --combos C1,C2,C3,C4,C5,C6
"""
from __future__ import annotations
import argparse, csv, dataclasses, hashlib, json, os, sys, time, traceback, warnings
from collections import defaultdict
import numpy as np

warnings.filterwarnings("ignore")
from .data.store import load_market, ROOT
from .data.fetch import core_symbols, EXTRA
from .features import Features, shift
from .scanners import SCANNERS, Regime, Card
from .sim.fill_model import Fills
from .combos import COMBOS, PROFILE_SCANNERS
from .engine import Account
from .policies import BaselinePolicy
from . import metrics as MX


def momentum_cards(F, every=240, top=2, min_liq=0.25):
    """C5 baseline: every `every` minutes buy the top-k coins by 4h return (if positive)."""
    tf = F.tf[15]; x = F.x[15]
    syms = F.m.symbols
    r4 = tf.c / shift(tf.c, 16) - 1
    out = []
    step = every // 15
    for b in range(16, tf.n, step):
        cand = []
        for s, sym in enumerate(syms):
            if sym in ("BTCUSDT", "USDTTMN"):
                continue
            if np.nan_to_num(x["liq"][s, b]) < min_liq or not np.isfinite(r4[s, b]):
                continue
            cand.append((r4[s, b], s))
        cand.sort(reverse=True)
        for r, s in cand[:top]:
            if r > 0:
                out.append(Card(int(tf.end_idx[b]), s, "MOM", 15, float(tf.c[s, b]),
                                float(tf.c[s, b] * 0.98), float("nan"), {"ret4h": float(r)}))
    return out


class Ctx:
    """Everything shared by all accounts in one run (built once)."""
    def __init__(self, days, spread_mult=1.5):
        t0 = time.time()
        self.m = load_market(core_symbols() + EXTRA, days=days)
        self.F = Features(self.m)
        self.R = Regime(self.F)
        self.f = Fills(self.m, spread_mult=spread_mult)
        self.cards = {k: fn(self.F) for k, fn in SCANNERS.items()}
        self.cards["MOM"] = momentum_cards(self.F)
        self.build_s = time.time() - t0


def run_account(ctx: Ctx, cid: str, policy, start_j: int, end_j: int, start_cash=2_500_000):
    cfg = COMBOS[cid]
    acct = Account(cid, policy, ctx.m, ctx.f, ctx.F, ctx.R, start_cash)
    by_idx = defaultdict(list)
    for sc in cfg["scanners"]:
        for c in ctx.cards[sc]:
            if start_j <= c.idx < end_j - 1:
                by_idx[c.idx].append(c)
    rsi15 = ctx.F.x[15]["rsi"]
    for j in range(start_j, end_j):
        acct.on_bar(j)
        for card in by_idx.get(j, ()):
            if cid == "C6":
                reg = ctx.R.at(j, card.sym)["coin"]
                prof = cfg["profile_by_regime"].get(reg)
                if prof is None or card.scanner not in PROFILE_SCANNERS[prof]:
                    acct._reject(card, None, f"router_{reg}_{prof}", j); continue
                acct.on_card(card, j, prof)
            elif cid == "C4":
                b = acct._b15(j)
                if b < 0 or not (rsi15[card.sym, b] < 45):          # rule proxy for ML gate M9
                    acct._reject(card, None, "gate", j); continue
                acct.on_card(card, j)
            else:
                acct.on_card(card, j)
    acct.close_all(end_j - 1)
    acct.equity.append(acct.cash)
    return acct


def save_account(acct, ctx, folder, start_j, end_j, extra=None):
    os.makedirs(folder, exist_ok=True)
    mx = MX.compute(acct, start_j, end_j, ctx.m.t)
    with open(os.path.join(folder, "trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = ["sym", "scanner", "profile", "entry_t", "entry_px", "qty", "notional", "sl_pct", "tp_plan",
                "exit_t", "exit_px", "exit_reason", "fees", "pnl", "pnl_pct", "r_mult", "thesis", "regime_coin", "btc_bull"]
        w.writerow(cols)
        for x in acct.trades:
            w.writerow([x.sym, x.scanner, x.profile, x.entry_t, round(x.entry_px, 6), x.qty, round(x.notional), x.sl_pct,
                        json.dumps(x.tp_plan), x.exit_t, round(x.exit_px, 6), x.exit_reason, round(x.fees, 1),
                        round(x.pnl, 1), round(x.pnl_pct, 3), round(x.r_mult, 3), x.decision.get("thesis", ""),
                        x.regime.get("coin"), x.regime.get("btc_bull")])
    with open(os.path.join(folder, "decisions.jsonl"), "w", encoding="utf-8") as f:
        for d in acct.decisions:
            d = dict(d); d["t"] = int(ctx.m.t[d["idx"]])
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
    with open(os.path.join(folder, "errors.jsonl"), "w", encoding="utf-8") as f:
        for e in acct.errors:
            f.write(json.dumps(e, default=str) + "\n")
    eq = np.asarray(acct.equity)
    np.save(os.path.join(folder, "equity.npy"), eq[::5])
    json.dump({"metrics": mx, **(extra or {})}, open(os.path.join(folder, "metrics.json"), "w"), indent=1, default=str)
    return mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--combos", default="C1,C2,C3,C4,C5,C6")
    ap.add_argument("--window-days", type=int, default=2, help="also run fresh accounts on each N-day window")
    ap.add_argument("--spread-mult", type=float, default=1.5)
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()
    ctx = Ctx(a.days, a.spread_mult)
    print(f"context built in {ctx.build_s:.1f}s  bars={ctx.m.T}  cards=" +
          ", ".join(f"{k}:{len(v)}" for k, v in ctx.cards.items()))
    run_id = a.run_id or time.strftime("bt_%Y%m%d_%H%M")
    base = os.path.join(ROOT, "runs", run_id)
    warm = 200 * 60                       # skip first ~3.3 days so EMA200(15m)/indicators are warm
    T = ctx.m.T
    summary = {}
    for cid in a.combos.split(","):
        t0 = time.time()
        try:
            acct = run_account(ctx, cid, BaselinePolicy(cid, COMBOS[cid]), warm, T)
            mx = save_account(acct, ctx, os.path.join(base, f"{cid}_baseline", "full"), warm, T)
            # rolling fresh 2-day windows (closest simulation of the contest)
            wins = []
            W = a.window_days * 1440
            for s0 in range(warm, T - W + 1, W):
                w = run_account(ctx, cid, BaselinePolicy(cid, COMBOS[cid]), s0, s0 + W)
                wm = MX.compute(w, s0, s0 + W, ctx.m.t)
                wins.append({"start_t": int(ctx.m.t[s0]), **{k: wm[k] for k in
                             ("R1_net_return_pct", "D2_dd_from_start_pct", "D1_maxdd_peak_pct", "trades")}})
            r2 = [w["R1_net_return_pct"] for w in wins]
            r2s = {"median": float(np.median(r2)), "p25": float(np.percentile(r2, 25)), "worst": float(min(r2)),
                   "best": float(max(r2)), "positive_share": float(np.mean(np.array(r2) > 0)), "n": len(r2)}
            score = MX.jsp(mx, r2s["median"])
            json.dump({"windows": wins, "R2": r2s, "jsp": score}, open(os.path.join(base, f"{cid}_baseline", "windows.json"), "w"), indent=1)
            summary[cid] = {"full": mx, "R2": r2s, "jsp": score}
            print(f"{cid} baseline  {time.time()-t0:5.1f}s  ret={mx['R1_net_return_pct']:+.2f}%  dd={mx['D1_maxdd_peak_pct']:.2f}%  "
                  f"trades={mx['trades']}  pf={mx['R3_profit_factor']}  win={mx['win_rate_pct']}%  "
                  f"2d median={r2s['median']:+.2f}% worst={r2s['worst']:+.2f}% pos={r2s['positive_share']:.0%}  JSP={score['total']}")
        except Exception as e:
            traceback.print_exc()
            summary[cid] = {"error": repr(e)}
    json.dump(summary, open(os.path.join(base, "summary.json"), "w"), indent=1, default=str)
    print("saved", base)


if __name__ == "__main__":
    main()
