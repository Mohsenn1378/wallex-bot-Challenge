"""
Test B: offline per-card evaluation of LLM decisions vs the rule baseline (same cards, same simulator).
    python -m wallex_agent.eval_cards --llm sol_opencode --per-combo 150 --anonymize
Outputs runs/<run_id>/cards_<combo>.jsonl + cards_summary.json (AI lift, CI, shuffle control, filter precision).
"""
from __future__ import annotations
import argparse, json, os, random, time, warnings
from concurrent.futures import ThreadPoolExecutor
import numpy as np
warnings.filterwarnings("ignore")
from .backtest import Ctx
from .combos import COMBOS, PROFILE_SCANNERS
from .policies import baseline_plan
from .llm_policy import LLMPolicy
from .sim.position import Plan
from .sim.single import simulate
from .data.store import ROOT


def to_plan(dec, profile):
    c = COMBOS[profile]
    band = c.get("sl_band", (1.5, 4))
    sl = min(max(float(dec.get("sl_pct") or sum(band) / 2), band[0]), band[1])
    if "risk_tmn" in c:
        notional = c["risk_tmn"] * float(dec.get("risk_mult", 1)) / (sl / 100)
    else:
        notional = float(dec.get("size_tmn") or c.get("size", 55_000))
    lo, hi = c.get("size_band", (55_000, 200_000)); notional = min(max(notional, lo), hi)
    tb = c.get("tp_band")
    tps = sorted([(min(max(float(t["pct"]), tb[0]), tb[1]) if tb else float(t["pct"]), float(t["portion"]))
                  for t in (dec.get("tp_plan") or [])[:3]])
    if not tps:
        tps = [(max(sl * 1.5, 1.0), 1.0)]
    ts = min(int(dec.get("time_stop_min") or c.get("time_stop", 720)), c.get("time_stop", 1440) or 1440)
    plan = Plan(sl_pct=sl, tps=tps, time_stop_min=ts, be_trigger_pct=c.get("be_pct", 0) or (sl * c["be_at_r"] if "be_at_r" in c else 0),
                entry=c.get("entry", "market"), limit_valid_min=c.get("limit_valid", 10))
    return plan, notional


def eligible(ctx, card, cid):
    c = COMBOS[cid]
    sym = ctx.m.symbols[card.sym]
    coins = c.get("coins")
    if coins not in (None, "core") and sym not in coins:
        return None
    b = int(np.searchsorted(ctx.F.tf[15].end_idx, card.idx, side="right") - 1)
    if b < 0 or np.nan_to_num(ctx.F.x[15]["liq"][card.sym, b]) < c.get("min_liq", 0):
        return None
    reg = ctx.R.at(card.idx, card.sym)
    prof = cid
    if cid == "C6":
        prof = c["profile_by_regime"].get(reg["coin"])
        if prof is None or card.scanner not in PROFILE_SCANNERS[prof]:
            return None
    pc = COMBOS[prof]
    if pc.get("regime_ok") and reg["coin"] not in pc["regime_ok"]:
        return None
    if pc.get("need_btc_bull") and not reg["btc_bull"]:
        return None
    if prof == "C4" and not (ctx.F.x[15]["rsi"][card.sym, b] < 45):
        return None
    return prof


