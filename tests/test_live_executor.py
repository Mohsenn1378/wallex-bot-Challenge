"""T6 - live mirror executor (P0-07). Offline only: FakeWallexClient, no network, no key.
Run: python tests/test_live_executor.py   (or python -m pytest -q tests/test_live_executor.py)
"""
import json
import os
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.sim.fill_model import TAKER, MIN_NOTIONAL
from wallex_agent.sim.position import Plan, Position
from wallex_agent.live.wallex_client import (RoundingMixin, WallexClient, OrderRejected, OrderUnknown,
                                             balance_tuple, fmt_qty, fmt_price, normalize_order)
from wallex_agent.live.executor import LiveExecutor, load_broker_cfg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROKER = os.path.join(ROOT, "config", "broker.yaml")
SYMS = ["BTCTMN", "DOGETMN", "PEPETMN"]
RULES = {"BTCTMN": {"step_dec": 8, "tick_dec": 0, "min_notional": 50_000, "base": "BTC"},
         "DOGETMN": {"step_dec": 1, "tick_dec": 0, "min_notional": 50_000, "base": "DOGE"},
         "PEPETMN": {"step_dec": 0, "tick_dec": 4, "min_notional": 50_000, "base": "PEPE"}}


# ====================================================================== fakes
class FakeWallexClient(RoundingMixin):
    """Exchange emulator: minNotional on EVERY order (stops included), locked balances reported the Wallex way
    (`value` excludes locked here; the executor runs with balance_value_includes_locked: null)."""

    def __init__(self, tmn=5_000_000.0, holdings=None, includes_locked=None):
        self._lk = threading.RLock()
        self._rules = {k: dict(v) for k, v in RULES.items()}
        self.books = {
            "BTCTMN": {"ask": [[20_000_000_000, 1.0]], "bid": [[19_990_000_000, 1.0]]},
            "DOGETMN": {"ask": [[20_000, 100.0], [20_010, 100.0], [20_050, 1000.0]],
                        "bid": [[19_990, 100.0], [19_980, 100.0], [19_900, 1000.0]]},
            "PEPETMN": {"ask": [[0.9500, 1e7]], "bid": [[0.9490, 1e7]]},
        }
        self.free = defaultdict(float)
        self.locked = defaultdict(float)
        self.free["TMN"] = tmn
        for a, q in (holdings or {}).items():
            self.free[a] += q
        self.includes_locked = includes_locked
        self.orders = {}
        self.log = []                       # ("create"| "cancel", details)
        self.reject_next = defaultdict(int)
        self.unknown_next = defaultdict(int)
        self.max_fill_value = {}

    # -------------------------------------------------------------- read
    def rules(self, sym):
        return self._rules[sym]

    def markets(self):
        with self._lk:
            return {s: {**self._rules[s], "market_ok": s == "BTCTMN",
                        "bid": self.books[s]["bid"][0][0] if self.books[s]["bid"] else 0.0,
                        "ask": self.books[s]["ask"][0][0] if self.books[s]["ask"] else 0.0,
                        "last": self.books[s]["bid"][0][0] if self.books[s]["bid"] else 0.0} for s in self._rules}

    def depth(self, sym):
        with self._lk:
            return {"ask": [list(x) for x in self.books[sym]["ask"]], "bid": [list(x) for x in self.books[sym]["bid"]]}

    def balances(self):
        with self._lk:
            out = {}
            for a in set(self.free) | set(self.locked):
                f, l = self.free[a], self.locked[a]
                value = f + l if self.includes_locked else f       # what Wallex would report as `value`
                t = balance_tuple(value, l, None)                 # executor config: unknown -> conservative
                if t[0] > 1e-12:
                    out[a] = t
            return out

    def open_orders(self, sym=None):
        with self._lk:
            return [dict(o) for o in self.orders.values() if o["active"] and (sym is None or o["symbol"] == sym)]

    def get_order(self, cid):
        with self._lk:
            o = self.orders.get(cid)
            return dict(o) if o else None

    # -------------------------------------------------------------- write
    def _order(self, cid, sym, side, type_, qty, price, stop):
        return {"client_id": cid, "symbol": sym, "side": side, "type": type_, "price": price, "stop_price": stop,
                "orig_qty": qty, "executed_qty": 0.0, "executed_sum": 0.0, "status": "NEW", "active": True}

    def create_order(self, symbol, side, type, quantity, price=None, stop_price=None, client_id=None):
        with self._lk:
            side, type_ = side.upper(), type.upper()
            self.log.append(("create", {"cid": client_id, "sym": symbol, "side": side, "type": type_,
                                        "qty": quantity, "price": price, "stop": stop_price}))
            if self.unknown_next[symbol] > 0:
                self.unknown_next[symbol] -= 1
                raise OrderUnknown("timeout; not found")
            if self.reject_next[symbol] > 0:
                self.reject_next[symbol] -= 1
                raise OrderRejected("400: price out of fair range")
            if type_ == "MARKET":
                raise OrderRejected("market orders disabled")
            if client_id in self.orders:
                raise OrderRejected("duplicate client id")
            qty = self.round_qty(symbol, quantity)
            if qty * price < self._rules[symbol]["min_notional"]:
                raise OrderRejected("min notional")
            base = self._rules[symbol]["base"]
            o = self._order(client_id, symbol, side, type_, qty, price, stop_price)
            if side == "BUY":
                if qty * price * (1 + TAKER) > self.free["TMN"] + 1e-6:
                    raise OrderRejected("insufficient TMN")
                cap = self.max_fill_value.pop(symbol, None)
                ex_q = ex_s = 0.0
                book = self.books[symbol]["ask"]
                while book and book[0][0] <= price and ex_q < qty - 1e-12:
                    lvl_px, lvl_q = book[0]
                    take = min(lvl_q, qty - ex_q)
                    if cap is not None:
                        take = min(take, self.round_qty(symbol, max(cap - ex_s, 0.0) / lvl_px))
                    if take <= 0:
                        break
                    ex_q += take; ex_s += take * lvl_px
                    book[0][1] -= take
                    if book[0][1] <= 1e-12:
                        book.pop(0)
                    if cap is not None and ex_s >= cap - lvl_px:
                        break
                self.free["TMN"] -= ex_s * (1 + TAKER)
                self.free[base] += ex_q
                rest = qty - ex_q
                self.free["TMN"] -= rest * price
                self.locked["TMN"] += rest * price
            else:
                if qty > self.free[base] + 1e-9:
                    raise OrderRejected("insufficient balance")
                self.free[base] -= qty
                ex_q = ex_s = 0.0
                if type_ == "LIMIT":
                    book = self.books[symbol]["bid"]
                    while book and book[0][0] >= price and ex_q < qty - 1e-12:
                        take = min(book[0][1], qty - ex_q)
                        ex_q += take; ex_s += take * book[0][0]
                        book[0][1] -= take
                        if book[0][1] <= 1e-12:
                            book.pop(0)
                    self.free["TMN"] += ex_s * (1 - TAKER)
                self.locked[base] += qty - ex_q
            o["executed_qty"], o["executed_sum"] = ex_q, ex_s
            if ex_q >= o["orig_qty"] - 1e-12:
                o["status"], o["active"] = "FILLED", False
            elif ex_q > 0:
                o["status"] = "PARTIALLY_FILLED"
            self.orders[client_id] = o
            return dict(o)

    def _release(self, o):
        base = self._rules[o["symbol"]]["base"]
        rest = o["orig_qty"] - o["executed_qty"]
        if o["side"] == "BUY":
            self.locked["TMN"] -= rest * o["price"]; self.free["TMN"] += rest * o["price"]
        else:
            self.locked[base] -= rest; self.free[base] += rest

    def cancel_order(self, cid, method=None):
        with self._lk:
            self.log.append(("cancel", {"cid": cid}))
            o = self.orders.get(cid)
            if o and o["active"]:
                self._release(o)
                o["active"], o["status"] = False, "CANCELED"
            return dict(o) if o else None

    # -------------------------------------------------------------- test helpers
    def fill_stop(self, cid, px):
        with self._lk:
            o = self.orders[cid]
            base = self._rules[o["symbol"]]["base"]
            q = o["orig_qty"]
            self.locked[base] -= q
            self.free["TMN"] += q * px * (1 - TAKER)
            o.update(executed_qty=q, executed_sum=q * px, status="FILLED", active=False)

    def vanish(self, cid):
        with self._lk:
            o = self.orders[cid]
            self._release(o)
            o.update(active=False, status="CANCELED")

    def creates(self, side=None, type_=None):
        return [d for k, d in self.log if k == "create" and (side is None or d["side"] == side)
                and (type_ is None or d["type"] == type_)]


