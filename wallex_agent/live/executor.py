"""
LiveExecutor - mirror execution of the paper engine on Wallex (proposal §5.4, task P0-07).

The paper engine decides; this class executes on Wallex and reports real numbers back.
Wallex balances/orders are the source of truth.

Engine -> executor (main-loop thread):
    hooks.on_open(acc, pos, j)    walk depth -> LIMIT BUY (cap +max_entry_slip over best ask) -> wait fill_wait_s
                                  -> cancel remainder -> real qty from TOTAL balance -> top-up if 0 < value < 55k
                                  -> acc.sync_position(...) -> STOP_LIMIT SELL only if stop_supported is True.
                                  failure -> acc.cancel_position(sym_idx, "live_entry_failed", j);
                                  2 consecutive rejections -> acc.disabled_coins.add(sym)
    hooks.on_close(acc, pos, j)   status closed_* -> only record numbers (NEVER sell twice);
                                  else cancel stop + confirm -> LIMIT SELL ladder (max 3) -> remainder < 50k = dust
    hooks.mark_unknown(sym)       next reconcile() resolves it
    reconcile()                   at start and every 5 min (main loop)
Executor -> engine (never touches `acc` from the fast_tick / flatten threads):
    pop_paper_closes() -> [(sym, px, qty, reason, ts)]   main loop: acc.close_external(sym_idx, j, px, reason)
    pop_adoptions()    -> [(sym, qty, px, plan_dict)]    main loop: acc.adopt_position(sym_idx, qty, px, j, plan)

Ctrl+C / crash: stop() -> thread stopped, state saved, NO order of any kind is sent afterwards.
"""
from __future__ import annotations

import csv
import io
import json
import os
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..sim.fill_model import MIN_NOTIONAL, STUCK_GUARD, TAKER
from .wallex_client import (OrderRejected, OrderUnknown, RateLimited, WallexError, append_line_retry, is_done)

TEHRAN = timezone(timedelta(hours=3, minutes=30))     # fixed offset, no zoneinfo (Windows)
TOPUP_BELOW = 55_000                                   # filled value in (0, 55k) -> top-up buy
ADOPT_MIN = 55_000                                     # unrecorded bot coins >= 55k -> adopt
DEFAULT_SL_PCT, DEFAULT_TP_PCT = 5.0, 6.0
ACTIVE = ("entering", "open", "unknown", "exit_failed")
FILLED = "FILLED"

DEFAULTS = {
    "stop_supported": False, "stop_type": "STOP_LIMIT", "stop_field": "stop_Price", "limit_rel_ok": False,
    "balance_value_includes_locked": None, "dup_id_rejected": None, "cancel_method": "query", "buy_fee_in_base": None,
    "max_entry_slip": 0.004, "max_limit_distance": 0.03, "exit_slip_ladder": [0.008, 0.012, 0.02, 0.03],
    "poll_s": 1, "fill_wait_s": 15,
    # executor-only (not in broker.yaml; runner may pass them)
    "fast_tick_s": 15, "flatten_interval_s": 60, "stop_limit_offset": 0.015,
    "flatten_start_ts": None, "hard_deadline_ts": None, "coins": None, "code_version": "",
}


# ---------------------------------------------------------------- helpers
def load_broker_cfg(path: str = "config/broker.yaml", overrides: dict | None = None) -> dict:
    import yaml
    raw = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = {**DEFAULTS, **raw, **(overrides or {})}
    cfg["stop_supported"] = cfg.get("stop_supported") is True          # boolean only; anything else = False
    for k in ("balance_value_includes_locked", "dup_id_rejected", "buy_fee_in_base", "limit_rel_ok"):
        if cfg.get(k) not in (True, False, None):
            cfg[k] = None
    return cfg


def tehran_ts(s: str) -> float:
    """'YYYY-MM-DD HH:MM' Tehran (+03:30) -> unix seconds."""
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TEHRAN).timestamp()


def tehran_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, TEHRAN).strftime("%Y-%m-%d %H:%M:%S")


def base36(n: int) -> str:
    chars, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    n = int(n)
    while True:
        n, r = divmod(n, 36)
        out = chars[r] + out
        if n == 0:
            return out


