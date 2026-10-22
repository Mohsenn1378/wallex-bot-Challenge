"""
P0-09 - REAL-ORDER smoke test on Wallex (Appendix D of docs/IMPROVEMENT_PROPOSAL.md).

!!! This script sends REAL orders with REAL money (~60k TMN round trip, cost ~800-1200 TMN). !!!
It refuses to do anything without --yes, and must only be run with Mohsen's explicit approval at that moment.

Part 1 (raw client):  python tools/live_smoke.py --symbol PEPETMN --tmn 60000 --yes
Part 2 (executor):    python tools/live_smoke.py --executor --symbol PEPETMN --tmn 65000 --yes

Outputs: runs/smoke/<ts>.json (+ <ts>_orders.jsonl without keys) and, after a complete part 1,
config/broker.yaml with boolean/number values only (undetermined -> null / safe false).
The API key is read from WALLEX_API_KEY (env or .env) and is never printed or logged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wallex_agent.live.wallex_client import (WallexClient, OrderRejected, OrderUnknown, WallexError, load_key,
                                             is_done, fmt_price, _f)
from wallex_agent.live.executor import LiveExecutor, load_broker_cfg, base36, write_text_retry
from wallex_agent.sim.fill_model import TAKER
from wallex_agent.sim.position import Plan, Position

BROKER = os.path.join(ROOT, "config", "broker.yaml")
SMOKE_DIR = os.path.join(ROOT, "runs", "smoke")
STOP_FIELDS = ["stop_Price", "stopPrice", "stop_price"]
CANCEL_METHODS = ["query", "body", "path"]
LADDER = [0.008, 0.012, 0.02, 0.03]


def now_tag():
    return time.strftime("%Y%m%d_%H%M%S")


def say(*a):
    print(*a, flush=True)


# ====================================================================== part 1 helpers
class Smoke:
    def __init__(self, client: WallexClient, sym: str, out: dict):
        self.c, self.sym, self.out = client, sym, out
        self.epoch = base36(int(time.time()))
        self.seq = 0
        self.r = client.rules(sym)
        self.base = self.r.get("base") or sym[:-3]
        self.our_cids: list[str] = []

    def cid(self, purpose):
        self.seq += 1
        cid = f"c7-{self.epoch}-{self.sym}-{purpose}-{self.seq}"   # same format/length as the live run
        self.our_cids.append(cid)
        return cid

    def raw_balance(self, asset):
        status, d = self.c._get("/v1/account/balances", private=True)
        v = ((d or {}).get("result") or {}).get("balances", {}).get(asset) or {}
        return _f(v.get("value")), _f(v.get("locked"))

    def book(self):
        return self.c.depth(self.sym)

    def wait_order(self, cid, secs=30.0):
        o = None
        t_end = time.time() + secs
        while time.time() < t_end:
            o = self.c.get_order(cid)
            if is_done(o) and o is not None:
                return o
            time.sleep(1)
        return o

    def gone(self, cid):
        o = self.c.get_order(cid)
        still = [x for x in self.c.open_orders(self.sym) if x.get("client_id") == cid]
        return (o is None or is_done(o)) and not still

    def cancel_any(self, cid, methods=CANCEL_METHODS):
        """Try cancel methods in order until the order is confirmed inactive. Returns the working method or None."""
        for m in methods:
            try:
                self.c.cancel_order(cid, method=m)
            except WallexError as e:
                self.out.setdefault("cancel_errors", []).append({"cid": cid, "method": m, "error": str(e)})
            for _ in range(5):
                time.sleep(1)
                if self.gone(cid):
                    return m
        return None

    def sell_all_excess(self, snapshot_base: float, label="final_sell"):
        """LIMIT SELL everything above the start snapshot on the bids with the ladder."""
        res = []
        for k, slip in enumerate(LADDER):
            value, locked = self.raw_balance(self.base)
            bals = self.c.balances()
            total, free, _ = bals.get(self.base, (0.0, 0.0, 0.0))
            qty = self.c.round_qty(self.sym, min(max(total - snapshot_base, 0.0), free))
            bid = self.book()["bid"][0][0]
            px = self.c.round_price(self.sym, bid * (1 - slip), "SELL")
            if qty * px < self.r.get("min_notional", 50_000):
                res.append({"stop": "below_min_notional", "qty": qty, "value": qty * bid})
                break
            cid = self.cid("sell")
            try:
                self.c.create_order(self.sym, "SELL", "LIMIT", qty, price=px, client_id=cid)
            except OrderRejected as e:
                res.append({"cid": cid, "rejected": str(e)})
                continue
            o = self.wait_order(cid, 15)
            if o is not None and not is_done(o):
                self.cancel_any(cid, [self.out.get("cancel_method") or "query"] + CANCEL_METHODS)
                o = self.c.get_order(cid)
            res.append({"cid": cid, "slip": slip, "px": px, "qty": qty, "order": o})
        self.out[label] = res
        return res


def part1(client: WallexClient, args, out: dict) -> dict:
    sym = args.symbol
    found = {"stop_supported": False, "stop_field": None, "limit_rel_ok": None, "balance_value_includes_locked": None,
             "dup_id_rejected": None, "cancel_method": None, "buy_fee_in_base": None}
    out["found"] = found

    # 1) market rules
    mk = client.markets()
    out["rules"] = {s: mk.get(s) for s in (sym, "BTCTMN")}
    say("1) rules:", out["rules"][sym])
    S = Smoke(client, sym, out)

    # 2) balances + snapshot
    bals = client.balances()
    out["balances_start"] = {a: list(v) for a, v in bals.items()}
    tmn_free = bals.get("TMN", (0, 0, 0))[1]
    if tmn_free < 150_000:
        raise SystemExit(f"ABORT: free TMN {tmn_free:.0f} < 150,000")
    snap_base = bals.get(S.base, (0.0, 0.0, 0.0))[0]
    out["snapshot_base"] = snap_base
    sample = S.cid("buy")
    out["client_id_sample"] = {"id": sample, "len": len(sample)}
    say("2) TMN free", round(tmn_free), "| client_id sample", sample, len(sample))

    try:
        # 3) MARKET BUY (expected to be rejected: isMarketTypeEnable=false)
        ask = S.book()["ask"][0][0]
        q = client.round_qty(sym, 55_000 / ask)
        try:
            o = client.create_order(sym, "BUY", "MARKET", q, client_id=S.cid("mkt"))
            out["market_buy"] = {"accepted": True, "order": o}
            say("3) MARKET BUY was ACCEPTED (unexpected) - coins will be sold at the end")
            time.sleep(3)
        except OrderRejected as e:
            out["market_buy"] = {"accepted": False, "error": str(e)}
            say("3) MARKET BUY rejected as expected:", str(e)[:120])

        # 4) LIMIT BUY ~tmn walked on the asks (cap +0.4%)
        base_before = client.balances().get(S.base, (0.0, 0.0, 0.0))[0]
        asks = S.book()["ask"]
        cap = asks[0][0] * 1.004
        need, acc = asks[0][0], 0.0
        for px, qq in asks:
            if px > cap:
                break
            need, acc = px, acc + px * qq
            if acc >= args.tmn:
                break
        px = client.round_price(sym, need if acc >= args.tmn else cap, "BUY")
        q = client.round_qty(sym, args.tmn / px)
        cid = S.cid("buy")
        client.create_order(sym, "BUY", "LIMIT", q, price=px, client_id=cid)
        o = S.wait_order(cid, 30)
        if o is not None and not is_done(o):
            S.cancel_any(cid)
            o = client.get_order(cid)
        out["limit_buy"] = {"cid": cid, "px": px, "qty": q, "order": o}
        ex_q = (o or {}).get("executed_qty", 0.0)
        ex_s = (o or {}).get("executed_sum", 0.0)
        if ex_q <= 0:
            raise RuntimeError("limit buy did not fill")
        avg = ex_s / ex_q
        time.sleep(2)
        base_after = client.balances().get(S.base, (0.0, 0.0, 0.0))[0]
        delta = base_after - base_before
        found["buy_fee_in_base"] = bool(delta < ex_q * (1 - TAKER / 3))
        try:
            out["account_trades"] = client.account_trades(sym)[:5]
        except WallexError as e:
            out["account_trades_error"] = str(e)
        say(f"4) bought {ex_q} @ {avg:.6g} (sum {ex_s:.0f}); balance delta {delta} -> fee in base: {found['buy_fee_in_base']}")

        # 4b) duplicate client_id
        bid = S.book()["bid"][0][0]
        dpx = client.round_price(sym, bid * 0.97, "BUY")
        dq = client.round_qty(sym, 51_000 / dpx)
        step = 10 ** -int(S.r["step_dec"])
        while dq * dpx < 50_500:
            dq = client.round_qty(sym, dq + step)
        dcid = S.cid("dup")
        try:
            client.create_order(sym, "BUY", "LIMIT", dq, price=dpx, client_id=dcid)
            first_ok = True
        except (OrderRejected, OrderUnknown) as e:
            first_ok = False
            out["dup_first_error"] = str(e)             # undetermined -> stays null (safe: no re-send)
        if first_ok:
            try:
                client.create_order(sym, "BUY", "LIMIT", dq, price=dpx, client_id=dcid)
                found["dup_id_rejected"] = False
            except OrderRejected as e:
                found["dup_id_rejected"] = True
                out["dup_error"] = str(e)
            except OrderUnknown:
                found["dup_id_rejected"] = None
        for _ in range(4):                       # cancel both (same id) until none is open
            if S.gone(dcid):
                break
            S.cancel_any(dcid)
        say("4b) dup_id_rejected:", found["dup_id_rejected"])

        # 5) stop protocol (safest first: limit ABOVE market -> if the stop field is ignored nothing fills)
        held = client.round_qty(sym, max(client.balances().get(S.base, (0, 0, 0))[0] - snap_base, 0.0))
        ref = S.book()["bid"][0][0]
        out["stop_attempts"] = []
        for field in STOP_FIELDS:
            for lim_mult in (1.03, 0.95 * 0.985):
                stop = ref * 0.95
                lim = ref * lim_mult
                scid = S.cid("stp")
                att = {"field": field, "stop": stop, "limit": lim, "cid": scid}
                try:
                    client.create_order(sym, "SELL", "STOP_LIMIT", held, price=lim, stop_price=stop,
                                        client_id=scid, stop_field=field)
                except OrderRejected as e:
                    att["rejected"] = str(e)
                    out["stop_attempts"].append(att)
                    continue                            # retry once with limit = stop * 0.985
                o1 = client.get_order(scid)
                sent_stop = fmt_price(stop, S.r["tick_dec"], "BUY")
                ok = (o1 is not None and "STOP" in (o1.get("type") or "") and o1.get("stop_price") is not None
                      and fmt_price(o1["stop_price"], S.r["tick_dec"], "BUY") == sent_stop
                      and o1.get("status") != "FILLED")
                if ok:
                    time.sleep(10)
                    o2 = client.get_order(scid)
                    ok = o2 is not None and o2.get("status") != "FILLED" and o2.get("executed_qty", 0) == 0
                    att["after_10s"] = o2
                att.update(order=o1, confirmed=ok)
                out["stop_attempts"].append(att)
                if o1 is not None and o1.get("executed_qty", 0) > 0:
                    att["filled_immediately"] = True        # field ignored: plain limit sell executed
                    break
                m = S.cancel_any(scid)
                att["cancelled_with"] = m
                time.sleep(1)
                v, l = S.raw_balance(S.base)
                att["balance_after_cancel"] = {"value": v, "locked": l}
                if ok:
                    found["stop_field"] = field
                    found["stop_supported"] = True
                break                                       # next field
            if found["stop_supported"] or any(a.get("filled_immediately") for a in out["stop_attempts"]):
                break
        say("5) stop field confirmed:", found["stop_field"])

        # 5b) real-config stop (stop = px*0.95, limit = stop*0.985) + locked-balance semantics
        held = client.round_qty(sym, max(client.balances().get(S.base, (0, 0, 0))[0] - snap_base, 0.0))
        if found["stop_supported"] and held > 0:
            ref = S.book()["bid"][0][0]
            stop, lim = ref * 0.95, ref * 0.95 * 0.985
            v0, l0 = S.raw_balance(S.base)
            rcid = S.cid("stp")
            try:
                client.create_order(sym, "SELL", "STOP_LIMIT", held, price=lim, stop_price=stop, client_id=rcid,
                                    stop_field=found["stop_field"])
                time.sleep(2)
                o = client.get_order(rcid)
                out["stop_real"] = o
                if o is None or o.get("executed_qty", 0) > 0 or o.get("status") == "FILLED":
                    found["stop_supported"] = False         # filled at once -> the field is ignored
                    found["limit_rel_ok"] = None
                else:
                    found["limit_rel_ok"] = True
                    v1, l1 = S.raw_balance(S.base)
                    out["locked_probe"] = {"before": [v0, l0], "after": [v1, l1]}
                    if l1 > l0 + held * 0.5:
                        found["balance_value_includes_locked"] = bool(abs(v1 - v0) < held * 0.5)
                    S.cancel_any(rcid)
            except OrderRejected as e:
                found["limit_rel_ok"] = False
                out["stop_real_error"] = str(e)
        say("5b) stop_supported:", found["stop_supported"], "| limit_rel_ok:", found["limit_rel_ok"])

        # 6) cancel method (query -> body -> path) on a non-marketable LIMIT SELL; also probes `locked`
        held = client.round_qty(sym, max(client.balances().get(S.base, (0, 0, 0))[0] - snap_base, 0.0))
        ask = S.book()["ask"][0][0]
        cpx = client.round_price(sym, ask * 1.025, "SELL")
        if held * cpx >= 50_000:
            v0, l0 = S.raw_balance(S.base)
            ccid = S.cid("cxl")
            client.create_order(sym, "SELL", "LIMIT", held, price=cpx, client_id=ccid)
            time.sleep(2)
            v1, l1 = S.raw_balance(S.base)
            out["locked_probe_limit"] = {"before": [v0, l0], "after": [v1, l1]}
            if found["balance_value_includes_locked"] is None and l1 > l0 + held * 0.5:
                found["balance_value_includes_locked"] = bool(abs(v1 - v0) < held * 0.5)
            for m in CANCEL_METHODS:
                if S.cancel_any(ccid, [m]) == m:
                    found["cancel_method"] = m
                    break
            time.sleep(1)
            v2, l2 = S.raw_balance(S.base)
            out["after_cancel"] = [v2, l2]
        say("6) cancel_method:", found["cancel_method"], "| value includes locked:",
            found["balance_value_includes_locked"])
        out["cancel_method"] = found["cancel_method"]
    finally:
        # 7) sell everything bought (always attempted, also after an error) and cancel leftovers
        for o in client.open_orders(sym):
            if o.get("client_id") in S.our_cids:
                S.cancel_any(o["client_id"])
        S.sell_all_excess(snap_base)
        # 8) final state
        out["balances_end"] = {a: list(v) for a, v in client.balances().items()}
        out["open_orders_end"] = [o for o in client.open_orders() if o.get("client_id") in S.our_cids]
    return found


def write_broker(found: dict):
    def b(v):
        return "null" if v is None else ("true" if v is True else "false")

    text = f"""stop_supported: {b(found.get('stop_supported') is True)}            # boolean only; set by tools/live_smoke.py