class FakeAccount:
    """Mimics the engine Account contract used by the executor."""

    def __init__(self, cash=5_000_000.0):
        self.m = SimpleNamespace(symbols=list(SYMS), t=[1_790_000_000 + 60 * i for i in range(500)])
        self.positions = {}
        self.cash = self.start_cash = cash
        self.hooks = None
        self.equity_fn = None
        self.disabled_coins = set()
        self.gov_state = "normal"
        self.cooldown_until = defaultdict(int)
        self.entries_hist = deque()
        self.flatten_mode = False
        self.allowed = {"DOGETMN", "PEPETMN", "BTCTMN"}
        self.events = []

    def idx(self, sym):
        return self.m.symbols.index(sym)

    def open(self, sym, notional, px, j=10, sl=5.0, tp=6.0):
        s = self.idx(sym)
        qty = notional / px
        pos = Position(s, qty, px, j, Plan(sl_pct=sl, tps=[(tp, 1.0)], time_stop_min=1440), px * (1 - sl / 100),
                       fees=qty * px * TAKER, meta={"dec": {"size_tier": "standard", "thesis": "test"}})
        pos.qty_initial = qty
        self.cash -= qty * px + pos.fees
        self.positions[s] = pos
        self.entries_hist.append(j)
        if self.hooks:
            self.hooks.on_open(self, pos, j)
        return pos

    def sync_position(self, s, entry_px, qty, fees):
        pos = self.positions[s]
        self.cash += pos.entry_px * pos.qty + pos.fees - (entry_px * qty + fees)
        pos.entry_px, pos.qty, pos.qty_initial, pos.fees = entry_px, qty, qty, fees
        self.events.append(("sync", s, entry_px, qty))

    def cancel_position(self, s, reason, j):
        pos = self.positions.pop(s)
        self.cash += pos.entry_px * pos.qty + pos.fees
        if j in self.entries_hist:
            self.entries_hist.remove(j)
        self.events.append(("cancel", s, reason))

    def close_external(self, s, j, px, reason):
        pos = self.positions.get(s)
        if pos is None:
            return
        fee = px * pos.qty * TAKER
        pos.exits.append((j, px, pos.qty, reason, fee))
        self.cash += px * pos.qty - fee
        pos.qty = 0.0
        del self.positions[s]
        self.events.append(("close", s, reason))
        if self.hooks:
            self.hooks.on_close(self, pos, j)

    close = close_external                                # engine-initiated close (TP / TIME) uses the same path

    def adopt_position(self, s, qty, px, j, plan):
        p = Plan(sl_pct=plan["sl_pct"], tps=list(plan["tps"]), time_stop_min=plan["time_stop_min"])
        pos = Position(s, qty, px, j, p, px * (1 - p.sl_pct / 100), meta={"dec": {}})
        pos.qty_initial = qty
        self.positions[s] = pos
        self.events.append(("adopt", s, qty))

    def drain(self, ex, j=20):
        for sym, px, qty, reason, ts in ex.pop_paper_closes():
            self.close_external(self.idx(sym), j, px, reason)
        for sym, qty, px, plan in ex.pop_adoptions():
            self.adopt_position(self.idx(sym), qty, px, j, plan)


