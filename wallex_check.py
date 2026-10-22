"""
Wallex read-only checker for the hackathon.
- Places NO orders. Only GET requests.
- Reads the API key from .env (WALLEX_API_KEY=...) or the environment.
- Never writes the key to the output.
Run:  python wallex_check.py
Output: wallex_check_result.json (send nothing; Claude reads this file)
"""
import json, os, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

BASE = "https://api.wallex.ir"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "wallex_check_result.json")


def load_key():
    key = os.environ.get("WALLEX_API_KEY", "").strip()
    env = os.path.join(HERE, ".env")
    if not key and os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            line = line.strip()
            if line.startswith("WALLEX_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


KEY = load_key()
SAFE_HEADERS = ("date", "content-type", "x-ratelimit", "ratelimit", "retry-after", "x-rate", "cf-ray", "server")


def call(path, params=None, private=False):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": "wallex-check/1.0", "Accept": "application/json"})
    if private:
        req.add_header("x-api-key", KEY)
    t0 = time.time()
    rec = {"path": path, "params": params, "private": private}
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            rec["status"] = r.status
            hdrs = r.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        rec["status"] = e.code
        hdrs = e.headers
    except Exception as e:
        rec["status"] = None
        rec["error"] = repr(e)
        rec["ms"] = round((time.time() - t0) * 1000)
        return rec, None
    rec["ms"] = round((time.time() - t0) * 1000)
    rec["headers"] = {k: v for k, v in hdrs.items() if any(s in k.lower() for s in SAFE_HEADERS)}
    try:
        data = json.loads(body)
    except Exception:
        data = None
        rec["body_head"] = body[:300]
    return rec, data


def trim(x, n=5):
    if isinstance(x, list):
        return x[:n]
    if isinstance(x, dict):
        return {k: trim(v, n) for k, v in list(x.items())[:40]}
    return x


res = {"run_at_utc": datetime.now(timezone.utc).isoformat(), "has_api_key": bool(KEY), "public": {}, "private": {}}

# ---------- public ----------
rec, d = call("/v1/markets")
res["public"]["markets"] = rec
if d:
    sy = d.get("result", {}).get("symbols", {})
    rec["count"] = len(sy)
    rec["rules"] = {s: {k: sy[s].get(k) for k in ("stepSize", "tickSize", "minQty", "minNotional", "isZeroFee", "isMarketTypeEnable")}
                    for s in ("BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT", "USDTTMN", "SOLTMN", "SOLUSDT", "XRPTMN", "DOGETMN", "PEPETMN", "XAUTTMN") if s in sy}

for path in ("/v1/currencies/stats", "/v1/time", "/v2/time"):
    rec, d = call(path)
    rec["sample"] = trim(d, 3) if path != "/v1/currencies/stats" else None
    res["public"][path] = rec

for sym in ("BTCTMN", "BTCUSDT", "USDTTMN"):
    rec, d = call("/v1/depth", {"symbol": sym})
    if d:
        r = d.get("result", {})
        rec["levels"] = {side: len(r.get(side, []) or []) for side in ("ask", "bid")}
        rec["top"] = {side: (r.get(side) or [None])[:2] for side in ("ask", "bid")}
    res["public"][f"depth_{sym}"] = rec
    rec, d = call("/v1/trades", {"symbol": sym})
    if d:
        tl = d.get("result", {}).get("latestTrades", [])
        rec["n"] = len(tl)
        rec["sample"] = tl[:2]
    res["public"][f"trades_{sym}"] = rec

# candles: how far back and how many bars per call, per resolution
now = int(time.time())
for reso, back_days in (("1", 1), ("1", 7), ("1", 30), ("1", 90), ("5", 30), ("15", 90), ("60", 365)):
    rec, d = call("/v1/udf/history", {"symbol": "BTCTMN", "resolution": reso, "from": now - back_days * 86400, "to": now})
    if isinstance(d, dict) and d.get("t"):
        rec["bars"] = len(d["t"])
        rec["first_utc"] = datetime.fromtimestamp(d["t"][0], timezone.utc).isoformat()
        rec["last_utc"] = datetime.fromtimestamp(d["t"][-1], timezone.utc).isoformat()
        rec["s"] = d.get("s")
    else:
        rec["sample"] = trim(d, 3)
    res["public"][f"candles_{reso}m_{back_days}d"] = rec

# rate limit probe: 30 quick public calls
codes = []
t0 = time.time()
for _ in range(30):
    r, _d = call("/v1/depth", {"symbol": "BTCTMN"})
    codes.append(r.get("status"))
res["public"]["burst_30_calls"] = {"seconds": round(time.time() - t0, 2), "status_counts": {str(c): codes.count(c) for c in set(codes)}}

# ---------- private (read-only) ----------
if KEY:
    for path in ("/v1/account/profile", "/v1/account/balances", "/v1/account/fee", "/v1/account/openOrders",
                 "/v1/account/trades", "/v1/account/api-keys"):
        rec, d = call(path, private=True)
        if path == "/v1/account/balances" and d:
            bal = d.get("result", {}).get("balances", {})
            rec["non_zero"] = {k: {"value": v.get("value"), "locked": v.get("locked")} for k, v in bal.items()
                               if float(v.get("value") or 0) > 0}
        elif path == "/v1/account/profile" and d:
            r = d.get("result", {}) if isinstance(d, dict) else {}
            rec["keys"] = list(r.keys())[:40]  # field names only, no personal values
        else:
            rec["sample"] = trim(d, 5)
        res["private"][path] = rec
else:
    res["private"]["note"] = "No WALLEX_API_KEY found in .env — private checks skipped."

json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("Done. Saved:", OUT)
print("API key found:", bool(KEY))
for k, v in list(res["public"].items()) + list(res["private"].items()):
    if isinstance(v, dict):
        print(f"{k:28s} status={v.get('status')} ms={v.get('ms')}")