stop_type: STOP_LIMIT
stop_field: {found.get('stop_field') or 'stop_Price'}
limit_rel_ok: {b(found.get('limit_rel_ok') is True)}
balance_value_includes_locked: {b(found.get('balance_value_includes_locked'))}
dup_id_rejected: {b(found.get('dup_id_rejected'))}
cancel_method: {found.get('cancel_method') or 'query'}
buy_fee_in_base: {b(found.get('buy_fee_in_base'))}
max_entry_slip: 0.004
max_limit_distance: 0.03
exit_slip_ladder: [0.008, 0.012, 0.02, 0.03]
poll_s: 1
fill_wait_s: 15
# written by tools/live_smoke.py at {time.strftime('%Y-%m-%d %H:%M:%S')}
"""
    write_text_retry(BROKER, text)


# ====================================================================== part 2 (executor)
class SmokeAccount:
    """Tiny engine stand-in implementing the Account contract the executor uses."""

    def __init__(self, sym):
        self.m = SimpleNamespace(symbols=[sym], t=[int(time.time())])
        self.positions, self.cash, self.start_cash = {}, 0.0, 0.0
        self.hooks, self.equity_fn, self.disabled_coins = None, None, set()
        self.gov_state, self.cooldown_until, self.entries_hist, self.flatten_mode = "normal", {}, deque(), False
        self.allowed = {sym}
        self.events = []

    def sync_position(self, s, px, qty, fees):
        p = self.positions[s]
        p.entry_px, p.qty, p.qty_initial, p.fees = px, qty, qty, fees
        self.events.append(("sync", px, qty))

    def cancel_position(self, s, reason, j):
        self.positions.pop(s, None)
        self.events.append(("cancel", reason))

    def close_external(self, s, j, px, reason):
        p = self.positions.pop(s, None)
        if p is None:
            return
        p.exits.append((j, px, p.qty, reason, px * p.qty * TAKER))
        p.qty = 0.0
        self.events.append(("close", reason))
        if self.hooks:
            self.hooks.on_close(self, p, j)

    def adopt_position(self, s, qty, px, j, plan):
        self.events.append(("adopt", qty, px))


def part2(key, args, out):
    cfg = load_broker_cfg(BROKER, {"coins": [args.symbol]})
    if cfg.get("stop_supported") not in (True, False):
        raise SystemExit("ABORT: config/broker.yaml has no boolean stop_supported - run part 1 first")
    run_dir = os.path.join(SMOKE_DIR, "exec_" + now_tag())
    client = WallexClient(key, cfg, os.path.join(run_dir, "orders.jsonl"))
    os.makedirs(run_dir, exist_ok=True)
    acc = SmokeAccount(args.symbol)
    ex = LiveExecutor(acc, client, cfg, run_dir)
    acc.hooks = ex
    out["run_dir"] = run_dir
    try:
        ex.start(thread=False)
        ask = client.depth(args.symbol)["ask"][0][0]
        qty = args.tmn / ask
        pos = Position(0, qty, ask, 0, Plan(sl_pct=5.0, tps=[(6.0, 1.0)], time_stop_min=60), ask * 0.95,
                       fees=args.tmn * TAKER, meta={"dec": {"size_tier": "probe", "thesis": "smoke"}})
        pos.qty_initial = qty
        acc.positions[0] = pos
        ex.on_open(acc, pos, 0)
        out["after_open"] = {"live": json.loads(json.dumps(ex.live, default=str)), "events": list(acc.events)}
        say("enter:", ex.live.get(args.symbol, {}).get("status"), "| server_stop:",
            ex.live.get(args.symbol, {}).get("server_stop"))
        if 0 not in acc.positions:
            raise RuntimeError("entry failed: " + str(acc.events))
        ex.fast_tick()
        time.sleep(15)
        ex.fast_tick()
        out["paper_closes_during_ticks"] = ex.pop_paper_closes()
        bid = client.depth(args.symbol)["bid"][0][0]
        if 0 in acc.positions:
            acc.close_external(0, 1, bid, "TIME")
        out["after_close"] = {"live": json.loads(json.dumps(ex.live, default=str)), "events": list(acc.events)}
        say("exit done; live records left:", list(ex.live))
    finally:
        left = [o for o in client.open_orders(args.symbol) if (o.get("client_id") or "").startswith("c7-" + ex.epoch)]
        if left or any(r.get("status") in ("open", "unknown", "exit_failed") for r in ex.live.values()):
            say("cleanup: balance-based flatten of what this smoke bought")
            out["cleanup_flatten"] = ex.final_flatten()
        ex.stop()
        out["open_orders_end"] = [o for o in client.open_orders(args.symbol)
                                  if (o.get("client_id") or "").startswith("c7-" + ex.epoch)]
        with open(os.path.join(run_dir, "orders.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        posts = [r for r in rows if r.get("method") == "POST"]
        out["verify"] = {
            "buys": sum(1 for r in posts if (r.get("body") or {}).get("side") == "BUY"),
            "stops": sum(1 for r in posts if "STOP" in str((r.get("body") or {}).get("type"))),
            "sells": sum(1 for r in posts if (r.get("body") or {}).get("side") == "SELL"
                         and (r.get("body") or {}).get("type") == "LIMIT"),
            "cancels": sum(1 for r in rows if r.get("method") == "DELETE"),
            "open_orders_left": len(out["open_orders_end"]),
        }
        say("verify:", out["verify"])


# ====================================================================== main
def main(argv=None):
    ap = argparse.ArgumentParser(description="REAL-ORDER Wallex smoke test (P0-09). Needs --yes.")
    ap.add_argument("--symbol", default="PEPETMN")
    ap.add_argument("--tmn", type=float, default=60_000)
    ap.add_argument("--executor", action="store_true", help="part 2: executor enter/stop/fast_tick/exit")
    ap.add_argument("--yes", action="store_true", help="I (Mohsen) approve sending REAL orders right now")
    ap.add_argument("--no-write-broker", action="store_true")
    args = ap.parse_args(argv)
    if not args.yes:
        say("REFUSED: this sends REAL orders on Wallex. Re-run with --yes only after explicit approval.")
        return 2
    if not (50_000 < args.tmn <= 120_000):
        say("REFUSED: --tmn must be in (50,000, 120,000]")
        return 2
    key = load_key(ROOT)
    if not key:
        say("REFUSED: WALLEX_API_KEY not found (env or .env)")
        return 2
    os.makedirs(SMOKE_DIR, exist_ok=True)
    tag = now_tag()
    out = {"ts": tag, "symbol": args.symbol, "tmn": args.tmn, "part": 2 if args.executor else 1}
    rc = 0
    try:
        if args.executor:
            part2(key, args, out)
        else:
            client = WallexClient(key, load_broker_cfg(BROKER), os.path.join(SMOKE_DIR, f"{tag}_orders.jsonl"))
            found = part1(client, args, out)
            clean = not out.get("open_orders_end")
            out["broker_written"] = bool(clean and not args.no_write_broker)
            if out["broker_written"]:
                write_broker(found)
            say("found:", found, "| broker.yaml written:", out["broker_written"])
    except BaseException as e:                    # record everything, never hide a failure
        out["error"] = f"{type(e).__name__}: {e}"
        say("ERROR:", out["error"])
        rc = 1
    finally:
        path = os.path.join(SMOKE_DIR, f"{tag}.json")
        write_text_retry(path, json.dumps(out, ensure_ascii=False, indent=1, default=str))
        say("saved", path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
