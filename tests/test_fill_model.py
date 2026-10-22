"""Golden scenarios for the fill model. Run: python -m pytest -q tests  (or python tests/test_fill_model.py)"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.data.store import Market
from wallex_agent.sim.fill_model import Fills, MAKER, TAKER, SLIP
from wallex_agent.sim.position import Plan, open_position

P = 100_000.0


def mk(bars, vol=None, step_dec=8, spread=0.0):
    """bars: list of (o,h,l,c); vol: list of volumes (0 = no trade)"""
    a = np.array(bars, dtype=float).T[:, None, :]
    v = np.array(vol if vol is not None else [1.0] * len(bars), dtype=float)[None, :]
    t = np.arange(len(bars)) * 60
    m = Market(["X"], t, a[0], a[1], a[2], a[3], v, v * a[3], v > 0,
               {"X": {"step_dec": step_dec, "min_notional": 50000, "spread_pct": spread}})
    return m, Fills(m, spread_mult=1.0)


def test_market_entry_costs():
    m, f = mk([(P, P, P, P)] * 3, spread=0.2)
    assert abs(f.market_buy_px(0, 1) - P * (1 + 0.001 + SLIP)) < 1e-6


def test_stale_penalty_grows():
    m, f = mk([(P, P, P, P)] * 6, vol=[1, 0, 0, 0, 0, 1])
    assert f.market_buy_px(0, 4) > f.market_buy_px(0, 1)


def test_stop_gap_fills_at_open():
    m, f = mk([(P, P, P, P), (P, P, P, P), (95_000, 96_000, 94_000, 95_500)])
    pos = open_position(f, 0, 1, 100_000, Plan(sl_pct=2, tps=[(5, 1)]))
    pos.step(f, 2)
    assert not pos.open and pos.exits[0][3] == "SL" and pos.exits[0][1] <= 95_000


def test_sl_before_tp_same_bar():
    m, f = mk([(P, P, P, P), (P, 110_000, 90_000, P)])
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=2, tps=[(3, 1)]))
    pos.step(f, 1)
    assert pos.exits[0][3] == "SL"


def test_tp_needs_trade_through():
    m, f = mk([(P, P, P, P), (P, 103_000, P, P), (P, 103_100, P, P)])
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=2, tps=[(3, 1)]))
    tp = pos.entry_px * 1.03
    pos.step(f, 1)
    assert pos.open or m.h[0, 1] > tp      # touching is not a fill
    pos.step(f, 2)


def test_no_fill_on_zero_volume_bar():
    m, f = mk([(P, P, P, P), (P, P, 90_000, P)], vol=[1, 0])
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=2, tps=[(3, 1)]))
    pos.step(f, 1)
    assert pos.open                         # stop cannot trigger without a real trade


def test_min_notional_entry_rejected():
    m, f = mk([(P, P, P, P)] * 2)
    assert open_position(f, 0, 0, 50_000, Plan(sl_pct=2, tps=[])) is None


def test_step_rounding_coarse():
    m, f = mk([(366_000.0,) * 4] * 2, step_dec=1)   # XRP-like: 0.1 coin = 36,600 TMN
    pos = open_position(f, 0, 0, 80_000, Plan(sl_pct=2, tps=[]))
    assert pos is not None and abs(pos.qty - 0.2) < 1e-9


def test_partial_tp_merges_when_too_small():
    m, f = mk([(P, P, P, P), (P, 104_000, P, P)])
    pos = open_position(f, 0, 0, 90_000, Plan(sl_pct=2, tps=[(3, 0.5), (6, 0.5)]))
    pos.step(f, 1)
    assert not pos.open                     # 45k halves are below min notional -> exit all


def test_partial_tp_splits_when_large():
    m, f = mk([(P, P, P, P), (P, 104_000, P, P)])
    pos = open_position(f, 0, 0, 150_000, Plan(sl_pct=2, tps=[(3, 0.5), (6, 0.5)]))
    pos.step(f, 1)
    assert pos.open and pos.tp_done == 1 and pos.stop > pos.entry_px


def test_time_stop():
    m, f = mk([(P, P, P, P)] * 70)
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=2, tps=[(3, 1)], time_stop_min=60))
    for j in range(1, 69):
        pos.step(f, j)
    assert not pos.open and pos.exits[0][3] == "TIME"


def test_break_even():
    m, f = mk([(P, P, P, P), (P, 100_800, P, 100_700), (P, P, 99_000, P)])
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=1, tps=[(3, 1)], be_trigger_pct=0.6))
    pos.step(f, 1)
    assert pos.stop > pos.entry_px
    pos.step(f, 2)
    assert not pos.open


def test_stuck_guard():
    m, f = mk([(P, P, P, P), (P, P, P, 90_000), (90_000,) * 4])
    pos = open_position(f, 0, 0, 56_000, Plan(sl_pct=20, tps=[]))
    pos.step(f, 1)
    assert not pos.open and pos.exits[-1][3] == "STUCK_GUARD"


def test_fees_charged_both_sides():
    m, f = mk([(P, P, P, P), (P, P, P, P)])
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=2, tps=[]))
    pos.exit_market(f, 1, "END")
    assert pos.pnl() < -0.005 * 100_000      # at least 0.6% round trip + slippage


def test_pnl_positive_on_tp():
    m, f = mk([(P, P, P, P), (P, 104_000, P, P)])
    pos = open_position(f, 0, 0, 100_000, Plan(sl_pct=2, tps=[(3, 1)]))
    pos.step(f, 1)
    assert pos.pnl() > 0.02 * 100_000


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except AssertionError as e:
            bad += 1; print("FAIL", fn.__name__, e)
    print(f"{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
