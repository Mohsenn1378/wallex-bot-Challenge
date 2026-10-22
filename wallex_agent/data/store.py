"""Load candles into one aligned minute grid (numpy arrays) for all symbols."""
from __future__ import annotations
import csv, glob, json, os
from dataclasses import dataclass
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data", "candles")
TEHRAN_OFFSET_S = 3 * 3600 + 1800


@dataclass
class Market:
    """All symbols on a shared 1-minute grid. Arrays are [n_symbols, T]."""
    symbols: list
    t: np.ndarray            # unix seconds, shape [T]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray            # base volume
    qv: np.ndarray           # quote volume (TMN or USDT) ~ v * c
    tradable: np.ndarray     # bool: real bar with volume > 0 (fills allowed)
    rules: dict              # symbol -> {step_dec, min_notional, spread_pct}

    def idx(self, sym):
        return self.symbols.index(sym)

    @property
    def T(self):
        return len(self.t)


def load_rules() -> dict:
    p = os.path.join(ROOT, "research", "combos", "market_rules_snapshot.csv")
    out = {}
    for r in csv.DictReader(open(p, encoding="utf-8")):
        out[r["k"]] = {"step_dec": int(r["sd"]), "min_notional": float(r["mn"]),
                       "spread_pct": float(r["sp"]), "price": float(r["p"])}
    return out


def _load_symbol(sym: str, t0: int | None, t1: int | None) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(DATA, sym, "*.csv")))
    if not files:
        raise FileNotFoundError(f"no data for {sym}; run: python -m wallex_agent.data.fetch")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df = df.drop_duplicates("t").sort_values("t")
    if t0 is not None:
        df = df[df.t >= t0]
    if t1 is not None:
        df = df[df.t < t1]
    return df


def build_market(frames: dict, lo: int | None = None, hi: int | None = None) -> Market:
    """frames: symbol -> DataFrame(t,o,h,l,c,v). lo/hi fix the grid (needed for stable indices in live mode)."""
    symbols = list(frames)
    lo = lo if lo is not None else min(int(f.t.min()) for f in frames.values())
    hi = hi if hi is not None else max(int(f.t.max()) for f in frames.values())
    lo -= lo % 60
    grid = np.arange(lo, hi + 60, 60, dtype=np.int64)
    S, T = len(symbols), len(grid)
    O, H, L, C, V = (np.full((S, T), np.nan) for _ in range(5))
    for i, s in enumerate(symbols):
        f = frames[s]
        f = f[(f.t >= lo) & (f.t <= hi)]
        pos = ((f.t.values.astype(np.int64) - lo) // 60).astype(int)
        O[i, pos], H[i, pos], L[i, pos], C[i, pos], V[i, pos] = f.o, f.h, f.l, f.c, f.v
    real = ~np.isnan(C)
    C = pd.DataFrame(C.T).ffill().bfill().values.T
    for A in (O, H, L):
        A[~real] = C[~real]
    V = np.nan_to_num(V, nan=0.0)
    tradable = real & (V > 0)
    rules = load_rules()
    return Market(symbols, grid, O, H, L, C, V, V * C, tradable, {s: rules.get(s, {}) for s in symbols})


def load_frames(symbols, t0=None, t1=None) -> dict:
    return {s: _load_symbol(s, t0, t1) for s in symbols}


def load_market(symbols: list[str], days: int | None = None, end_ts: int | None = None,
                start_ts: int | None = None) -> Market:
    t1 = end_ts
    t0 = start_ts if start_ts else ((t1 or int(pd.Timestamp.utcnow().timestamp())) - days * 86400 if days else None)
    return build_market(load_frames(symbols, t0, t1))


def tehran_day(t: np.ndarray) -> np.ndarray:
    """Integer day id where days start at 00:00 Tehran."""
    return (t + TEHRAN_OFFSET_S) // 86400
