"""T8 — resume after restart (P0-08). Real engine Account + LiveExecutor + FakeWallexClient (no network).
Run: python tests/test_resume.py   (or python -m pytest -q tests/test_resume.py)"""
import json, os, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wallex_agent.data.store import Market
from wallex_agent.sim.fill_model import Fills
from wallex_agent.scanners import Card
from wallex_agent.engine import Account
from wallex_agent.runtime import parse_tehran
from wallex_agent.live.executor import LiveExecutor, load_broker_cfg
from wallex_agent.shadow import restore_counters, drain_executor, ts_to_idx
from test_live_executor import FakeWallexClient, BROKER

SYMS = ["BTCTMN", "DOGETMN", "PEPETMN", "BTCUSDT"]
PX = {"BTCTMN": 20_000_000_000.0, "DOGETMN": 20_000.0, "PEPETMN": 0.95, "BTCUSDT": 100_000.0}
T0 = parse_tehran("2026-10-23 00:00")                     # bar 1000 = 16:40 Tehran
ENTER = {"action": "enter", "size_tier": "standard", "sl_pct": 5.0, "tp_plan": [{"pct": 6.0, "portion": 1.0}],
         "time_stop_min": 1440, "thesis": "test"}


class F15:
    def __init__(self, T):
        S = len(SYMS)
        self.x = {15: {"atr": np.full((S, T // 15), np.nan), "liq": np.full((S, T // 15), 0.5)}}
        self.tf = {15: type("TF", (), {"end_idx": np.arange(14, T, 15)})()}


class Reg:
    def at(self, j, s):
        return {"coin": "range", "btc_bull": True, "btc_ret15": 0.0, "atr_pct": 0.01}


class Pol:
    name = "stub"

    def decide(self, card, ctx):
        return dict(ENTER)


def account(t0, T):
    t = (t0 + np.arange(T) * 60).astype(np.int64)
    a = np.array([[PX[s]] * T for s in SYMS])
    v = np.ones((len(SYMS), T))
    m = Market(SYMS, t, a.copy(), a.copy(), a.copy(), a.copy(), v, v * a, v > 0,
               {s: {"step_dec": 8, "min_notional": 50_000, "spread_pct": 0.2} for s in SYMS})
    return Account("C7", Pol(), m, Fills(m), F15(T), Reg(), start_cash=5_000_000)


def executor(acc, fake, run_dir):
    cfg = load_broker_cfg(BROKER, {"fill_wait_s": 0, "poll_s": 0, "stop_supported": True,
                                   "coins": ["DOGETMN", "PEPETMN", "BTCTMN"]})
    ex = LiveExecutor(acc, fake, cfg, run_dir, sleep=lambda s: None)
    acc.hooks, acc.equity_fn = ex, ex.live_equity
    return ex


def first_run():
    """Run 1: one DOGE entry (live buy + server stop), a PEPE cooldown, then Ctrl+C (stop: no orders)."""
    run_dir = tempfile.mkdtemp(prefix="t8_")
    fake = FakeWallexClient()
    acc = account(T0, 1500)
    ex = executor(acc, fake, run_dir)
    ex.start(thread=False)
    d, p = SYMS.index("DOGETMN"), SYMS.index("PEPETMN")
    acc.m1_hist[d].add(900)                                # earlier signal -> this card triggers
    acc.on_card(Card(1000, d, "M1", 15, PX["DOGETMN"], PX["DOGETMN"] * 0.95, float("nan"), {}), 1000)
    acc.on_bar(1001)
    acc.cooldown_until[p] = 1100
    ex.save_state()
    n_orders = len(fake.log)
    ex.stop()
    assert len(fake.log) == n_orders                       # Ctrl+C path: nothing sent
    return run_dir, fake, acc, ex


def test_resume_on_new_grid_restores_positions_stops_counters():
    run_dir, fake, acc1, ex1 = first_run()
    d, p = SYMS.index("DOGETMN"), SYMS.index("PEPETMN")
    rec1 = dict(ex1.live["DOGETMN"])
    assert rec1["status"] == "open" and rec1["server_stop"]
    buys, cancels = len(fake.creates("BUY")), sum(1 for k, _ in fake.log if k == "cancel")
    st = json.load(open(os.path.join(run_dir, "live_state.json")))
    assert st["cooldowns"]["PEPETMN"] == float(acc1.m.t[1100]) and st["entries_hist"] == [float(acc1.m.t[1001])]

    # restart: the grid starts 60 minutes later -> every index shifts by -60
    acc2 = account(T0 + 3600, 1000)
    ex2 = executor(acc2, fake, run_dir)
    ex2.start(thread=False)
    restore_counters(acc2, ex2.state)
    drain_executor(acc2, ex2, 999)
    pos = acc2.positions[d]
    assert abs(pos.qty - rec1["qty"]) < 1e-9 and abs(pos.entry_px - rec1["avg_px"]) < 1e-6
    assert abs(pos.stop - rec1["stop_px"]) < 1e-6
    assert acc2.cooldown_until[p] == 1100 - 60
    assert list(acc2.entries_hist) == [1001 - 60]
    assert acc2.start_cash == 5_000_000
    # c7- stop re-linked, never cancelled; no second buy
    assert sum(1 for k, _ in fake.log if k == "cancel") == cancels and len(fake.creates("BUY")) == buys
    assert any(o["client_id"] == rec1["stop_cid"] and o["active"] for o in fake.open_orders())
    assert ex2.live["DOGETMN"]["stop_cid"] == rec1["stop_cid"]
    # paper cash re-based on the live value: paper equity == live equity
    assert abs(acc2.equity_at(999) - ex2.live_equity()) / ex2.live_equity() < 0.002


def test_no_duplicate_position_after_resume():
    run_dir, fake, acc1, ex1 = first_run()
    d = SYMS.index("DOGETMN")
    acc2 = account(T0 + 3600, 1000)
    ex2 = executor(acc2, fake, run_dir)
    ex2.start(thread=False)
    restore_counters(acc2, ex2.state)
    drain_executor(acc2, ex2, 990)
    buys = len(fake.creates("BUY"))
    for j in range(991, 996):                               # more cycles + reconcile: still one position
        acc2.on_bar(j)
        ex2.reconcile()
        drain_executor(acc2, ex2, j)
        ex2.fast_tick()
    assert list(acc2.positions) == [d] and len(fake.creates("BUY")) == buys
    # a new card on the same coin is refused by the engine (already in position) -> no new order
    acc2.on_card(Card(995, d, "M1", 15, PX["DOGETMN"], PX["DOGETMN"] * 0.95, float("nan"), {}), 995)
    acc2.on_bar(996)
    assert len(fake.creates("BUY")) == buys


def test_ts_to_idx():
    t = np.array([0, 60, 120], dtype=np.int64)
    assert ts_to_idx(t, 60) == 1 and ts_to_idx(t, 300) == 5 and ts_to_idx(t, 0) == 0


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