def make(stop_supported=True, holdings=None, tmn=5_000_000.0, **over):
    run_dir = tempfile.mkdtemp(prefix="t6_")
    fake = FakeWallexClient(tmn=tmn, holdings=holdings)
    acc = FakeAccount()
    cfg = load_broker_cfg(BROKER, {"fill_wait_s": 0, "poll_s": 0, "stop_supported": stop_supported,
                                   "coins": ["DOGETMN", "PEPETMN", "BTCTMN"], **over})
    ex = LiveExecutor(acc, fake, cfg, run_dir, sleep=lambda s: None)
    acc.hooks = ex
    acc.equity_fn = ex.live_equity
    ex.start(thread=False)
    return fake, acc, ex, run_dir


D = "DOGETMN"


# ====================================================================== T6 cases
def test_01_full_buy_places_stop_and_syncs():
    fake, acc, ex, run_dir = make()
    acc.open(D, 150_000, 20_000)
    buys = fake.creates("BUY", "LIMIT")
    assert len(buys) == 1 and buys[0]["price"] <= 20_000 * 1.004, buys
    stops = fake.creates("SELL", "STOP_LIMIT")
    assert len(stops) == 1, stops
    rec = ex.live[D]
    assert rec["status"] == "open" and rec["server_stop"] and abs(rec["qty"] - 7.5) < 1e-9
    assert abs(stops[0]["stop"] - 19_000) < 1 and stops[0]["qty"] == 7.5
    assert abs(stops[0]["price"] - 19_000 * 0.985) < 1
    pos = acc.positions[acc.idx(D)]
    assert abs(pos.qty - 7.5) < 1e-9 and abs(pos.entry_px - 20_000) < 1e-6
    st = json.load(open(os.path.join(run_dir, "live_state.json")))
    for k in ("start_cash", "start_ts", "epoch", "seq", "initial_balances", "gov_state", "disabled_coins",
              "cooldowns", "entries_hist"):
        assert k in st, k
    for k in ("qty", "avg_px", "fees", "entry_cid", "stop_cid", "stop_px", "stop_limit_px", "server_stop", "sl_pct",
              "tp_pct", "time_stop_deadline_ts", "size_tier", "opened_ts", "status"):
        assert k in st["live"][D], k
    assert rec["entry_cid"].startswith(f"c7-{ex.epoch}-{D}-buy-") and rec["entry_cid"] in st["sent_cids"]


