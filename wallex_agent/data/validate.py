"""Data quality report:  python -m wallex_agent.data.validate --days 35"""
import argparse, json, os
import numpy as np
from .store import load_market, ROOT
from .fetch import core_symbols, EXTRA


def report(m):
    out = {}
    for i, s in enumerate(m.symbols):
        real = m.tradable[i]
        r = np.diff(np.log(m.c[i]))
        atr_like = np.nanmedian(np.abs(r[r != 0])) if np.any(r != 0) else 0
        spikes = int(np.sum(np.abs(r) > 8 * max(atr_like, 1e-6) + 0.02))
        out[s] = {"bars": int(m.T), "tradable_pct": round(100 * real.mean(), 1),
                  "zero_vol_pct": round(100 * (1 - real.mean()), 1), "spikes": spikes,
                  "first": int(m.t[0]), "last": int(m.t[-1])}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=35); a = ap.parse_args()
    m = load_market(core_symbols() + EXTRA, days=a.days)
    rep = report(m)
    os.makedirs(os.path.join(ROOT, "runs"), exist_ok=True)
    json.dump(rep, open(os.path.join(ROOT, "runs", "data_quality.json"), "w"), indent=1)
    for s, r in sorted(rep.items(), key=lambda x: x[1]["tradable_pct"]):
        print(f"{s:10s} tradable {r['tradable_pct']:5.1f}%  spikes {r['spikes']}")
