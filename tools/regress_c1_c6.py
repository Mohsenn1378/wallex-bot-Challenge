"""
Bit-for-bit regression of the old combos (C1..C6, rule baselines) on a FIXED window of candles.
    python tools/regress_c1_c6.py                 # print hashes
    python tools/regress_c1_c6.py --write         # store them as tests/golden_c1_c6.json (only from known-good code)
Engine changes for C7 must leave every hash unchanged.
"""
import argparse, hashlib, json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.data.store import load_market
from wallex_agent.data.fetch import core_symbols, EXTRA
from wallex_agent.features import Features
from wallex_agent.scanners import SCANNERS, Regime
from wallex_agent.sim.fill_model import Fills
from wallex_agent.backtest import momentum_cards, run_account
from wallex_agent.combos import COMBOS
from wallex_agent.policies import BaselinePolicy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "golden_c1_c6.json")
START, END = 1790985600, 1792281600          # 2026-10-03 00:00 UTC .. 2026-10-18 00:00 UTC (15 days)
WARM = 200 * 60


class _Ctx:
    def __init__(self):
        self.m = load_market(core_symbols() + EXTRA, start_ts=START, end_ts=END)
        self.F = Features(self.m)
        self.R = Regime(self.F)
        self.f = Fills(self.m, spread_mult=1.5)
        self.cards = {k: fn(self.F) for k, fn in SCANNERS.items()}
        self.cards["MOM"] = momentum_cards(self.F)


def digest(acct):
    rows = [[t.sym, t.scanner, t.profile, t.entry_idx, round(t.entry_px, 8), round(t.qty, 10), t.exit_t,
             round(t.exit_px, 8), t.exit_reason, round(t.pnl, 4)] for t in acct.trades]
    dec = [[d.get("idx"), d.get("sym"), d.get("result"), d.get("why"), d.get("notional")] for d in acct.decisions]
    eq = [round(float(e), 4) for e in acct.equity]
    return {k: hashlib.sha256(json.dumps(v, default=str).encode()).hexdigest()[:16]
            for k, v in (("trades", rows), ("decisions", dec), ("equity", eq))} | {"n_trades": len(rows)}


def run():
    ctx = _Ctx()
    out = {}
    for cid in ("C1", "C2", "C3", "C4", "C5", "C6"):
        a = run_account(ctx, cid, BaselinePolicy(cid, COMBOS[cid]), WARM, ctx.m.T)
        out[cid] = digest(a)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    res = run()
    print(json.dumps(res, indent=1))
    if a.write:
        json.dump({"window": [START, END], "hashes": res}, open(GOLDEN, "w"), indent=1)
        print("written", GOLDEN)