def test_02_partial_fill_30k_tops_up_52k():
    fake, acc, ex, _ = make()
    fake.max_fill_value[D] = 30_000
    acc.open(D, 65_000, 20_000)
    buys = fake.creates("BUY", "LIMIT")
    assert len(buys) == 2, buys
    top = buys[1]["qty"] * buys[1]["price"]
    assert 51_000 <= top <= 52_100 and "-topup-" in buys[1]["cid"], top
    rec = ex.live[D]
    assert rec["status"] == "open" and abs(rec["qty"] - (1.5 + 2.6)) < 1e-9, rec["qty"]
    assert abs(acc.positions[acc.idx(D)].qty - rec["qty"]) < 1e-9
    assert len(fake.creates("SELL", "STOP_LIMIT")) == 1          # 4.1 * 18,715 > 50k -> stop accepted


def test_03_rejection_cancels_paper_and_two_disable_coin():
    fake, acc, ex, _ = make()
    fake.reject_next[D] = 2
    acc.open(D, 150_000, 20_000)
    assert acc.idx(D) not in acc.positions and abs(acc.cash - 5_000_000) < 1e-6
    assert D not in acc.disabled_coins and ("cancel", acc.idx(D), "live_entry_failed") in acc.events
    acc.open(D, 150_000, 20_000)
    assert acc.idx(D) not in acc.positions and abs(acc.cash - 5_000_000) < 1e-6
    assert D in acc.disabled_coins and D in ex.state["disabled_coins"]
    n = len(fake.creates())
    acc.open(D, 150_000, 20_000)                                    # disabled -> no order at all
    assert len(fake.creates()) == n and acc.idx(D) not in acc.positions


