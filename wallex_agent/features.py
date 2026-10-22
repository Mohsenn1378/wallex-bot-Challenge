"""
Vectorised features on higher timeframes (5m/15m/60m) built from the 1m grid.
Every value at bar b is computed only from bars <= b, and becomes usable at the
1-minute index `end_idx[b]` (the last minute of that bar) -> no look-ahead.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .data.store import Market, tehran_day


class TF:
    def __init__(self, m: Market, k: int):
        self.k = k
        off = int(((-(m.t[0] // 60)) % k))           # first index aligned to k-minute UTC boundary
        n = (m.T - off) // k
        sl = slice(off, off + n * k)
        sh = (len(m.symbols), n, k)
        self.o = m.o[:, sl].reshape(sh)[:, :, 0]
        self.h = m.h[:, sl].reshape(sh).max(2)
        self.l = m.l[:, sl].reshape(sh).min(2)
        self.c = m.c[:, sl].reshape(sh)[:, :, -1]
        self.v = m.v[:, sl].reshape(sh).sum(2)
        self.qv = m.qv[:, sl].reshape(sh).sum(2)
        self.trades = m.tradable[:, sl].reshape(sh).sum(2)     # minutes with a trade inside the bar
        self.end_idx = off + np.arange(n) * k + (k - 1)         # 1m index where bar b is complete
        self.t_end = m.t[self.end_idx]
        self.n = n


def _df(a):  # [S, n] -> DataFrame (n x S) for pandas rolling
    return pd.DataFrame(a.T)


def ema(a, span):
    return _df(a).ewm(span=span, adjust=False, min_periods=span).mean().values.T


def sma(a, n):
    return _df(a).rolling(n, min_periods=n).mean().values.T


def rstd(a, n):
    return _df(a).rolling(n, min_periods=n).std(ddof=0).values.T


def rmin(a, n):
    return _df(a).rolling(n, min_periods=n).min().values.T


def rmax(a, n):
    return _df(a).rolling(n, min_periods=n).max().values.T


def shift(a, n=1):
    out = np.full_like(a, np.nan, dtype=float)
    if n > 0:
        out[:, n:] = a[:, :-n]
    else:
        out[:, :n] = a[:, -n:]
    return out


def rsi(c, n=14):
    d = np.diff(c, axis=1, prepend=c[:, :1])
    up = _df(np.clip(d, 0, None)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().values.T
    dn = _df(np.clip(-d, 0, None)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().values.T
    rs = up / np.where(dn == 0, np.nan, dn)
    out = 100 - 100 / (1 + rs)
    return np.where(dn == 0, np.where(up > 0, 100.0, 50.0), out)


def atr(h, l, c, n=14):
    pc = shift(c)
    tr = np.nanmax(np.stack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return _df(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().values.T


def adx(h, l, c, n=14):
    up, dn = h - shift(h), shift(l) - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(h, l, c, n)
    sm = lambda x: _df(x).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().values.T
    pdi = 100 * sm(pdm) / a
    ndi = 100 * sm(ndm) / a
    dx = 100 * np.abs(pdi - ndi) / np.where((pdi + ndi) == 0, np.nan, pdi + ndi)
    return sm(np.nan_to_num(dx)), pdi, ndi


def macd(c, f=12, s=26, sig=9):
    m = ema(c, f) - ema(c, s)
    return m, ema(np.nan_to_num(m), sig)


def zscore(a, n=20):
    mu, sd = sma(a, n), rstd(a, n)
    return (a - mu) / np.where(sd == 0, np.nan, sd)


def daily_vwap(tf: TF):
    """VWAP and its volume-weighted std, reset at 00:00 Tehran."""
    day = tehran_day(tf.t_end)
    tp = (tf.h + tf.l + tf.c) / 3
    vw = np.zeros_like(tp); sd = np.zeros_like(tp)
    for s in range(tp.shape[0]):
        df = pd.DataFrame({"d": day, "pv": tp[s] * tf.v[s], "v": tf.v[s], "p2v": tp[s] ** 2 * tf.v[s]})
        g = df.groupby("d")
        cv = g.v.cumsum().values
        cpv = g.pv.cumsum().values
        cp2v = g.p2v.cumsum().values
        with np.errstate(invalid="ignore", divide="ignore"):
            m = np.where(cv > 0, cpv / cv, tp[s])
            var = np.where(cv > 0, cp2v / cv - m ** 2, 0)
        vw[s], sd[s] = m, np.sqrt(np.clip(var, 0, None))
    return vw, sd


class Features:
    """Bundle of the timeframes + indicators the scanners need."""
    def __init__(self, m: Market):
        self.m = m
        self.tf = {k: TF(m, k) for k in (5, 15, 60)}
        f5, f15, f60 = self.tf[5], self.tf[15], self.tf[60]
        x = {}
        for k, f in self.tf.items():
            d = {}
            d["ema8"], d["ema21"], d["ema50"], d["ema200"] = ema(f.c, 8), ema(f.c, 21), ema(f.c, 50), ema(f.c, 200)
            d["rsi"] = rsi(f.c, 14)
            d["atr"] = atr(f.h, f.l, f.c, 14)
            d["bb_mid"] = sma(f.c, 20); sd = rstd(f.c, 20)
            d["bb_up"], d["bb_lo"] = d["bb_mid"] + 2 * sd, d["bb_mid"] - 2 * sd
            d["kc_up"], d["kc_lo"] = ema(f.c, 20) + 1.5 * d["atr"], ema(f.c, 20) - 1.5 * d["atr"]
            d["adx"], d["pdi"], d["ndi"] = adx(f.h, f.l, f.c, 14)
            d["macd"], d["macd_sig"] = macd(f.c)
            d["vz"] = zscore(f.v, 20)
            d["vavg20"] = sma(f.v, 20)
            d["liq"] = sma(f.trades / f.k, 96)       # share of minutes with trades, rolling
            x[k] = d
        x[5]["vwap"], x[5]["vwap_sd"] = daily_vwap(f5)
        self.x = x
