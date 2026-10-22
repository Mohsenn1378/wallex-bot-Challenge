"""Metrics aligned with the 300-point rubric (see docs/BACKTEST_PLAN.md §7)."""
from __future__ import annotations
import math
import numpy as np


def compute(acct, start_j: int, end_j: int, t: np.ndarray) -> dict:
    eq = np.asarray(acct.equity, dtype=float)
    E0 = acct.start_cash
    trades = acct.trades
    days = max((end_j - start_j) / 1440, 1e-9)
    ret = (eq[-1] - E0) / E0 if len(eq) else 0.0
    peak = np.maximum.accumulate(eq) if len(eq) else np.array([E0])
    dd_peak = float(np.max((peak - eq) / peak)) if len(eq) else 0.0
    dd_start = float(max(0.0, (E0 - eq.min()) / E0)) if len(eq) else 0.0
    ulcer = float(np.sqrt(np.mean(((peak - eq) / peak) ** 2))) if len(eq) else 0.0
    pnl = np.array([x.pnl for x in trades]) if trades else np.array([])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win = wins.sum() if len(wins) else 0.0
    gross_loss = -losses.sum() if len(losses) else 0.0
    fees = sum(x.fees for x in trades)
    hours = np.unique([(x.entry_t // 3600) for x in trades]) if trades else []
    n_hours = max(int((end_j - start_j) / 60), 1)
    hourly = np.zeros(n_hours)
    for x in trades:
        h = int((x.entry_idx - start_j) // 60)
        if 0 <= h < n_hours:
            hourly[h] += 1
    gaps = 0
    if trades:
        ts = sorted([x.entry_idx for x in trades])
        gaps = max([ts[0] - start_j] + [b - a for a, b in zip(ts, ts[1:])] + [end_j - ts[-1]]) / 60
    else:
        gaps = (end_j - start_j) / 60
    acc = [d for d in acct.decisions if d.get("result") == "accepted"]
    reasons = {}
    for x in trades:
        reasons[x.exit_reason] = reasons.get(x.exit_reason, 0) + 1
    rej = {}
    for d in acct.decisions:
        if d.get("result") == "rejected":
            rej[d["why"]] = rej.get(d["why"], 0) + 1
    hr = eq[::60]
    r_h = np.diff(hr) / hr[:-1] if len(hr) > 2 else np.array([0.0])
    sharpe_h = float(np.mean(r_h) / np.std(r_h) * math.sqrt(24 * 365)) if np.std(r_h) > 0 else 0.0
    max_pos_pct = max([x.notional / E0 for x in trades], default=0.0)
    return {
        "R1_net_return_pct": round(ret * 100, 3),
        "R3_profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "R4_expectancy_tmn": round(float(pnl.mean()), 1) if len(pnl) else 0.0,
        "R4_expectancy_R": round(float(np.mean([x.r_mult for x in trades])), 3) if trades else 0.0,
        "R5_fee_ratio": round(fees / gross_win, 3) if gross_win > 0 else None,
        "win_rate_pct": round(100 * len(wins) / len(pnl), 1) if len(pnl) else 0.0,
        "trades": len(trades),
        "D1_maxdd_peak_pct": round(dd_peak * 100, 3),
        "D2_dd_from_start_pct": round(dd_start * 100, 3),
        "D3_ulcer": round(ulcer * 100, 3),
        "A1_trades_per_day": round(len(trades) / days, 2),
        "A2_hour_coverage_pct": round(100 * float(np.mean(hourly > 0)), 1),
        "A3_max_gap_h": round(gaps, 1),
        "A4_regularity": round(1 - float(np.std(hourly) / np.mean(hourly)), 3) if hourly.mean() > 0 else 0.0,
        "S1_order_error_rate": round(sum(1 for e in acct.errors if e["type"] == "order_rejected") / max(len(acc), 1), 4),
        "S2_policy_error_rate": round(sum(1 for e in acct.errors if e["type"] == "policy") / max(len(acct.decisions), 1), 4),
        "K1_sl_coverage_pct": 100.0 if all(x.sl_pct > 0 for x in trades) else 0.0,
        "K2_max_pos_pct": round(max_pos_pct * 100, 2),
        "K2_max_exposure_pct": round(100 * max((float(e) for e in []), default=0.0), 2),
        "sharpe_hourly_ann": round(sharpe_h, 2),
        "avg_hold_min": round(float(np.mean([(x.exit_t - x.entry_t) / 60 for x in trades])), 1) if trades else 0.0,
        "fees_tmn": round(fees, 0),
        "exit_reasons": reasons,
        "rejections": rej,
        "cards_seen": len(acct.decisions),
    }


def jsp(mx: dict, r2_median: float | None = None, ai: dict | None = None) -> dict:
    """Judge-Score Proxy (0-300). Targets are our own normalisation guesses (6%/2d, 8% dd)."""
    r = r2_median if r2_median is not None else mx["R1_net_return_pct"]
    p_ret = 90 * min(max(r / 6.0, 0), 1) if r > 0 else 0
    p_dd = 30 * min(max(1 - mx["D2_dd_from_start_pct"] / 8.0, 0), 1)
    p_act = 15 * (0.5 * min(mx["A1_trades_per_day"] / 12, 1) + 0.5 * mx["A2_hour_coverage_pct"] / 100)
    p_stab = 15 * min(max(1 - 10 * mx["S1_order_error_rate"] - 5 * mx["S2_policy_error_rate"], 0), 1)
    p_risk = 30 * (0.4 * (mx["K1_sl_coverage_pct"] / 100) + 0.3 * (mx["K2_max_pos_pct"] <= 8) + 0.3)
    p_ai = 35 * (ai or {}).get("score", 0)
    p_bt, p_repro, p_logic = 20 * 0.5, 25, 40 * 1.0
    parts = {"return": p_ret, "drawdown": p_dd, "activity": p_act, "stability": p_stab, "risk": p_risk,
             "ai": p_ai, "logic": p_logic, "repro": p_repro, "bt_shadow": p_bt}
    parts = {k: round(v, 1) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 1)
    return parts