def test_04_exit_cancels_stop_then_sells():
    fake, acc, ex, run_dir = make()
    acc.open(D, 150_000, 20_000)
    stop_cid = ex.live[D]["stop_cid"]
    acc.close(acc.idx(D), 30, 21_200, "TP1")
    i_cancel = next(i for i, (k, d) in enumerate(fake.log) if k == "cancel" and d["cid"] == stop_cid)
    i_sell = next(i for i, (k, d) in enumerate(fake.log) if k == "create" and d["side"] == "SELL" and d["type"] == "LIMIT")
    assert i_cancel < i_sell
    assert fake.free["DOGE"] < 1e-9 and fake.locked["DOGE"] < 1e-9
    assert D not in ex.live and os.path.exists(os.path.join(run_dir, "trades_live.csv"))


def test_05_server_stop_filled_closes_paper_without_second_sell():
    fake, acc, ex, _ = make()
    acc.open(D, 150_000, 20_000)
    fake.fill_stop(ex.live[D]["stop_cid"], 18_800)
    ex.fast_tick()
    closes = ex.pop_paper_closes()
    assert len(closes) == 1 and closes[0][3] == "SERVER_STOP" and abs(closes[0][1] - 18_800) < 1e-6, closes
    ex._paper_closes.put(closes[0])
    acc.drain(ex)
    assert acc.idx(D) not in acc.positions and D not in ex.live
    assert len(fake.creates("SELL", "LIMIT")) == 0


def test_06_client_stop_closes_paper_without_second_sell():
    fake, acc, ex, _ = make(stop_supported=False)
    acc.open(D, 150_000, 20_000)
    assert len(fake.creates("SELL", "STOP_LIMIT")) == 0 and ex.live[D]["server_stop"] is False
    fake.books[D]["bid"] = [[18_900, 100.0], [18_800, 1000.0]]
    ex.fast_tick()
    assert len(fake.creates("SELL", "LIMIT")) == 1
    closes = ex.pop_paper_closes()
    assert len(closes) == 1 and closes[0][3] == "CLIENT_STOP", closes
    ex._paper_closes.put(closes[0])
    acc.drain(ex)
    ex.fast_tick()
    assert len(fake.creates("SELL", "LIMIT")) == 1 and acc.idx(D) not in acc.positions and D not in ex.live


def test_07_fully_locked_in_stop_is_not_gone_and_equity_right():
    fake, acc, ex, _ = make()
    acc.open(D, 150_000, 20_000)
    assert fake.free["DOGE"] < 1e-9 and abs(fake.locked["DOGE"] - 7.5) < 1e-9      # all qty locked by the stop
    ex.reconcile()
    assert ex.live[D]["status"] == "open" and ex.pop_paper_closes() == [] and acc.idx(D) in acc.positions
    expected = fake.free["TMN"] + fake.locked["TMN"] + 7.5 * 19_990
    assert abs(ex.live_equity() - expected) < 1.0, (ex.live_equity(), expected)
    assert abs(expected - 5_000_000) < 0.01 * 5_000_000


def test_08_vanished_stop_is_replaced():
    fake, acc, ex, _ = make()
    acc.open(D, 150_000, 20_000)
    old = ex.live[D]["stop_cid"]
    fake.vanish(old)
    ex.fast_tick()
    new = ex.live[D]["stop_cid"]
    assert new and new != old and fake.orders[new]["active"] and fake.orders[new]["orig_qty"] == 7.5
    assert ex.pop_paper_closes() == [] and len(fake.creates("SELL", "LIMIT")) == 0