def write_text_retry(path: str, text: str, tries: int = 6, wait: float = 0.2) -> bool:
    """Atomic write (tmp + os.replace) with retry: a file open in Excel must not crash the bot."""
    tmp = f"{path}.tmp{os.getpid()}"
    for k in range(tries):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(wait * (k + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


class EntryFailed(Exception):
    pass


# ---------------------------------------------------------------- executor
class LiveExecutor:
    def __init__(self, account, client, cfg: dict | None, run_dir: str, now=time.time, sleep=time.sleep):
        self.acc = account
        self.client = client
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.cfg["stop_supported"] = self.cfg.get("stop_supported") is True
        self.stop_supported = self.cfg["stop_supported"]
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.state_path = os.path.join(run_dir, "live_state.json")
        self.events_path = os.path.join(run_dir, "live_events.jsonl")
        self.trades_path = os.path.join(run_dir, "trades_live.csv")
        self.flatten_path = os.path.join(run_dir, "flatten_report.json")
        self._now, self._sleep = now, sleep
        self.epoch = base36(int(self._now()))
        self.live: dict[str, dict] = {}
        self.state: dict = {"seq": 0, "initial_balances": {}, "disabled_coins": [], "reject_streak": {},
                            "sent_cids": {}, "closed": []}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._state_lock = threading.RLock()
        self._paper_closes: queue.Queue = queue.Queue()
        self._adoptions: queue.Queue = queue.Queue()
        self._adopt_queued: set[str] = set()
        self._stop_evt = threading.Event()
        self._flat_evt = threading.Event()
        self._thread = None
        self._flat_thread = None
        self._stopped = False
        self._flattening = False
        self._main_ident = threading.get_ident()
        self._acc_snap: dict = {}
        self._eq_cache = (0.0, None)
        self._eq_lock = threading.Lock()
        self.flatten_report = {"passes": [], "done": False}
        coins = self.cfg.get("coins") or getattr(account, "allowed", None) or list(account.m.symbols)
        self.coins = sorted(s for s in coins if str(s).endswith("TMN"))

    # ------------------------------------------------------------ small utils
    def _lock(self, sym: str) -> threading.Lock:
        with self._locks_guard:
            if sym not in self._locks:
                self._locks[sym] = threading.Lock()
            return self._locks[sym]

    def _event(self, kind: str, **kw):
        rec = {"ts": round(self._now(), 3), "event": kind, **kw}
        append_line_retry(self.events_path, json.dumps(rec, ensure_ascii=False, default=str))

    def _base(self, sym: str) -> str:
        try:
            b = self.client.rules(sym).get("base")
        except Exception:
            b = None
        return b or (sym[:-3] if sym.endswith("TMN") else sym)

    def _sym_idx(self, sym: str):
        try:
            return list(self.acc.m.symbols).index(sym)
        except ValueError:
            return None

    def _is_main(self) -> bool:
        return threading.get_ident() == self._main_ident

    def _new_cid(self, sym: str, purpose: str) -> str:
        """c7-{epoch}-{sym}-{purpose}-{seq}; recorded in live_state.json BEFORE the order is sent."""
        with self._state_lock:
            self.state["seq"] = int(self.state.get("seq", 0)) + 1
            cid = f"c7-{self.epoch}-{sym}-{purpose}-{self.state['seq']}"
            sc = self.state.setdefault("sent_cids", {})
            sc[cid] = {"sym": sym, "purpose": purpose, "ts": round(self._now(), 3)}
            if len(sc) > 600:
                for k in list(sc)[:-500]:
                    del sc[k]
            self._save_state()
        return cid

    def _excess(self, bals: dict, asset: str) -> tuple[float, float]:
        """(coins above the pre-bot snapshot, free) - pre-existing holdings are never touched."""
        total, free, _locked = bals.get(asset, (0.0, 0.0, 0.0))
        init = float(self.state.get("initial_balances", {}).get(asset, 0.0))
        return max(total - init, 0.0), free

    def _best(self, sym: str, side: str) -> float:
        book = self.client.depth(sym)
        rows = book.get(side) or []
        return float(rows[0][0]) if rows else 0.0

    # ------------------------------------------------------------ state
    def _snapshot_acc(self):
        """Read engine counters - only from the main thread (the engine is not thread-safe)."""
        acc = self.acc
        try:
            t = acc.m.t
            T = len(t)

            def idx_ts(j):
                j = int(j)
                return float(t[j]) if 0 <= j < T else float(t[T - 1]) + (j - (T - 1)) * 60.0

            syms = list(acc.m.symbols)
            self._acc_snap = {
                "start_cash": float(getattr(acc, "start_cash", 0.0)),
                "gov_state": getattr(acc, "gov_state", "normal"),
                "cooldowns": {syms[s]: idx_ts(j) for s, j in dict(getattr(acc, "cooldown_until", {}) or {}).items()
                              if j and 0 <= s < len(syms)},
                "entries_hist": [idx_ts(j) for j in list(getattr(acc, "entries_hist", []) or [])],
                "disabled_coins": sorted(set(getattr(acc, "disabled_coins", set()) or set())
                                         | set(self.state.get("disabled_coins", []))),
            }
        except Exception as e:                       # never let bookkeeping break trading
            self._event("snapshot_error", error=repr(e))

    def _save_state(self):
        with self._state_lock:
            if self._is_main():
                self._snapshot_acc()
            st = {**self.state, **self._acc_snap, "epoch": self.epoch, "live": self.live,
                  "code_version": self.cfg.get("code_version", ""), "saved_ts": round(self._now(), 3),
                  "saved_tehran": tehran_str(self._now())}
            st["disabled_coins"] = sorted(set(st.get("disabled_coins", [])) | set(self.state.get("disabled_coins", [])))
            text = None
            for _ in range(5):                          # another thread may mutate a dict mid-dump
                try:
                    text = json.dumps(st, ensure_ascii=False, indent=1, default=str)
                    break
                except RuntimeError:
                    time.sleep(0.01)
            ok = text is not None and write_text_retry(self.state_path, text)
        if not ok:
            self._event("state_write_failed")

    def _load_state(self) -> bool:
        if not os.path.exists(self.state_path):
            return False
        with open(self.state_path, encoding="utf-8") as fh:
            st = json.load(fh)
        self.live = st.pop("live", {}) or {}
        st.pop("epoch", None)                       # new process -> new epoch (unique client ids)
        self.state.update(st)
        self._acc_snap = {k: st[k] for k in ("start_cash", "gov_state", "cooldowns", "entries_hist", "disabled_coins")
                          if k in st}
        return True

    # ------------------------------------------------------------ lifecycle
    def start(self, thread: bool = True):
        self._main_ident = threading.get_ident()
        self._stopped = False
        try:
            self.client.markets()                    # load rules
        except Exception as e:
            self._event("markets_error", error=repr(e))
        resumed = self._load_state()
        if not resumed:
            bals = self.client.balances()
            self.state["initial_balances"] = {a: float(v[0]) for a, v in bals.items() if a != "TMN"}
            self.state["initial_tmn"] = float(bals.get("TMN", (0.0,))[0])
            self.state["start_ts"] = round(self._now(), 3)
            self.state["start_cash"] = float(getattr(self.acc, "start_cash", 0.0))
        else:
            try:
                for c in self.state.get("disabled_coins", []):
                    self.acc.disabled_coins.add(c)
                if self.state.get("start_cash"):
                    self.acc.start_cash = float(self.state["start_cash"])
            except Exception as e:
                self._event("resume_apply_error", error=repr(e))
            # restored open positions -> engine adopts them (paper side rebuilt from timestamps)
            for sym, rec in self.live.items():
                if rec.get("status") == "open" and rec.get("qty", 0) > 0:
                    self._queue_adoption(sym, rec, source="resume")
        self._event("start", resumed=resumed, epoch=self.epoch, stop_supported=self.stop_supported)
        self._save_state()
        self.reconcile()
        if thread:
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._tick_loop, name="fast_tick", daemon=True)
            self._thread.start()

    def _tick_loop(self):
        while not self._stop_evt.wait(float(self.cfg.get("fast_tick_s", 15))):
            if self._stopped:
                break
            try:
                self.fast_tick()
            except Exception as e:
                self._event("fast_tick_error", error=repr(e))

    def stop(self):
        """Ctrl+C / crash path: stop threads, save state. Sends NO order (no sell, no cancel)."""
        self._stopped = True
        self._stop_evt.set()
        self._flat_evt.set()
        for th in (self._thread, self._flat_thread):
            if th is not None and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=30)
        self._event("stop")
        self._save_state()

    # ------------------------------------------------------------ queues to the main loop
    def pop_paper_closes(self) -> list:
        out = []
        while True:
            try:
                out.append(self._paper_closes.get_nowait())
            except queue.Empty:
                return out

    def pop_adoptions(self) -> list:
        out = []
        while True:
            try:
                item = self._adoptions.get_nowait()
            except queue.Empty:
                return out
            self._adopt_queued.discard(item[0])
            out.append(item)

    def _queue_paper_close(self, sym, px, qty, reason):
        self._paper_closes.put((sym, float(px), float(qty), reason, round(self._now(), 3)))
        self._event("paper_close_queued", sym=sym, px=px, qty=qty, reason=reason)

    def _queue_adoption(self, sym, rec, source):
        if sym in self._adopt_queued:
            return
        mins = max(int((rec.get("time_stop_deadline_ts") or (self._now() + 1440 * 60)) - self._now()) // 60, 1)
        plan = {"sl_pct": rec.get("sl_pct", DEFAULT_SL_PCT), "tps": [(rec.get("tp_pct", DEFAULT_TP_PCT), 1.0)],
                "time_stop_min": mins, "stop_px": rec.get("stop_px"), "opened_ts": rec.get("opened_ts"),
                "source": source}
        rec["adopt_pending"] = True
        self._adopt_queued.add(sym)
        self._adoptions.put((sym, float(rec["qty"]), float(rec["avg_px"]), plan))
        self._event("adoption_queued", sym=sym, qty=rec["qty"], px=rec["avg_px"], source=source)

    # ------------------------------------------------------------ order primitives
    def _wait_fill(self, cid: str, o: dict | None) -> dict | None:
        deadline = self._now() + float(self.cfg.get("fill_wait_s", 15))
        while not is_done(o) and self._now() < deadline:
            self._sleep(float(self.cfg.get("poll_s", 1)))
            try:
                o2 = self.client.get_order(cid)
                if o2 is not None:
                    o = o2
            except WallexError:
                pass
        if o is not None and not is_done(o):
            ok, o2 = self._cancel_confirm(cid)
            if o2 is not None:
                o = o2
        return o

    def _cancel_confirm(self, cid: str):
        """Cancel and confirm it is no longer active. Returns (confirmed, order_or_None)."""
        try:
            o = self.client.cancel_order(cid)
        except Exception as e:
            self._event("cancel_error", cid=cid, error=repr(e))
            try:
                o = self.client.get_order(cid)
            except Exception:
                return False, None
        for _ in range(3):
            if is_done(o):
                return True, o
            self._sleep(float(self.cfg.get("poll_s", 1)))
            try:
                o = self.client.get_order(cid)
            except Exception:
                pass
        return is_done(o), o

    def _limit_buy(self, sym: str, notional: float, purpose: str, rec: dict) -> dict:
        r = self.client.rules(sym)
        asks = self.client.depth(sym).get("ask") or []
        if not asks:
            raise EntryFailed("empty_book")
        best = float(asks[0][0])
        cap = best * (1 + min(float(self.cfg["max_entry_slip"]), float(self.cfg["max_limit_distance"])))
        acc_val, need = 0.0, best
        for px, q in asks:
            if px > cap:
                break
            need = px
            acc_val += px * q
            if acc_val >= notional:
                break
        px = self.client.round_price(sym, need if acc_val >= notional else cap, "BUY")
        qty = self.client.round_qty(sym, notional / px)
        if qty * px < float(r.get("min_notional", MIN_NOTIONAL)):
            raise EntryFailed(f"below_min_notional {qty * px:.0f}")
        if self._stopped:
            raise EntryFailed("executor_stopped")
        cid = self._new_cid(sym, purpose)
        rec.setdefault("cids", []).append(cid)
        o = self.client.create_order(sym, "BUY", "LIMIT", qty, price=px, client_id=cid)
        o = self._wait_fill(cid, o)
        if o is not None and not is_done(o):
            raise OrderUnknown(f"{cid}: remainder cancel not confirmed")
        o = o or {}
        o.setdefault("client_id", cid)
        return o

    def _place_stop(self, sym: str, rec: dict, max_qty: float | None = None) -> bool:
        if not self.stop_supported or self._stopped:
            rec["server_stop"] = False
            return False
        qty = self.client.round_qty(sym, rec["qty"] if max_qty is None else min(rec["qty"], max_qty))
        lim = self.client.round_price(sym, rec["stop_limit_px"], "SELL")
        if qty * lim < float(self.client.rules(sym).get("min_notional", MIN_NOTIONAL)):
            rec["server_stop"], rec["stop_cid"] = False, None
            self._event("stop_below_min_notional", sym=sym, value=qty * lim)
            return False
        cid = self._new_cid(sym, "stop")
        rec["stop_cid"] = cid
        try:
            self.client.create_order(sym, "SELL", self.cfg.get("stop_type", "STOP_LIMIT"), qty, price=lim,
                                     stop_price=rec["stop_px"], client_id=cid)
            rec["server_stop"], rec["stop_qty"] = True, qty
            return True
        except OrderRejected as e:
            rec["server_stop"], rec["stop_cid"] = False, None
            self._event("stop_rejected", sym=sym, error=str(e))
            return False
        except OrderUnknown as e:                      # fast_tick checks it and re-places if missing
            rec["server_stop"] = True
            self._event("stop_unknown", sym=sym, error=str(e))
            return False

    def _drop_stop(self, sym: str, rec: dict) -> str:
        """Cancel the server stop and confirm. Returns 'none' | 'cancelled' | 'filled' | 'failed'."""
        cid = rec.get("stop_cid")
        if not cid:
            return "none"
        ok, o = self._cancel_confirm(cid)
        if o is not None and o.get("executed_qty", 0) > 0:
            ex_q, ex_s = o["executed_qty"], o["executed_sum"]
            px = ex_s / ex_q if ex_q else rec.get("stop_limit_px", 0)
            rest = max(rec["qty"] - ex_q, 0.0)
            if o.get("status") == FILLED or rest * px < MIN_NOTIONAL:
                self._mark_closed(sym, rec, "closed_by_server", px, ex_q, ex_s)
                self._queue_paper_close(sym, px, ex_q, "SERVER_STOP")
                return "filled"
            rec["qty"] = rest
            rec.setdefault("partial_sells", []).append({"qty": ex_q, "sum": ex_s, "src": "stop"})
        if not ok:
            self._event("stop_cancel_unconfirmed", sym=sym, cid=cid)
            return "failed"
        rec["stop_cid"], rec["server_stop"] = None, False
        return "cancelled"

    def _sell_ladder(self, sym: str, qty: float, purpose: str, ladder_start: int = 0, attempts: int = 3) -> dict:
        """LIMIT SELL on the bids with the broker ladder; never sells pre-existing holdings or locked coins."""
        ladder = list(self.cfg.get("exit_slip_ladder") or [0.008, 0.012, 0.02, 0.03])
        maxd = float(self.cfg.get("max_limit_distance", 0.03))
        mn = float(self.client.rules(sym).get("min_notional", MIN_NOTIONAL))
        base = self._base(sym)
        res = {"sym": sym, "target_qty": qty, "sold_qty": 0.0, "sold_sum": 0.0, "attempts": [], "dust": False,
               "remaining_qty": qty, "remaining_value": 0.0}
        remaining, best = qty, 0.0
        for k in range(attempts):
            if self._stopped:
                res["aborted"] = "executor_stopped"
                break
            bals = self.client.balances()
            excess, free = self._excess(bals, base)
            sellable = self.client.round_qty(sym, min(remaining, excess, free))
            bids = self.client.depth(sym).get("bid") or []
            if not bids:
                res["attempts"].append({"error": "empty_book"})
                break
            best = float(bids[0][0])
            slip = ladder[min(ladder_start + k, len(ladder) - 1)]
            px = self.client.round_price(sym, best * (1 - min(slip, maxd)), "SELL")
            if sellable * px < mn:
                res["dust"] = sellable > 0 or remaining * best < mn
                res["attempts"].append({"skip": "below_min_notional", "sellable": sellable, "px": px})
                break
            cid = self._new_cid(sym, purpose)
            att = {"cid": cid, "slip": slip, "px": px, "qty": sellable}
            try:
                o = self.client.create_order(sym, "SELL", "LIMIT", sellable, price=px, client_id=cid)
            except OrderRejected as e:
                att["rejected"] = str(e)
                res["attempts"].append(att)
                continue
            except OrderUnknown as e:
                att["unknown"] = str(e)
                res["attempts"].append(att)
                break
            o = self._wait_fill(cid, o) or {}
            ex_q, ex_s = float(o.get("executed_qty", 0.0)), float(o.get("executed_sum", 0.0))
            att.update(executed_qty=ex_q, executed_sum=ex_s, status=o.get("status"))
            res["attempts"].append(att)
            res["sold_qty"] += ex_q
            res["sold_sum"] += ex_s
            remaining = max(remaining - ex_q, 0.0)
            if remaining * best < mn:
                break
        res["remaining_qty"] = remaining
        res["remaining_value"] = remaining * best
        if 0 < res["remaining_value"] < mn:
            res["dust"] = True
        if res["dust"]:
            self._event("dust", sym=sym, qty=remaining, value=res["remaining_value"], purpose=purpose)
        return res

    def _mark_closed(self, sym, rec, status, px, qty, gross):
        rec["status"] = status
        rec["closed_ts"] = round(self._now(), 3)
        rec["exit"] = {"px": px, "qty": qty, "sum": gross, "fee": gross * TAKER, "ts": rec["closed_ts"]}
        self._save_state()

    # ------------------------------------------------------------ hooks
    def on_open(self, acc, pos, j):
        sym = acc.m.symbols[pos.sym]
        if self._stopped:
            acc.cancel_position(pos.sym, "live_entry_failed", j)
            return
        with self._lock(sym):
            rec = self.live.get(sym)
            if rec and rec.get("status") in ACTIVE:
                if rec.get("adopt_pending") and rec.get("status") == "open":
                    rec.pop("adopt_pending", None)
                    acc.sync_position(pos.sym, rec["avg_px"], rec["qty"], rec.get("fees", 0.0))
                    self._save_state()
                    return
                self._event("entry_refused_busy", sym=sym, status=rec.get("status"))
                acc.cancel_position(pos.sym, "live_entry_failed", j)
                return
            if sym in (getattr(acc, "disabled_coins", set()) or set()) or sym in self.state.get("disabled_coins", []):
                acc.cancel_position(pos.sym, "live_entry_failed", j)
                return
            plan = pos.plan
            dec = (pos.meta or {}).get("dec") or {}
            target = float(pos.qty) * float(pos.entry_px)
            now = self._now()
            rec = {"sym": sym, "status": "entering", "opened_ts": round(now, 3), "target_tmn": target,
                   "sl_pct": float(plan.sl_pct), "tp_pct": float(plan.tps[0][0]) if plan.tps else DEFAULT_TP_PCT,
                   "time_stop_deadline_ts": round(now + int(plan.time_stop_min or 1440) * 60, 3),
                   "size_tier": dec.get("size_tier"), "thesis": dec.get("thesis"), "qty": 0.0, "avg_px": 0.0,
                   "fees": 0.0, "entry_cid": None, "stop_cid": None, "stop_px": None, "stop_limit_px": None,
                   "server_stop": False, "cids": []}
            self.live[sym] = rec
            self._save_state()
            base = self._base(sym)
            try:
                before = self.client.balances().get(base, (0.0, 0.0, 0.0))[0]
                o1 = self._limit_buy(sym, target, "buy", rec)
                rec["entry_cid"] = o1.get("client_id")
                ex_q, ex_s = float(o1.get("executed_qty", 0)), float(o1.get("executed_sum", 0))
                if 0 < ex_s < TOPUP_BELOW:
                    top = max(target - ex_s, STUCK_GUARD)
                    try:
                        o2 = self._limit_buy(sym, top, "topup", rec)
                        ex_q += float(o2.get("executed_qty", 0))
                        ex_s += float(o2.get("executed_sum", 0))
                    except (OrderRejected, EntryFailed) as e:
                        self._event("topup_failed", sym=sym, error=str(e))
                    except OrderUnknown as e:
                        rec["topup_unknown"] = True
                        self._event("topup_unknown", sym=sym, error=str(e))
                if ex_q <= 0 or ex_s <= 0:
                    raise EntryFailed("no_fill")
                after = self.client.balances().get(base, (0.0, 0.0, 0.0))[0]
            except OrderRejected as e:
                return self._entry_failed(acc, pos, j, sym, f"rejected: {e}", rejected=not isinstance(e, RateLimited))
            except EntryFailed as e:
                return self._entry_failed(acc, pos, j, sym, str(e), rejected=False)
            except Exception as e:                     # OrderUnknown / network after a possible send
                if rec.get("cids"):
                    rec["status"] = "unknown"
                    self._save_state()
                    self._event("entry_unknown", sym=sym, error=repr(e))
                    acc.cancel_position(pos.sym, "live_entry_failed", j)
                    return
                return self._entry_failed(acc, pos, j, sym, repr(e), rejected=False)

            # real qty from the TOTAL balance (fees in base are already netted out)
            delta = after - before
            fallback = ex_q if self.cfg.get("buy_fee_in_base") is False else ex_q * (1 - TAKER)
            qty = self.client.round_qty(sym, min(delta, ex_q) if delta > 0 else fallback)
            avg = ex_s / ex_q
            fees = ex_s * TAKER
            self.state.setdefault("reject_streak", {})[sym] = 0
            if qty * avg < MIN_NOTIONAL:
                rec.update(status="dust", qty=qty, avg_px=avg, fees=fees)
                self._event("entry_dust", sym=sym, qty=qty, value=qty * avg)
                self._archive(sym)
                acc.cancel_position(pos.sym, "live_entry_failed", j)
                return
            sl = float(plan.sl_pct)
            stop_px = avg * (1 - sl / 100)
            rec.update(status="open", qty=qty, avg_px=avg, fees=fees, stop_px=stop_px,
                       stop_limit_px=stop_px * (1 - float(self.cfg.get("stop_limit_offset", 0.015))))
            self._save_state()
            acc.sync_position(pos.sym, avg, qty, fees)
            self._event("entry_filled", sym=sym, qty=qty, avg_px=avg, value=qty * avg, target=target)
            self._place_stop(sym, rec)
            self._save_state()

    def _entry_failed(self, acc, pos, j, sym, why, rejected):
        self._event("entry_failed", sym=sym, why=why, rejected=rejected)
        rs = self.state.setdefault("reject_streak", {})
        if rejected:
            rs[sym] = int(rs.get(sym, 0)) + 1
            if rs[sym] >= 2:
                dc = set(self.state.get("disabled_coins", []))
                dc.add(sym)
                self.state["disabled_coins"] = sorted(dc)
                try:
                    acc.disabled_coins.add(sym)
                except Exception:
                    pass
                self._event("coin_disabled", sym=sym)
        self.live.pop(sym, None)
        acc.cancel_position(pos.sym, "live_entry_failed", j)
        self._save_state()

    def on_close(self, acc, pos, j):
        sym = acc.m.symbols[pos.sym]
        reason = pos.exits[-1][3] if pos.exits else "?"
        if self._stopped:
            self._event("close_ignored_stopped", sym=sym, reason=reason)
            return
        with self._lock(sym):
            rec = self.live.get(sym)
            if rec is None:
                self._event("close_no_live_record", sym=sym, reason=reason)
                return
            st = rec.get("status", "")
            if st.startswith("closed_"):                    # closed outside the engine: numbers only, no 2nd sell
                self._finalize(sym, acc, pos, reason=reason)
                return
            if st not in ("open", "unknown", "exit_failed"):
                self._event("close_unexpected_status", sym=sym, status=st)
                return
            d = self._drop_stop(sym, rec)
            if d == "filled":
                self._finalize(sym, acc, pos, reason=reason)
                return
            if d == "failed":                               # keep the stop; never sell over a live stop
                rec["status"] = "exit_failed"
                self._save_state()
                return
            res = self._sell_ladder(sym, rec["qty"], "exit", 0, 3)
            self._after_sell(sym, rec, res, "closed_by_engine")
            if rec["status"] == "closed_by_engine":
                self._finalize(sym, acc, pos, reason=reason)

    def _after_sell(self, sym, rec, res, closed_status):
        if res["sold_qty"] > 0:
            rec.setdefault("partial_sells", []).append({"qty": res["sold_qty"], "sum": res["sold_sum"]})
        if res["remaining_value"] < MIN_NOTIONAL and not res.get("aborted"):
            sells = rec.get("partial_sells", [])
            q = sum(s["qty"] for s in sells)
            g = sum(s["sum"] for s in sells)
            self._mark_closed(sym, rec, closed_status, g / q if q else 0.0, q, g)
            rec["dust_qty"] = res["remaining_qty"]
        else:
            rec["qty"] = res["remaining_qty"]
            rec["status"] = "exit_failed"
            self._event("exit_incomplete", sym=sym, remaining=res["remaining_qty"], value=res["remaining_value"])
            self._save_state()

    def mark_unknown(self, sym_name: str):
        lk = self._lock(sym_name)
        got = lk.acquire(timeout=5)
        try:
            rec = self.live.get(sym_name)
            if rec is None:
                self.live[sym_name] = {"sym": sym_name, "status": "unknown", "qty": 0.0, "avg_px": 0.0,
                                       "opened_ts": round(self._now(), 3)}
            elif not rec.get("status", "").startswith("closed_"):
                rec["status"] = "unknown"
            self._event("marked_unknown", sym=sym_name)
            self._save_state()
        finally:
            if got:
                lk.release()

    # ------------------------------------------------------------ finalize / trade log
    def _archive(self, sym):
        rec = self.live.pop(sym, None)
        if rec is not None:
            cl = self.state.setdefault("closed", [])
            cl.append(rec)
            del cl[:-100]
        self._save_state()

    def _finalize(self, sym, acc=None, pos=None, reason=None):
        rec = self.live.get(sym)
        if rec is None:
            return
        ex = rec.get("exit") or {}
        sold_q, gross, fee = ex.get("qty", 0.0), ex.get("sum", 0.0), ex.get("fee", 0.0)
        entry_fees = rec.get("fees", 0.0)
        pnl = gross - fee - rec.get("avg_px", 0.0) * sold_q - entry_fees
        why = {"closed_by_server": "SERVER_STOP", "closed_by_client": "CLIENT_STOP",
               "closed_reconcile": "RECONCILE_GONE", "closed_by_flatten": "END"}.get(rec["status"], reason or "?")
        row = {"sym": sym, "opened": tehran_str(rec.get("opened_ts") or 0), "closed": tehran_str(ex.get("ts") or self._now()),
               "qty": rec.get("qty"), "entry_px": rec.get("avg_px"), "exit_px": ex.get("px"), "exit_qty": sold_q,
               "fees": round(entry_fees + fee, 2), "pnl_tmn": round(pnl, 2), "exit_reason": why,
               "paper_reason": reason, "status": rec["status"], "size_tier": rec.get("size_tier"),
               "thesis": rec.get("thesis"), "code_version": self.cfg.get("code_version", "")}
        self._write_trade(row)
        if acc is not None and hasattr(acc, "patch_close") and pos is not None:
            try:
                acc.patch_close(pos.sym, ex.get("px"), sold_q, entry_fees + fee, why)
            except Exception as e:
                self._event("patch_close_error", sym=sym, error=repr(e))
        dust_q = rec.get("dust_qty", 0.0)
        residual = rec.get("residual_qty", 0.0)
        self._archive(sym)
        if residual:                                       # >= 50k left after a client stop: keep selling it
            self.live[sym] = {"sym": sym, "status": "exit_failed", "qty": residual, "avg_px": rec.get("avg_px", 0.0),
                              "fees": 0.0, "stop_cid": None, "server_stop": False, "stop_px": None,
                              "opened_ts": rec.get("opened_ts"), "residual_of": rec.get("entry_cid")}
            self._save_state()
        elif dust_q:
            self._event("dust_left", sym=sym, qty=dust_q)

    def _write_trade(self, row: dict):
        new = not os.path.exists(self.trades_path)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
        for line in buf.getvalue().splitlines():
            append_line_retry(self.trades_path, line)

    # ------------------------------------------------------------ fast tick (separate thread)
    def fast_tick(self):
        if self._stopped:
            return
        try:
            oo = {o.get("client_id"): o for o in self.client.open_orders()}
        except Exception as e:
            self._event("fast_tick_open_orders_error", error=repr(e))
            return
        changed = False
        for sym in list(self.live):
            lk = self._lock(sym)
            if not lk.acquire(blocking=False):            # a hook / flatten is working on it
                continue
            try:
                rec = self.live.get(sym)
                if not rec or self._stopped:
                    continue
                st = rec.get("status")
                if st in ("open", "unknown") and rec.get("qty", 0) > 0:
                    if rec.get("server_stop"):
                        changed |= self._tick_server_stop(sym, rec, oo)
                    if rec.get("status") in ("open", "unknown"):
                        changed |= self._tick_client_stop(sym, rec)
                elif st == "exit_failed" and rec.get("qty", 0) > 0 and not self._flattening:
                    if self._drop_stop(sym, rec) in ("none", "cancelled"):
                        res = self._sell_ladder(sym, rec["qty"], "exit", 1, 1)
                        self._after_sell(sym, rec, res, "closed_by_engine")
                        if rec["status"] == "closed_by_engine":
                            self._finalize(sym)
                    changed = True
            except Exception as e:
                self._event("fast_tick_error", sym=sym, error=repr(e))
            finally:
                lk.release()
        if changed:
            self._save_state()

    def _tick_server_stop(self, sym, rec, oo) -> bool:
        cid = rec.get("stop_cid")
        if cid and cid in oo:
            o = oo[cid]
            want = self.client.round_qty(sym, rec["qty"])
            have = o.get("orig_qty", 0.0) - o.get("executed_qty", 0.0)
            step = 10 ** -int(self.client.rules(sym).get("step_dec", 8))
            if abs(have - want) <= step * 1.5 or o.get("executed_qty", 0) > 0:
                return False
            self._event("stop_qty_mismatch", sym=sym, have=have, want=want)
            if self._drop_stop(sym, rec) != "cancelled":
                return True
            self._place_stop(sym, rec)
            return True
        o = None
        if cid:
            try:
                o = self.client.get_order(cid)
            except WallexError:
                return False
            if o is not None and o.get("executed_qty", 0) > 0:
                ex_q, ex_s = o["executed_qty"], o["executed_sum"]
                px = ex_s / ex_q
                rest = max(rec["qty"] - ex_q, 0.0)
                if o.get("status") == FILLED or (is_done(o) and rest * px < MIN_NOTIONAL):
                    self._mark_closed(sym, rec, "closed_by_server", px, ex_q, ex_s)
                    self._queue_paper_close(sym, px, ex_q, "SERVER_STOP")
                    return True
                if not is_done(o):
                    return False                           # partially filled, still working
                rec["qty"] = rest
                rec.setdefault("partial_sells", []).append({"qty": ex_q, "sum": ex_s, "src": "stop"})
            elif o is not None and not is_done(o):
                return False                               # alive, just not in the open-orders page
        bals = self.client.balances()
        excess, free = self._excess(bals, self._base(sym))
        if excess * float(rec.get("stop_px") or 0) < MIN_NOTIONAL:     # coins are gone -> do not re-place
            bid = self._best(sym, "bid")
            self._mark_closed(sym, rec, "closed_reconcile", bid, rec["qty"], bid * rec["qty"])
            rec["exit"]["estimated"] = True
            self._queue_paper_close(sym, bid, rec["qty"], "RECONCILE_GONE")
            return True
        self._event("stop_missing_replace", sym=sym, cid=cid, status=(o or {}).get("status"))
        rec["stop_cid"] = None
        self._place_stop(sym, rec, max_qty=free)
        return True

    def _tick_client_stop(self, sym, rec) -> bool:
        stop_px = rec.get("stop_px")
        if not stop_px:
            return False
        bid = self._best(sym, "bid")
        if bid <= 0 or bid > stop_px:
            return False
        if rec.get("server_stop") and rec.get("stop_cid") and bid >= (rec.get("stop_limit_px") or 0):
            return False                                   # the server stop is in charge
        d = self._drop_stop(sym, rec)
        if d == "filled":
            return True
        if d == "failed":
            return True
        self._event("client_stop_triggered", sym=sym, bid=bid, stop_px=stop_px)
        res = self._sell_ladder(sym, rec["qty"], "cstop", 0, 3)
        if res["sold_qty"] <= 0:
            return True
        rec.setdefault("partial_sells", []).append({"qty": res["sold_qty"], "sum": res["sold_sum"]})
        sells = rec["partial_sells"]
        q, g = sum(s["qty"] for s in sells), sum(s["sum"] for s in sells)
        px = g / q
        self._mark_closed(sym, rec, "closed_by_client", px, q, g)
        rec["dust_qty"] = res["remaining_qty"] if res["remaining_value"] < MIN_NOTIONAL else 0.0
        if res["remaining_value"] >= MIN_NOTIONAL:          # residual: keep selling it as a separate record
            self._event("client_stop_residual", sym=sym, qty=res["remaining_qty"])
            rec["residual_qty"] = res["remaining_qty"]
        self._queue_paper_close(sym, px, q, "CLIENT_STOP")
        return True

    # ------------------------------------------------------------ reconcile (main loop: start + every 5 min)
    def reconcile(self):
        if self._stopped:
            return
        main = self._is_main()
        try:
            bals = self.client.balances()
            oo = self.client.open_orders()
            mk = self.client.markets()
        except Exception as e:
            self._event("reconcile_error", error=repr(e))
            return

        def bid_of(sym):
            v = mk.get(sym) or {}
            return float(v.get("bid") or v.get("last") or 0.0)

        # 1) open orders: relink c7- orders, only log foreign ones (never cancel on reconcile)
        known = {r.get("stop_cid") for r in self.live.values()}
        for o in oo:
            cid = o.get("client_id") or ""
            if not cid.startswith("c7-"):
                self._event("foreign_open_order", cid=cid, sym=o.get("symbol"), side=o.get("side"))
                continue
            if cid in known:
                continue
            rec = self.live.get(o.get("symbol"))
            if rec and o.get("side") == "SELL" and "STOP" in o.get("type", "") and not rec.get("stop_cid"):
                rec["stop_cid"], rec["server_stop"] = cid, True
                self._event("stop_relinked", sym=o.get("symbol"), cid=cid)
            else:
                self._event("c7_order_unlinked", cid=cid, sym=o.get("symbol"), type=o.get("type"))
        open_syms = {o.get("symbol") for o in oo}

        # 2) recorded positions vs TOTAL balances
        paper = {}
        if main:
            try:
                paper = {self.acc.m.symbols[s]: s for s in self.acc.positions}
            except Exception:
                paper = {}
        for sym in list(self.live):
            lk = self._lock(sym)
            if not lk.acquire(timeout=30):
                continue
            try:
                rec = self.live.get(sym)
                if rec is None:
                    continue
                st = rec.get("status", "")
                excess, _free = self._excess(bals, self._base(sym))
                bid = bid_of(sym)
                val = excess * bid
                if st == "open":
                    if val < MIN_NOTIONAL and sym not in open_syms:
                        so = None
                        if rec.get("stop_cid"):
                            try:
                                so = self.client.get_order(rec["stop_cid"])
                            except WallexError:
                                so = None
                        if so is not None and so.get("executed_qty", 0) > 0:
                            px = so["executed_sum"] / so["executed_qty"]
                            self._mark_closed(sym, rec, "closed_by_server", px, so["executed_qty"], so["executed_sum"])
                            self._queue_paper_close(sym, px, so["executed_qty"], "SERVER_STOP")
                            continue
                        q = rec.get("qty", 0.0)
                        self._mark_closed(sym, rec, "closed_reconcile", bid, q, bid * q)
                        rec["exit"]["estimated"] = True
                        self._queue_paper_close(sym, bid, q, "RECONCILE_GONE")
                        continue
                    self._sync_qty(sym, rec, excess, main, paper)
                elif st in ("unknown", "entering"):
                    if val >= MIN_NOTIONAL:
                        rec["status"] = "open"
                        if not rec.get("avg_px"):
                            rec["avg_px"] = bid
                        if not rec.get("stop_px"):
                            rec["sl_pct"] = rec.get("sl_pct") or DEFAULT_SL_PCT
                            rec["stop_px"] = bid * (1 - rec["sl_pct"] / 100)
                            rec["stop_limit_px"] = rec["stop_px"] * (1 - float(self.cfg.get("stop_limit_offset", 0.015)))
                        rec["qty"] = self.client.round_qty(sym, excess)
                        self._event("unknown_resolved_open", sym=sym, qty=rec["qty"])
                        if main and sym not in paper:
                            self._queue_adoption(sym, rec, source="reconcile")
                        if not rec.get("server_stop"):
                            self._place_stop(sym, rec)
                    else:
                        self._event("unknown_resolved_gone", sym=sym)
                        rec["status"] = "closed_reconcile"
                        if main and sym in paper:
                            self._queue_paper_close(sym, bid, 0.0, "RECONCILE_GONE")
                        self._archive(sym)
                elif st == "exit_failed" and val < MIN_NOTIONAL:
                    self._event("exit_failed_resolved", sym=sym)
                    self._archive(sym)
                elif st.startswith("closed_") and self._now() - float(rec.get("closed_ts") or 0) > 600:
                    self._finalize(sym)                     # the engine never picked it up
            finally:
                lk.release()

        # 3) unrecorded C7 coins bought by the bot (above the start snapshot) -> adopt with default stop
        if not self._flattening:
            for sym in self.coins:
                if sym in self.live:
                    continue
                excess, _ = self._excess(bals, self._base(sym))
                bid = bid_of(sym)
                if excess * bid < ADOPT_MIN:
                    continue
                with self._lock(sym):
                    stop_px = bid * (1 - DEFAULT_SL_PCT / 100)
                    end = self.cfg.get("flatten_start_ts") or (self._now() + 1440 * 60)
                    rec = {"sym": sym, "status": "open", "qty": self.client.round_qty(sym, excess), "avg_px": bid,
                           "fees": 0.0, "entry_cid": None, "stop_cid": None, "stop_px": stop_px,
                           "stop_limit_px": stop_px * (1 - float(self.cfg.get("stop_limit_offset", 0.015))),
                           "server_stop": False, "sl_pct": DEFAULT_SL_PCT, "tp_pct": DEFAULT_TP_PCT,
                           "time_stop_deadline_ts": end, "size_tier": None, "opened_ts": round(self._now(), 3),
                           "adopted": True}
                    self.live[sym] = rec
                    self._event("adopted_unrecorded", sym=sym, qty=rec["qty"], bid=bid)
                    self._place_stop(sym, rec)
                    if not main or sym not in paper:
                        self._queue_adoption(sym, rec, source="reconcile")

        # 4) paper positions with no live record and no coins -> close them on paper
        if main:
            for sym, s in paper.items():
                if sym in self.live or sym in self._adopt_queued:
                    continue
                excess, _ = self._excess(bals, self._base(sym))
                if excess * bid_of(sym) < MIN_NOTIONAL:
                    self._queue_paper_close(sym, bid_of(sym), 0.0, "RECONCILE_GONE")
        self._save_state()

    def _sync_qty(self, sym, rec, excess, main, paper):
        q = self.client.round_qty(sym, excess)
        step = 10 ** -int(self.client.rules(sym).get("step_dec", 8))
        if abs(q - rec["qty"]) > step * 1.5:
            self._event("qty_resynced", sym=sym, old=rec["qty"], new=q)
            rec["qty"] = q
            if main and sym in paper:
                try:
                    self.acc.sync_position(paper[sym], rec["avg_px"], q, rec.get("fees", 0.0))
                except Exception as e:
                    self._event("sync_error", sym=sym, error=repr(e))
        if main and sym not in paper and sym not in self._adopt_queued:
            self._queue_adoption(sym, rec, source="reconcile")
        elif main and sym in paper:
            rec.pop("adopt_pending", None)

    # ------------------------------------------------------------ flatten (balance based, engine independent)
    def start_flatten_loop(self):
        if self._flat_thread is not None and self._flat_thread.is_alive():
            return
        self._flattening = True
        self._flat_evt.clear()

        def loop():
            p = 0
            while not self._stopped:
                try:
                    done = self._flatten_pass(p)
                except Exception as e:
                    self._event("flatten_error", error=repr(e))
                    done = False
                p += 1
                dl = self.cfg.get("hard_deadline_ts")
                if done or (dl and self._now() >= dl):
                    break
                if self._flat_evt.wait(float(self.cfg.get("flatten_interval_s", 60))):
                    break

        self._flat_thread = threading.Thread(target=loop, name="flatten", daemon=True)
        self._flat_thread.start()

    def final_flatten(self) -> dict:
        self._flattening = True
        self._flat_evt.set()
        if self._flat_thread is not None and self._flat_thread.is_alive():
            self._flat_thread.join(timeout=120)
        n0 = len(self.flatten_report["passes"])
        for p in range(len(self.cfg.get("exit_slip_ladder") or [0])):
            if self._stopped or self._flatten_pass(n0 + p):
                break
        self.flatten_report["final"] = True
        self._write_flatten_report()
        return self.flatten_report

    def _flatten_pass(self, p: int) -> bool:
        if self._stopped:
            return False
        ladder = list(self.cfg.get("exit_slip_ladder") or [0.008])
        rep = {"pass": p, "ts": round(self._now(), 3), "tehran": tehran_str(self._now()), "cancelled": [],
               "sells": [], "dust": [], "errors": []}
        # cancel every c7- order (stops first, via their records)
        for sym in list(self.live):
            with self._lock(sym):
                rec = self.live.get(sym)
                if rec and rec.get("stop_cid"):
                    cid = rec["stop_cid"]
                    r = self._drop_stop(sym, rec)
                    rep["cancelled"].append({"sym": sym, "cid": cid, "result": r})
        try:
            for o in self.client.open_orders():
                cid = o.get("client_id") or ""
                if cid.startswith("c7-"):
                    ok, _ = self._cancel_confirm(cid)
                    rep["cancelled"].append({"sym": o.get("symbol"), "cid": cid, "result": ok})
            bals = self.client.balances()
        except Exception as e:
            rep["errors"].append(repr(e))
            self.flatten_report["passes"].append(rep)
            self._write_flatten_report()
            return False
        done = True
        for sym in sorted(set(self.coins) | set(self.live)):
            base = self._base(sym)
            excess, _ = self._excess(bals, base)
            if excess <= 0:
                continue
            with self._lock(sym):
                try:
                    bid = self._best(sym, "bid")
                except Exception as e:
                    rep["errors"].append(f"{sym}: {e!r}")
                    done = False
                    continue
                if excess * bid < MIN_NOTIONAL:
                    rep["dust"].append({"sym": sym, "qty": excess, "value": excess * bid})
                    continue
                res = self._sell_ladder(sym, excess, "flat", min(p, len(ladder) - 1), 3)
                rep["sells"].append(res)
                rec = self.live.get(sym)
                prior = rec.get("status") if rec else None
                if rec and prior in ACTIVE:
                    if res["sold_qty"] > 0:
                        rec.setdefault("partial_sells", []).append({"qty": res["sold_qty"], "sum": res["sold_sum"]})
                    if res["remaining_value"] < MIN_NOTIONAL:
                        sells = rec.get("partial_sells", [])
                        q, g = sum(s["qty"] for s in sells), sum(s["sum"] for s in sells)
                        self._mark_closed(sym, rec, "closed_by_flatten", g / q if q else bid, q, g)
                        if prior == "exit_failed":           # paper side already closed by the engine
                            self._finalize(sym)
                        else:
                            self._queue_paper_close(sym, g / q if q else bid, q, "END")
                    else:
                        rec["qty"] = res["remaining_qty"]
                if res["remaining_value"] >= MIN_NOTIONAL:
                    done = False
        rep["untouched_initial"] = self.state.get("initial_balances", {})
        self.flatten_report["passes"].append(rep)
        self.flatten_report["done"] = done
        self._write_flatten_report()
        self._save_state()
        return done

    def _write_flatten_report(self):
        write_text_retry(self.flatten_path, json.dumps(self.flatten_report, ensure_ascii=False, indent=1, default=str))

    # ------------------------------------------------------------ equity
    def live_equity(self) -> float:
        """Σ TOTAL balance × bid + TMN total (cached 5 s). Raises on API failure (engine falls back to paper)."""
        with self._eq_lock:
            ts, val = self._eq_cache
            if val is not None and self._now() - ts < 5:
                return val
            bals = self.client.balances()
            mk = self.client.markets()
            eq = 0.0
            for asset, (total, _free, _locked) in bals.items():
                if asset == "TMN":
                    eq += total
                    continue
                v = mk.get(asset + "TMN") or {}
                eq += total * float(v.get("bid") or v.get("last") or 0.0)
            self._eq_cache = (self._now(), eq)
            return eq
