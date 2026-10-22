"""
Minimal Wallex REST client (stdlib only) for the live mirror executor (P0-07).

Safety rules baked in:
- The API key is only ever placed in the `x-api-key` request header. It is never logged, printed or returned.
  `orders.jsonl` records method / path / params / body / status / latency / parsed response - never headers.
- Retries are for GET only (0.5, 1, 2 s). An order POST is sent at most once, unless the outcome is unknown
  (timeout / 5xx), the order is not found by client_id afterwards AND `dup_id_rejected is True` in broker.yaml;
  then it is re-sent exactly once with the SAME client_id. Otherwise OrderUnknown is raised (no blind re-buy).
- Number formatting: quantity floored to the step, buy prices rounded UP and sell prices DOWN to the tick,
  never scientific notation.

Verified facts (NOTES.md, wallex_check_result.json): base https://api.wallex.ir, header x-api-key,
/v1/markets (stepSize/tickSize are DECIMAL PLACES), /v1/depth?symbol= (levels {price, quantity, sum}),
/v1/account/balances ({asset: {value, locked}}), /v1/account/openOrders, /v1/account/trades.
Unverified until tools/live_smoke.py: order POST field names, stop field name, cancel method, the meaning of
balance `value` vs `locked` -> all driven by config/broker.yaml.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

BASE = "https://api.wallex.ir"
TIMEOUT_S = 10
GET_RETRY_DELAYS = (0.5, 1.0, 2.0)
DONE_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}


# ---------------------------------------------------------------- errors
class WallexError(Exception):
    pass


class OrderRejected(WallexError):
    """Exchange answered success=false or 4xx: the order does NOT exist."""


class RateLimited(OrderRejected):
    """HTTP 429 on an order POST: not processed (not counted as a coin rejection)."""


class OrderUnknown(WallexError):
    """Outcome of an order POST is unknown (timeout / 5xx and not found afterwards). Never re-buy blindly."""


# ---------------------------------------------------------------- numbers
def _dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, str):
        return Decimal(x)
    return Decimal(repr(float(x)))


def floor_to(x, decimals: int) -> Decimal:
    q = Decimal(1).scaleb(-int(decimals))
    return _dec(x).quantize(q, rounding=ROUND_FLOOR)


def ceil_to(x, decimals: int) -> Decimal:
    q = Decimal(1).scaleb(-int(decimals))
    return _dec(x).quantize(q, rounding=ROUND_CEILING)


def fmt_dec(d: Decimal) -> str:
    """Plain decimal string, never scientific notation, no trailing zeros after the point."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def fmt_qty(q, step_dec: int) -> str:
    return fmt_dec(floor_to(q, step_dec))


def fmt_price(px, tick_dec: int, side: str) -> str:
    side = side.upper()
    return fmt_dec(ceil_to(px, tick_dec) if side == "BUY" else floor_to(px, tick_dec))