def test_09_initial_holdings_never_adopted_or_sold():
    fake, acc, ex, _ = make(holdings={"DOGE": 10.0, "BTC": 0.001})
    ex.reconcile()
    assert ex.pop_adoptions() == [] and ex.live == {}
    fake.free["DOGE"] += 5.0                                         # coins bought by the bot, not recorded
    ex.reconcile()
    ad = ex.pop_adoptions()
    assert len(ad) == 1 and ad[0][0] == D and abs(ad[0][1] - 5.0) < 1e-9, ad
    assert abs(ex.live[D]["stop_px"] - 19_990 * 0.95) < 1e-6
    rep = ex.final_flatten()
    sold = sum(s["sold_qty"] for p in rep["passes"] for s in p["sells"])
    assert abs(sold - 5.0) < 1e-9 and abs(fake.free["DOGE"] - 10.0) < 1e-9 and abs(fake.free["BTC"] - 0.001) < 1e-12
    assert all(s["sym"] != "BTCTMN" for p in rep["passes"] for s in p["sells"])


def test_10_final_flatten_shallow_book_ladder_and_report():
    fake, acc, ex, run_dir = make()
    acc.open(D, 150_000, 20_000)
    fake.books[D]["bid"] = [[19_990, 1.0], [19_900, 1.0], [19_700, 1.0], [19_500, 10.0]]
    ex.start_flatten_loop()
    ex._flat_thread.join(timeout=10)
    rep = ex.final_flatten()
    sells = [s for p in rep["passes"] for s in p["sells"]]
    assert sum(s["sold_qty"] for s in sells) == 7.5 and rep["done"] is True
    att = [a for s in sells for a in s["attempts"] if "executed_qty" in a]
    assert len(att) >= 2 and att[0]["slip"] == 0.008 and att[1]["slip"] == 0.012, att
    assert len(fake.creates("SELL", "LIMIT")) == len(att)
    assert any(c["cid"] for p in rep["passes"] for c in p["cancelled"])          # stop cancelled first
    assert os.path.exists(os.path.join(run_dir, "flatten_report.json"))
    closes = ex.pop_paper_closes()
    assert closes and closes[0][3] == "END"
    ex._paper_closes.put(closes[0])
    acc.drain(ex)                                                     # on_close -> numbers only
    assert len(fake.creates("SELL", "LIMIT")) == len(att) and D not in ex.live


def test_11_rounding():
    assert fmt_qty(1234.56789, 0) == "1234"
    assert fmt_qty(0.000012345678, 8) == "0.00001234"
    assert fmt_price(0.123456, 4, "BUY") == "0.1235" and fmt_price(0.123456, 4, "SELL") == "0.1234"
    assert fmt_price(22485751813.2, 0, "BUY") == "22485751814" and fmt_price(22485751813.7, 0, "sell") == "22485751813"
    assert fmt_qty(1e-8, 8) == "0.00000001" and "e" not in fmt_qty(5e-7, 8)
    assert fmt_qty(7.5, 1) == "7.5" and fmt_qty(0.3, 2) == "0.3"
    c = WallexClient("k", {}, None, transport=lambda *a: (_ for _ in ()).throw(AssertionError("no network")))
    c._rules = {k: dict(v) for k, v in RULES.items()}
    b = c.build_order_body("PEPETMN", "buy", "limit", 52631.9, price=0.95004, client_id="c7-x-PEPETMN-buy-1")
    assert b["quantity"] == "52631" and b["price"] == "0.9501" and b["side"] == "BUY" and b["type"] == "LIMIT"
    s = c.build_order_body("DOGETMN", "SELL", "STOP_LIMIT", 7.56, price=18714.7, stop_price=18999.2)
    assert s["quantity"] == "7.5" and s["price"] == "18714" and s["stop_Price"] == "19000"
    assert c.round_qty("BTCTMN", 0.123456789) == 0.12345678 and c.round_price("PEPETMN", 0.94991, "SELL") == 0.9499


