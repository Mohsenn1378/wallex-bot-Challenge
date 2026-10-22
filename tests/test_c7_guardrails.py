"""T4 — C7 engine guardrails. Run: python tests/test_c7_guardrails.py  (or python -m pytest -q tests/test_c7_guardrails.py)"""
import json, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.data.store import Market
from wallex_agent.sim.fill_model import Fills
from wallex_agent.sim.position import Plan, Position
from wallex_agent.scanners import Card
from wallex_agent.engine import Account
from wallex_agent.runtime import parse_tehran

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMS = ["BTCTMN", "PEPETMN", "ETHTMN", "SOLTMN", "BTCUSDT"]
P = 100_000.0
T = 1500
T0 = parse_tehran("2026-10-23 00:00")            # bar 900 = 15:00, bar 1000 = 16:40 Tehran (not night)


class F15:
    def __init__(self):
        S = len(SYMS)
        self.x = {15: {"atr": np.full((S, T // 15), np.nan), "liq": np.full((S, T // 15), 0.5)}}
        self.tf = {15: type("TF", (), {"end_idx": np.arange(14, T, 15)})()}


class Reg:
    def __init__(self):
        self.btc = 0.0

    def at(self, j, s):
        return {"coin": "range", "btc_bull": True, "btc_ret15": self.btc, "atr_pct": 0.01}


class Pol:
    name = "stub"

    def __init__(self, dec=None):
        self.dec, self.calls = dec, 0

    def decide(self, card, ctx):
        self.calls += 1
        self.ctx = ctx
        return dict(self.dec)


class Ctl:
    def __init__(self, **kw):
        self.pause_entries, self.size_mult, self.max_positions = False, 1.0, 6
        self.__dict__.update(kw)


ENTER = {"action": "enter", "size_tier": "high", "sl_pct": 5.0, "tp_plan": [{"pct": 6.0, "portion": 1.0}],
         "time_stop_min": 1440, "thesis": "test"}


def mk(dec=ENTER, t0=T0, start_cash=5_000_000):
    t = (t0 + np.arange(T) * 60).astype(np.int64)
    a = np.full((len(SYMS), T), P)
    v = np.ones((len(SYMS), T))
    m = Market(SYMS, t, a.copy(), a.copy(), a.copy(), a.copy(), v, v * P, v > 0,
               {s: {"step_dec": 8, "min_notional": 50_000, "spread_pct": 0.2} for s in SYMS})
    pol = Pol(dec)
    acc = Account("C7", pol, m, Fills(m), F15(), Reg(), start_cash=start_cash)
    return acc, pol


def card(acc, sym="PEPETMN", j=1000):
    s = acc.m.symbols.index(sym)
    return Card(j, s, "M1", 15, float(acc.m.c[s, j]), float(acc.m.c[s, j] * 0.95), float("nan"), {})


def last(acc):
    return acc.decisions[-1]


def armed(acc, sym="PEPETMN", j=900):
    """Give the coin an earlier M1 signal so the next one triggers."""
    acc.on_card(card(acc, sym, j), j)


# (1) arming
def test_first_signal_only_arms_second_triggers():
    acc, pol = mk()
    acc.on_card(card(acc, j=900), 900)
    assert last(acc)["why"] == "armed_first_signal" and pol.calls == 0
    acc.on_card(card(acc, j=1000), 1000)
    assert pol.calls == 1 and last(acc)["result"] == "accepted"
    assert pol.ctx["m1_prev_6h"] == 1 and "dd6_pct" in pol.ctx and pol.ctx["gov_state"] == "normal"


def test_same_bar_duplicate_not_counted_twice():
    acc, pol = mk()
    acc.on_card(card(acc, j=900), 900)
    acc.on_card(card(acc, j=900), 900)
    assert [d["why"] for d in acc.decisions] == ["armed_first_signal"] * 2 and pol.calls == 0


def test_signal_outside_window_does_not_count():
    acc, pol = mk()
    acc.on_card(card(acc, j=500), 500)
    acc.on_card(card(acc, j=1000), 1000)                 # 500 bars later > 360
    assert last(acc)["why"] == "armed_first_signal" and pol.calls == 0


# (2) deep drop triggers without an earlier signal
def test_deep_drop_triggers_first_signal():
    acc, pol = mk()
    s = acc.m.symbols.index("PEPETMN")
    acc.m.h[s, 800] = P / 0.91                           # close is 9% under the 6h high
    acc.on_card(card(acc, j=1000), 1000)
    assert pol.calls == 1 and last(acc)["result"] == "accepted"
    assert pol.ctx["dd6_pct"] <= -8.0


# (3) BTC falling
def test_btc_falling_blocks():
    acc, pol = mk()
    armed(acc)
    acc.R.btc = -0.006
    acc.on_card(card(acc), 1000)
    assert last(acc)["why"] == "btc_falling" and pol.calls == 0


# (4) risk cap on the effective distance
def test_high_tier_risk_cap():
    acc, _ = mk()
    (plan, size), why = acc.guard(card(acc), dict(ENTER), 1000, "C7")
    assert why is None and abs(size - 0.003 * 5e6 / 0.071) < 1 and 210_000 < size < 212_000
    acc.equity_fn = lambda: 4_000_000
    (plan, size), why = acc.guard(card(acc), dict(ENTER), 1000, "C7")
    assert abs(size - 0.003 * 4e6 / 0.071) < 1 and 168_000 < size < 170_000
    rec = acc._guard_info
    assert rec["tier_requested"] == "high" and rec["tier_final"] == "high" and rec["risk_tmn"] == round(size * 0.071)


def test_missing_tier_means_standard_and_decision_record():
    acc, _ = mk(dict(ENTER, size_tier=None))
    armed(acc)
    acc.on_card(card(acc), 1000)
    d = last(acc)
    assert d["result"] == "accepted" and d["tier_requested"] == "standard" and d["size"] == 150_000


# (5) night cap and late-window probe
def test_night_caps_tier_and_late_window_forces_probe():
    acc, _ = mk(t0=parse_tehran("2026-10-23 02:00"))
    (_, size), _ = acc.guard(card(acc, j=10), dict(ENTER), 10, "C7")
    assert size == 150_000 and acc._guard_info["tier_final"] == "standard"
    acc, _ = mk()
    acc.window = {"probe_only_after_ts": float(acc.m.t[1000]) - 60}
    (_, size), _ = acc.guard(card(acc), dict(ENTER), 1000, "C7")
    assert size == 65_000 and acc._guard_info["tier_final"] == "probe"


# (6) portfolio heat
def test_heat_cap():
    acc, _ = mk()
    s = acc.m.symbols.index("ETHTMN")
    acc.positions[s] = Position(s, 10.0, P, 900, Plan(sl_pct=5.0, tps=[(6.0, 1.0)]), P * 0.95)   # 1.0M -> heat 71k
    acc.cash -= 10.0 * P                                                                         # equity stays 5M
    res, why = acc.guard(card(acc), dict(ENTER), 1000, "C7")
    assert res is None and why == "heat_cap"


# (7) drawdown governor
def test_governor_soft_hard_resume():
    acc, _ = mk()
    eq = {"v": 5_000_000}
    acc.equity_fn = lambda: eq["v"]
    eq["v"] = 4_900_000                                   # -2%
    acc.on_bar(1000)
    assert acc.gov_state == "soft"
    (_, size), _ = acc.guard(card(acc), dict(ENTER, size_tier="standard"), 1000, "C7")
    assert size == 75_000
    (_, size), _ = acc.guard(card(acc), dict(ENTER, size_tier="probe"), 1000, "C7")
    assert size == 65_000                                 # floor, never below the minimum
    eq["v"] = 4_800_000                                   # -4%
    acc.on_bar(1001)
    assert acc.gov_state == "hard"
    assert acc.guard(card(acc), dict(ENTER), 1001, "C7") == (None, "dd_governor")
    eq["v"] = 4_870_000                                   # -2.6% < 3% resume
    acc.on_bar(1002)
    assert acc.gov_state == "soft"
    eq["v"] = 4_960_000                                   # -0.8%
    acc.on_bar(1003)
    assert acc.gov_state == "normal"
    assert [d["to"] for d in acc.decisions if d.get("result") == "governor"] == ["soft", "hard", "soft", "normal"]


# (8) end of window
def test_too_close_to_end():
    acc, _ = mk()
    acc.window = {"flatten_start_ts": float(acc.m.t[1000]) + 64 * 60}
    assert acc.guard(card(acc), dict(ENTER), 1000, "C7") == (None, "too_close_to_end")
    acc.window = {"flatten_start_ts": float(acc.m.t[1000]) + 200 * 60}
    (plan, _), why = acc.guard(card(acc), dict(ENTER), 1000, "C7")
    assert why is None and plan.time_stop_min == 195


def test_window_closed_and_control():
    acc, pol = mk()
    armed(acc)
    acc.window = {"no_new_entries_after_ts": float(acc.m.t[1000])}
    acc.on_card(card(acc), 1000)
    assert last(acc)["why"] == "window_closed"
    acc.window = None
    acc.control = Ctl(pause_entries=True)
    acc.on_card(card(acc), 1000)
    assert last(acc)["why"] == "paused_by_control"
    acc.control = Ctl(max_positions=0)
    acc.on_card(card(acc), 1000)
    assert last(acc)["why"] == "max_positions"
    acc.control, acc.flatten_mode = None, True
    acc.on_card(card(acc), 1000)
    assert last(acc)["why"] == "flatten_mode" and pol.calls == 0


# (9) plan: single TP inside the band, no BE, no trailing
def test_plan_has_no_be_or_trailing():
    acc, _ = mk(dict(ENTER, tp_plan=[{"pct": 9.0, "portion": 0.5}, {"pct": 12.0, "portion": 0.5}], sl_pct=3.0))
    (plan, _), _ = acc.guard(card(acc), dict(acc.policy.dec), 1000, "C7")
    assert plan.tps == [(7.0, 1.0)] and plan.sl_pct == 4.5
    assert plan.be_trigger_pct == 0 and plan.trail_atr_mult == 0 and plan.time_stop_min == 1440


# (10) disabled coin
def test_coin_disabled():
    acc, pol = mk()
    armed(acc)
    acc.disabled_coins.add("PEPETMN")
    acc.on_card(card(acc), 1000)
    assert last(acc)["why"] == "coin_disabled" and pol.calls == 0


# (11) a failing hook never re-sends the entry
class BadHooks:
    def __init__(self):
        self.opens, self.unknown = 0, []

    def on_open(self, acc, pos, j):
        self.opens += 1
        raise RuntimeError("network down")

    def on_close(self, acc, pos, j):
        pass

    def mark_unknown(self, sym):
        self.unknown.append(sym)


def test_hook_exception_no_second_order():
    acc, _ = mk()
    acc.hooks = h = BadHooks()
    armed(acc)
    acc.on_card(card(acc), 1000)
    acc.on_bar(1001)
    acc.on_bar(1002)
    acc.on_bar(1003)
    assert h.opens == 1 and acc.pending == [] and h.unknown == ["PEPETMN"]
    assert any(e["type"] == "hook_error" for e in acc.errors) and len(acc.positions) == 1


def test_hook_order_pending_cleared_before_on_open():
    seen = {}

    class H(BadHooks):
        def on_open(self, acc, pos, j):
            seen["pending"] = list(acc.pending)
            seen["entries"] = len(acc.entries_hist)
    acc, _ = mk()
    acc.hooks = H()
    armed(acc)
    acc.on_card(card(acc), 1000)
    acc.on_bar(1001)
    assert seen == {"pending": [], "entries": 1}


# live helpers used by the executor/runner
def test_cancel_sync_adopt_close_external():
    acc, _ = mk()
    s = acc.m.symbols.index("PEPETMN")
    armed(acc)
    acc.on_card(card(acc), 1000)
    acc.on_bar(1001)
    pos = acc.positions[s]
    acc.sync_position(s, 101_000.0, 1.5, 450.0)
    assert pos.entry_px == 101_000 and pos.qty == 1.5 and abs(pos.stop - 101_000 * 0.95) < 1e-6
    assert abs(acc.cash - (5_000_000 - 101_000 * 1.5 - 450)) < 1e-6
    acc.cancel_position(s, "live_entry_failed", 1001)
    assert s not in acc.positions and abs(acc.cash - 5_000_000) < 1e-6 and len(acc.entries_hist) == 0
    pos = acc.adopt_position(s, 1.0, P, 1002, Plan(sl_pct=5.0, tps=[(6.0, 1.0)], time_stop_min=600))
    assert acc.positions[s] is pos and last(acc)["result"] == "adopted"
    closed = []
    acc.hooks = type("H", (), {"on_open": lambda *a: None, "mark_unknown": lambda *a: None,
                               "on_close": lambda self, a, p, j: closed.append(p.exits[-1][3])})()
    acc.close_external(s, 1003, P * 0.95, "SERVER_STOP")
    assert s not in acc.positions and closed == ["SERVER_STOP"] and acc.trades[-1].exit_reason == "SERVER_STOP"


def test_flatten_mode_closes_paper_positions():
    acc, _ = mk()
    armed(acc)
    acc.on_card(card(acc), 1000)
    acc.on_bar(1001)
    acc.flatten_mode = True
    acc.on_bar(1002)
    assert not acc.positions and acc.trades[-1].exit_reason == "END"


# (12) C1..C6 unchanged (bit-for-bit vs golden hashes recorded on the pre-C7 engine)
def test_c1_c6_regression():
    golden = os.path.join(ROOT, "tests", "golden_c1_c6.json")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    try:
        import regress_c1_c6 as rg
        res = rg.run()
    except FileNotFoundError:
        print("  (skipped: no candle data in data/candles)")
        return
    assert res == json.load(open(golden))["hashes"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for t in tests:
        try:
            t(); ok += 1; print("PASS", t.__name__)
        except AssertionError as e:
            import traceback; traceback.print_exc()
            print("FAIL", t.__name__, e)
    print(f"{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
