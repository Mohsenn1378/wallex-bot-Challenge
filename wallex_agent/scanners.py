"""
Scanners M1..M8 + regime. They only CREATE opportunity cards; they never trade.
A card fires on the close of a higher-timeframe bar and is usable at 1m index `idx`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
from .features import Features, shift, rmin, rmax, sma

QUOTE_EXCLUDE = {"BTCUSDT", "USDTTMN"}          # reference series, never traded


@dataclass
class Card:
    idx: int            # 1m index when the card becomes known (bar close)
    sym: int
    scanner: str
    tf: int
    price: float        # close of the signal bar
    stop_hint: float    # structural stop suggested by the scanner
    tp_hint: float      # natural target (nan if none)
    info: dict = field(default_factory=dict)


def _cards(F: Features, k: int, mask: np.ndarray, name: str, stop: np.ndarray, tp: np.ndarray | None = None, info=None):
    tf = F.tf[k]
    out = []
    S, B = np.nonzero(np.nan_to_num(mask, nan=0).astype(bool))
    for s, b in zip(S, B):
        sym = F.m.symbols[s]
        if sym in QUOTE_EXCLUDE:
            continue
        out.append(Card(int(tf.end_idx[b]), int(s), name, k, float(tf.c[s, b]),
                        float(stop[s, b]), float(tp[s, b]) if tp is not None else float("nan"),
                        {key: float(v[s, b]) for key, v in (info or {}).items()}))
    return out


def _active(F, k):
    """bar had real trades in it (avoid signals on frozen prices)."""
    return F.tf[k].trades > 0


def m1_bb_dip(F, d=0.005, rsi_th=30):
    k = 5; tf, x = F.tf[k], F.x[k]
    mask = (tf.c < x["bb_lo"] * (1 - d)) & (x["rsi"] < rsi_th) & (tf.c < x["ema50"]) & \
           (tf.v < 20 * np.nan_to_num(x["vavg20"], nan=np.inf)) & _active(F, k)
    stop = tf.l - 0.5 * x["atr"]
    return _cards(F, k, mask, "M1", stop, x["bb_mid"], {"rsi": x["rsi"]})


def m2_vwap(F, z=2.0):
    k = 5; tf, x = F.tf[k], F.x[k]
    lower = x["vwap"] - z * x["vwap_sd"]
    prev_below = shift(tf.c) < shift(lower)
    reclaim = (tf.c > shift(tf.h)) & (tf.c > tf.o)
    mask = prev_below & reclaim & (x["vwap_sd"] > 0) & _active(F, k)
    stop = rmin(tf.l, 3) * (1 - 0.0015)
    tp = x["vwap"] - 1.0 * x["vwap_sd"]
    return _cards(F, k, mask, "M2", stop, np.maximum(tp, tf.c * 1.009), {"vwap": x["vwap"]})


def m3_vol_breakout(F, k_mult=0.5, vol_mult=1.5):
    k = 5; tf, x = F.tf[k], F.x[k]
    sess = (tf.t_end // (4 * 3600))                      # 4h sessions (UTC)
    S, n = tf.c.shape
    level = np.full((S, n), np.nan); prev_rng = np.full((S, n), np.nan)
    for s in range(S):
        cur_open, prev_hi, prev_lo, hi, lo, last = np.nan, np.nan, np.nan, -np.inf, np.inf, None
        for b in range(n):
            if sess[b] != last:
                if last is not None:
                    prev_hi, prev_lo = hi, lo
                cur_open, hi, lo, last = tf.o[s, b], -np.inf, np.inf, sess[b]
            if np.isfinite(prev_hi):
                prev_rng[s, b] = prev_hi - prev_lo
                level[s, b] = cur_open + k_mult * (prev_hi - prev_lo)
            hi, lo = max(hi, tf.h[s, b]), min(lo, tf.l[s, b])
    cross = (tf.c > level) & (shift(tf.c) <= shift(level))
    mask = cross & (tf.c > x["ema200"]) & (tf.v > vol_mult * np.nan_to_num(x["vavg20"], nan=np.inf)) & _active(F, k)
    stop = level - 0.5 * prev_rng
    return _cards(F, k, mask, "M3", stop, None, {"range_pct": prev_rng / tf.c * 100})


def m4_sweep(F, L=10, vz=1.5):
    k = 15; tf, x = F.tf[k], F.x[k]
    swing = shift(rmin(tf.l, L))
    mask = (tf.l < swing) & (tf.c > swing) & (np.nan_to_num(x["vz"]) > vz) & _active(F, k)
    stop = tf.l - 0.2 * x["atr"]
    return _cards(F, k, mask, "M4", stop, shift(rmax(tf.h, L)), {"swing": swing})


def m5_pullback(F):
    k = 5; tf, x = F.tf[k], F.x[k]
    h1 = F.x[60]
    # 1h trend mapped to 5m bars: use the last COMPLETED 1h bar
    idx60 = np.searchsorted(F.tf[60].end_idx, tf.end_idx, side="right") - 1
    up1h = np.zeros_like(tf.c, dtype=bool)
    ok = idx60 >= 0
    c60, e60 = F.tf[60].c, h1["ema50"]
    up1h[:, ok] = c60[:, idx60[ok]] > e60[:, idx60[ok]]
    cross = (x["macd"] > x["macd_sig"]) & (shift(x["macd"]) <= shift(x["macd_sig"]))
    mask = (tf.c > x["ema200"]) & (x["ema8"] > x["ema21"]) & (rmin(x["rsi"], 8) < 41) & cross & up1h & _active(F, k)
    stop = np.minimum(rmin(tf.l, 6), tf.c - 1.5 * x["atr"])
    return _cards(F, k, mask, "M5", stop)


def m6_squeeze(F, bars=6, vol_mult=1.5):
    k = 15; tf, x = F.tf[k], F.x[k]
    inside = (x["bb_up"] < x["kc_up"]) & (x["bb_lo"] > x["kc_lo"])
    was = sma(inside.astype(float), bars) == 1.0
    mom = tf.c - (rmax(tf.h, 20) + rmin(tf.l, 20)) / 2
    mask = (shift(was.astype(float)) == 1) & ~inside & (mom > 0) & (mom > shift(mom)) & \
           (tf.v > vol_mult * np.nan_to_num(x["vavg20"], nan=np.inf)) & _active(F, k)
    stop = (x["kc_up"] + x["kc_lo"]) / 2
    return _cards(F, k, mask, "M6", stop)


def m7_vol_spike(F, z=3.0):
    k = 5; tf, x = F.tf[k], F.x[k]
    rng = tf.h - tf.l
    mask = (np.nan_to_num(x["vz"]) >= z) & ((tf.c - tf.l) >= 0.67 * rng) & (tf.l <= rmin(tf.l, 12)) & (rng > 0) & _active(F, k)
    return _cards(F, k, mask, "M7", tf.l * (1 - 0.001), x["vwap"] if "vwap" in x else None)


def m8_rel_strength(F, top=3):
    k = 15; tf, x = F.tf[k], F.x[k]
    syms = F.m.symbols
    b = syms.index("BTCUSDT") if "BTCUSDT" in syms else syms.index("BTCTMN")
    r1 = tf.c / shift(tf.c, 4) - 1; r4 = tf.c / shift(tf.c, 16) - 1
    rel = (r1 - r1[b]) + (r4 - r4[b])
    for q in QUOTE_EXCLUDE:
        if q in syms:
            rel[syms.index(q)] = np.nan
    rank = np.argsort(np.argsort(-np.nan_to_num(rel, nan=-9), axis=0), axis=0)
    pull = (tf.l <= x["ema21"]) & (tf.c > x["ema21"])
    mask = (rank < top) & pull & (rel > 0) & _active(F, k)
    return _cards(F, k, mask, "M8", tf.c - 1.5 * x["atr"], None, {"rel": rel})


SCANNERS = {"M1": m1_bb_dip, "M2": m2_vwap, "M3": m3_vol_breakout, "M4": m4_sweep,
            "M5": m5_pullback, "M6": m6_squeeze, "M7": m7_vol_spike, "M8": m8_rel_strength}


class Regime:
    """Per-coin regime on 15m + market regime from BTCUSDT (pure crypto) and USDTTMN (rial)."""
    TREND, RANGE, DOWN, DEAD = "trend_up", "range", "down", "dead"

    def __init__(self, F: Features):
        self.F = F
        tf, x = F.tf[15], F.x[15]
        self.end_idx = tf.end_idx
        slope = x["ema50"] / shift(x["ema50"], 4) - 1
        S, n = tf.c.shape
        lab = np.full((S, n), self.RANGE, dtype=object)
        lab[(x["adx"] > 22) & (tf.c > x["ema50"]) & (slope > 0)] = self.TREND
        lab[(tf.c < x["ema50"]) & (slope < 0) & (x["adx"] > 22)] = self.DOWN
        lab[np.nan_to_num(x["liq"]) < 0.05] = self.DEAD
        self.lab = lab
        syms = F.m.symbols
        bi = syms.index("BTCUSDT")
        c60, e60 = F.tf[60].c[bi], F.x[60]["ema50"][bi]
        self.btc_bull_60 = c60 > e60
        self.btc_ret15 = tf.c[bi] / shift(tf.c, 1)[bi] - 1
        self.atr_pct = x["atr"] / tf.c

    def at(self, idx: int, sym: int):
        b = np.searchsorted(self.end_idx, idx, side="right") - 1
        b60 = np.searchsorted(self.F.tf[60].end_idx, idx, side="right") - 1
        if b < 0:
            return {"coin": self.RANGE, "btc_bull": True, "btc_ret15": 0.0, "atr_pct": float("nan")}
        return {"coin": self.lab[sym, b], "btc_bull": bool(self.btc_bull_60[b60]) if b60 >= 0 else True,
                "btc_ret15": float(np.nan_to_num(self.btc_ret15[b])), "atr_pct": float(self.atr_pct[sym, b])}