class _Transport:
    def __init__(self, post_fail=True):
        self.calls = []
        self.post_fail = post_fail

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url))
        if method == "POST":
            if self.post_fail:
                raise TimeoutError("timed out")
            return 200, json.dumps({"success": True, "result": {"clientOrderId": "c7-a-PEPETMN-buy-1", "status": "NEW",
                                                                "origQty": "52631", "executedQty": "0"}})
        if "/v1/account/orders/" in url:
            return 404, json.dumps({"success": False, "message": "not found"})
        return 200, json.dumps({"success": True, "result": {}})


def test_12_create_timeout_get_order_no_blind_resend():
    log = os.path.join(tempfile.mkdtemp(prefix="t6_"), "orders.jsonl")
    key = "SECRET-KEY-123"
    tr = _Transport()
    c = WallexClient(key, {"dup_id_rejected": None}, log, transport=tr, sleep=lambda s: None)
    c._rules = {k: dict(v) for k, v in RULES.items()}
    try:
        c.create_order("PEPETMN", "BUY", "LIMIT", 52631, price=0.95, client_id="c7-a-PEPETMN-buy-1")
        assert False, "must raise OrderUnknown"
    except OrderUnknown:
        pass
    assert sum(1 for m, _ in tr.calls if m == "POST") == 1
    assert any(m == "GET" and "/v1/account/orders/c7-a-PEPETMN-buy-1" in u for m, u in tr.calls)
    text = open(log).read()
    assert key not in text and "x-api-key" not in text and '"POST"' in text
    # dup_id_rejected: true -> exactly one re-send with the SAME id
    tr2 = _Transport()
    c2 = WallexClient(key, {"dup_id_rejected": True}, log, transport=tr2, sleep=lambda s: None)
    c2._rules = c._rules
    try:
        c2.create_order("PEPETMN", "BUY", "LIMIT", 52631, price=0.95, client_id="c7-a-PEPETMN-buy-1")
    except OrderUnknown:
        pass
    assert sum(1 for m, _ in tr2.calls if m == "POST") == 2
    # executor level: unknown entry -> paper cancelled, status unknown, no second BUY; reconcile resolves it
    fake, acc, ex, _ = make()
    fake.unknown_next[D] = 1
    acc.open(D, 150_000, 20_000)
    assert len(fake.creates("BUY")) == 1 and acc.idx(D) not in acc.positions and ex.live[D]["status"] == "unknown"
    ex.reconcile()
    assert D not in ex.live and len(fake.creates("BUY")) == 1


def test_13_keyboard_interrupt_sends_zero_sell_orders():
    fake, acc, ex, run_dir = make(fast_tick_s=0.01)
    ex.stop()                                     # restart with the fast_tick thread running
    ex._stopped = False
    ex._stop_evt.clear()
    ex._thread = threading.Thread(target=ex._tick_loop, daemon=True)
    ex._thread.start()
    acc.open(D, 150_000, 20_000)
    n_orders = len(fake.creates())
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:                     # what shadow.py does: detach hooks, save, exit - no close_all
        acc.hooks = None
        ex.stop()
    fake.books[D]["bid"] = [[15_000, 1000.0]]     # crash below the stop after the interrupt
    time.sleep(0.1)
    ex.fast_tick(); ex.on_close(acc, acc.positions[acc.idx(D)], 50); ex.reconcile()
    assert len(fake.creates("SELL", "LIMIT")) == 0 and len(fake.creates()) == n_orders
    assert not ex._thread.is_alive()
    st = json.load(open(os.path.join(run_dir, "live_state.json")))
    assert st["live"][D]["status"] == "open" and st["live"][D]["stop_cid"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(dict(globals()).items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except Exception as e:
            bad += 1; print("FAIL", fn.__name__, type(e).__name__, e)
    print(f"{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