def bootstrap_ci(x, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    x = np.asarray(x)
    if len(x) < 5:
        return [None, None]
    means = [rng.choice(x, len(x)).mean() for _ in range(n)]
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", required=True)
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--eval-days", type=int, default=12, help="cards from the last N days only (out of tuning sample)")
    ap.add_argument("--combos", default="C1,C2,C3,C4,C6")
    ap.add_argument("--per-combo", type=int, default=150)
    ap.add_argument("--anonymize", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()
    ctx = Ctx(a.days)
    run_id = a.run_id or time.strftime("cards_%Y%m%d_%H%M")
    out = os.path.join(ROOT, "runs", run_id); os.makedirs(out, exist_ok=True)
    start_idx = ctx.m.T - a.eval_days * 1440
    rnd = random.Random(42)
    summary = {}
    for cid in a.combos.split(","):
        pool = []
        for sc in COMBOS[cid]["scanners"]:
            for card in ctx.cards[sc]:
                if card.idx >= start_idx and card.idx < ctx.m.T - 1500:
                    prof = eligible(ctx, card, cid)
                    if prof:
                        pool.append((card, prof))
        rnd.shuffle(pool)
        pool = pool[:a.per_combo]
        pol = LLMPolicy(cid, a.llm, anonymize=a.anonymize, log_path=os.path.join(out, f"llm_calls_{cid}.jsonl"))
        pol.bind(ctx.m, ctx.F, ctx.R)

        def work(item):
            card, prof = item
            cx = {"idx": card.idx, "profile": prof, "equity": 2_500_000, "open_positions": []}
            base = baseline_plan(prof, card, cx)
            bp, bn = to_plan(base, prof)
            ob = simulate(ctx.m, ctx.f, ctx.F, card, bp, bn)
            t0 = time.time()
            try:
                d = pol.decide(card, cx); err = None
            except Exception as e:
                d, err = None, str(e)
            if d and d.get("action") == "enter":
                lp, ln = to_plan(d, prof)
                ol = simulate(ctx.m, ctx.f, ctx.F, card, lp, ln)
            else:
                ol = {"filled": False, "pnl_pct": 0.0, "pnl_tmn": 0.0, "reason": "SKIP" if d else "ERROR", "hold_min": 0}
            return {"sym": ctx.m.symbols[card.sym], "scanner": card.scanner, "profile": prof, "t": int(ctx.m.t[card.idx]),
                    "base": ob, "llm": ol, "llm_action": d.get("action") if d else None, "error": err,
                    "confidence": d.get("confidence") if d else None, "thesis": d.get("thesis") if d else None,
                    "risk_manager": d.get("risk_manager") if d else None, "latency_s": round(time.time() - t0, 1)}

        t0 = time.time()
        with ThreadPoolExecutor(a.workers) as ex:
            rows = list(ex.map(work, pool))
        with open(os.path.join(out, f"cards_{cid}.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        base = np.array([r["base"]["pnl_tmn"] for r in rows]); llm = np.array([r["llm"]["pnl_tmn"] for r in rows])
        enter = np.array([r["llm_action"] == "enter" for r in rows])
        diff = llm - base
        rng = np.random.default_rng(1)
        shuf = []
        for _ in range(1000):
            pe = rng.permutation(enter)
            shuf.append(np.where(pe, base, 0).mean())       # random selection of the same count, baseline params
        sel_base = np.where(enter, base, 0).mean()
        conf = [(r["confidence"], r["llm"]["pnl_tmn"] > 0) for r in rows if r["llm_action"] == "enter" and r["confidence"] is not None]
        brier = float(np.mean([(c - w) ** 2 for c, w in conf])) if conf else None
        s = {"n_cards": len(rows), "errors": int(sum(r["error"] is not None for r in rows)),
             "llm_enter_rate": float(enter.mean()) if len(rows) else 0,
             "baseline_mean_pnl_tmn": float(base.mean()) if len(rows) else 0,
             "llm_mean_pnl_tmn": float(llm.mean()) if len(rows) else 0,
             "AI2_lift_per_card_tmn": float(diff.mean()) if len(rows) else 0,
             "AI2_ci95": bootstrap_ci(diff),
             "AI3_selection_vs_shuffle_tmn": float(sel_base - np.mean(shuf)) if len(rows) else 0,
             "AI3_p_value": float(np.mean(np.array(shuf) >= sel_base)) if len(rows) else None,
             "AI4_skip_precision": float(np.mean(base[~enter] < 0)) if (~enter).any() else None,
             "AI4_enter_precision": float(np.mean(llm[enter] > 0)) if enter.any() else None,
             "AI5_brier": brier, "avg_latency_s": float(np.mean([r["latency_s"] for r in rows])) if rows else 0,
             "minutes": round((time.time() - t0) / 60, 1)}
        summary[cid] = s
        print(cid, json.dumps(s))
    json.dump(summary, open(os.path.join(out, "cards_summary.json"), "w"), indent=1)
    print("saved", out)


if __name__ == "__main__":
    main()