def _f(x, default=0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


class RoundingMixin:
    """Shared by WallexClient and test fakes: needs self.rules(sym) -> {step_dec, tick_dec, min_notional}."""

    def round_qty(self, sym: str, qty: float) -> float:
        return float(floor_to(max(qty, 0.0), self.rules(sym)["step_dec"]))

    def round_price(self, sym: str, px: float, side: str) -> float:
        r = self.rules(sym)
        return float(ceil_to(px, r["tick_dec"]) if side.upper() == "BUY" else floor_to(px, r["tick_dec"]))


# ---------------------------------------------------------------- normalisers
def normalize_order(o: dict | None) -> dict | None:
    if not isinstance(o, dict):
        return None
    status = str(o.get("status") or "").upper()
    ex_qty = _f(o.get("executedQty", o.get("executed_qty")))
    ex_sum = _f(o.get("executedSum", o.get("executed_sum")))
    if not ex_sum and ex_qty:
        ex_sum = ex_qty * _f(o.get("executedPrice", o.get("price")))
    active = o.get("active")
    if active is None:
        active = status not in DONE_STATUSES
    return {
        "client_id": o.get("clientOrderId") or o.get("client_id") or o.get("clientId"),
        "symbol": o.get("symbol"),
        "side": str(o.get("side") or "").upper(),
        "type": str(o.get("type") or "").upper(),
        "price": _f(o.get("price")),
        "stop_price": _f(o.get("stopPrice", o.get("stop_price", o.get("stop_Price")))) or None,
        "orig_qty": _f(o.get("origQty", o.get("quantity", o.get("orig_qty")))),
        "executed_qty": ex_qty,
        "executed_sum": ex_sum,
        "status": status,
        "active": bool(active),
    }


def is_done(o: dict | None) -> bool:
    return o is None or (o.get("status") in DONE_STATUSES) or not o.get("active", True)


def balance_tuple(value: float, locked: float, includes_locked) -> tuple[float, float, float]:
    """Wallex balance row -> (total, free, locked).
    includes_locked True: value already contains locked; False: value is the free part; None (unknown):
    conservative total = value + locked (a position fully locked in a stop is never "gone") and
    conservative free = value - locked (never try to sell more than is surely free)."""
    if includes_locked is True:
        return value, max(value - locked, 0.0), locked
    if includes_locked is False:
        return value + locked, value, locked
    return value + locked, max(value - locked, 0.0), locked


def load_key(repo_root: str | None = None) -> str:
    """WALLEX_API_KEY from env, else from <repo>/.env. The value is never printed."""
    key = os.environ.get("WALLEX_API_KEY", "").strip()
    if key:
        return key
    root = repo_root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = os.path.join(root, ".env")
    if os.path.exists(env):
        with open(env, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("WALLEX_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def append_line_retry(path: str, line: str, tries: int = 5, wait: float = 0.2) -> bool:
    """Append with retry (Windows: file open in Excel must not kill the bot)."""
    for k in range(tries):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return True
        except OSError:
            time.sleep(wait * (k + 1))
    return False


def _urllib_transport(method, url, headers, data, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- client
class WallexClient(RoundingMixin):
    def __init__(self, api_key: str = "", cfg: dict | None = None, log_path: str | None = None,
                 base: str = BASE, timeout: float = TIMEOUT_S, transport=None, sleep=time.sleep):
        self._key = api_key or ""
        self.cfg = dict(cfg or {})
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.log_path = log_path
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._rules: dict[str, dict] = {}
        self._log_lock = threading.Lock()
        self._warned_locked = False

    def __repr__(self):                       # never expose the key
        return f"WallexClient(base={self.base!r}, key={'set' if self._key else 'missing'})"

    # ------------------------------------------------------------ transport + log
    def _log(self, rec: dict):
        if not self.log_path:
            return
        rec = {"ts": round(time.time(), 3), **rec}
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
        except Exception:
            line = json.dumps({"ts": rec["ts"], "path": rec.get("path"), "unserialisable": True})
        if self._key and self._key in line:  # paranoia: never write the key anywhere
            line = line.replace(self._key, "***")
        with self._log_lock:
            append_line_retry(self.log_path, line)

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 private: bool = False):
        """One HTTP exchange. Returns (status, parsed_json_or_None). Network errors propagate."""
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"User-Agent": "wallex-bot-c7/1.0", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if private:
            headers["x-api-key"] = self._key
        t0 = time.time()
        rec = {"method": method, "path": path, "params": params, "body": body}
        try:
            status, text = self._transport(method, url, headers, data, self.timeout)
        except Exception as e:
            rec.update(status=None, error=type(e).__name__ + ": " + str(e)[:200], ms=round((time.time() - t0) * 1000))
            self._log(rec)
            raise
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
            rec["body_head"] = (text or "")[:300]
        rec.update(status=status, ms=round((time.time() - t0) * 1000))
        if not path.startswith("/v1/markets"):           # markets payload is huge; log only status
            rec["response"] = parsed if len(json.dumps(parsed, default=str)) < 6000 else "<truncated>"
        self._log(rec)
        return status, parsed

    def _get(self, path, params=None, private=False):
        last = None
        for k in range(len(GET_RETRY_DELAYS) + 1):
            try:
                status, d = self._request("GET", path, params, private=private)
                if status == 429 or (status is not None and status >= 500):
                    last = WallexError(f"GET {path} -> {status}")
                elif status == 404:
                    return 404, d
                else:
                    return status, d
            except Exception as e:           # network / timeout
                last = e
            if k < len(GET_RETRY_DELAYS):
                self._sleep(GET_RETRY_DELAYS[k])
        raise WallexError(f"GET {path} failed after retries: {last}")

    @staticmethod
    def _result(d):
        return d.get("result") if isinstance(d, dict) else None

    # ------------------------------------------------------------ public data
    def markets(self) -> dict:
        """{sym: {step_dec, tick_dec, min_notional, min_qty, market_ok, bid, ask, last}} (rules cached)."""
        status, d = self._get("/v1/markets")
        sy = (self._result(d) or {}).get("symbols", {}) if status == 200 else {}
        out = {}
        for s, v in sy.items():
            st = v.get("stats") or {}
            out[s] = {"step_dec": int(_f(v.get("stepSize"), 8)), "tick_dec": int(_f(v.get("tickSize"), 0)),
                      "min_notional": _f(v.get("minNotional"), 50_000), "min_qty": _f(v.get("minQty")),
                      "market_ok": bool(v.get("isMarketTypeEnable")),
                      "base": v.get("baseAsset") or (s[:-3] if s.endswith("TMN") else s[:-4]),
                      "bid": _f(st.get("bidPrice")), "ask": _f(st.get("askPrice")), "last": _f(st.get("lastPrice"))}
        if out:
            self._rules.update({s: {k: out[s][k] for k in ("step_dec", "tick_dec", "min_notional", "min_qty", "base")}
                                for s in out})
        return out

    def rules(self, sym: str) -> dict:
        if sym not in self._rules:
            self.markets()
        if sym not in self._rules:
            raise WallexError(f"unknown market {sym}")
        return self._rules[sym]

    def depth(self, sym: str) -> dict:
        """{"ask": [[px, qty], ...] ascending, "bid": [[px, qty], ...] descending}"""
        status, d = self._get("/v1/depth", {"symbol": sym})
        r = self._result(d) or {}

        def side(rows):
            out = []
            for row in rows or []:
                if isinstance(row, dict):
                    out.append([_f(row.get("price")), _f(row.get("quantity"))])
                elif isinstance(row, (list, tuple)) and len(row) >= 2:
                    out.append([_f(row[0]), _f(row[1])])
            return [x for x in out if x[0] > 0 and x[1] > 0]

        return {"ask": sorted(side(r.get("ask")), key=lambda x: x[0]),
                "bid": sorted(side(r.get("bid")), key=lambda x: -x[0])}

    # ------------------------------------------------------------ account
    def balances(self) -> dict:
        """{asset: (total, free, locked)} per broker.yaml `balance_value_includes_locked`."""
        status, d = self._get("/v1/account/balances", private=True)
        if status != 200:
            raise WallexError(f"balances -> {status}")
        bal = (self._result(d) or {}).get("balances", {}) or {}
        flag = self.cfg.get("balance_value_includes_locked")
        out = {}
        if flag not in (True, False) and not self._warned_locked:
            self._warned_locked = True
            self._log({"warning": "balance_value_includes_locked unknown: using total=value+locked"})
        for asset, v in bal.items():
            value, locked = _f(v.get("value")), _f(v.get("locked"))
            total, free, locked = balance_tuple(value, locked, flag)
            if total > 0 or locked > 0:
                out[asset] = (total, free, locked)
        return out

    def open_orders(self, sym: str | None = None) -> list:
        status, d = self._get("/v1/account/openOrders", {"symbol": sym} if sym else None, private=True)
        if status != 200:
            raise WallexError(f"openOrders -> {status}")
        r = self._result(d) or {}
        rows = r.get("orders", []) if isinstance(r, dict) else (r if isinstance(r, list) else [])
        return [o for o in (normalize_order(x) for x in rows) if o]

    def get_order(self, client_id: str) -> dict | None:
        status, d = self._get("/v1/account/orders/" + urllib.parse.quote(client_id, safe=""), private=True)
        if status == 404 or not isinstance(d, dict) or d.get("success") is False:
            return None
        return normalize_order(self._result(d))

    def account_trades(self, sym: str | None = None) -> list:
        status, d = self._get("/v1/account/trades", {"symbol": sym} if sym else None, private=True)
        r = self._result(d) or {}
        return r.get("AccountLatestTrades", []) if isinstance(r, dict) else []

    # ------------------------------------------------------------ orders
    def build_order_body(self, symbol, side, type, quantity, price=None, stop_price=None, client_id=None,
                         stop_field=None) -> dict:
        r = self.rules(symbol)
        side = side.upper()
        body = {"symbol": symbol, "side": side, "type": type.upper(), "quantity": fmt_qty(quantity, r["step_dec"])}
        if price is not None:
            body["price"] = fmt_price(price, r["tick_dec"], side)
        if stop_price is not None:
            # stop trigger rounded toward the market (UP for a SELL stop): triggers no later than intended
            body[stop_field or self.cfg.get("stop_field") or "stop_Price"] = fmt_price(
                stop_price, r["tick_dec"], "BUY" if side == "SELL" else "SELL")
        if client_id:
            body["client_id"] = client_id
        return body

    def create_order(self, symbol, side, type, quantity, price=None, stop_price=None, client_id=None,
                     stop_field=None) -> dict:
        body = self.build_order_body(symbol, side, type, quantity, price, stop_price, client_id, stop_field)
        for attempt in (1, 2):
            unknown = None
            try:
                status, d = self._request("POST", "/v1/account/orders", body=body, private=True)
                if status == 429:
                    raise RateLimited(f"429 rate limited: {self._msg(d)}")
                if status is not None and 400 <= status < 500:
                    raise OrderRejected(f"{status}: {self._msg(d)}")
                if status is not None and status < 300:
                    if isinstance(d, dict) and d.get("success") is False:
                        raise OrderRejected(self._msg(d))
                    o = normalize_order(self._result(d)) or {}
                    o.setdefault("client_id", client_id)
                    if not o.get("client_id"):
                        o["client_id"] = client_id
                    return o
                unknown = f"http {status}"
            except OrderRejected:
                raise
            except Exception as e:                          # timeout / connection error -> outcome unknown
                unknown = e.__class__.__name__
            # unknown outcome: ask by client_id, never blindly re-send
            if not client_id:
                raise OrderUnknown(f"order POST outcome unknown ({unknown}) and no client_id")
            try:
                o = self.get_order(client_id)
            except WallexError:
                o = None
            if o is not None:
                return o
            if attempt == 1 and self.cfg.get("dup_id_rejected") is True:
                continue                                    # safe: exchange rejects a duplicate client_id
            raise OrderUnknown(f"order POST outcome unknown ({unknown}); {client_id} not found; not re-sent")
        raise OrderUnknown(f"order {client_id} unknown after one re-send")

    @staticmethod
    def _msg(d):
        if isinstance(d, dict):
            return str(d.get("message") or d.get("result") or d)[:300]
        return str(d)[:300]

    def cancel_order(self, client_id: str, method: str | None = None) -> dict | None:
        """Cancel by client_id using broker.yaml `cancel_method` (query|body|path); returns the order after cancel."""
        method = method or self.cfg.get("cancel_method") or "query"
        if method == "body":
            self._request("DELETE", "/v1/account/orders", body={"clientOrderId": client_id}, private=True)
        elif method == "path":
            self._request("DELETE", "/v1/account/orders/" + urllib.parse.quote(client_id, safe=""), private=True)
        else:
            self._request("DELETE", "/v1/account/orders", params={"clientOrderId": client_id}, private=True)
        return self.get_order(client_id)
