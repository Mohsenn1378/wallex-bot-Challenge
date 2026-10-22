"""
Download 1-minute candles from Wallex (public API, no key needed).

    python -m wallex_agent.data.fetch --days 35            # 28 core coins + BTCUSDT + USDTTMN
    python -m wallex_agent.data.fetch --days 3 --symbols BTCTMN,SOLTMN

Stores one CSV per symbol per UTC day in data/candles/<SYMBOL>/<YYYY-MM-DD>.csv (t,o,h,l,c,v).
Resumable: complete past days are skipped; today is always refreshed.
Wallex returns at most ~36h per request, so we fetch day by day.
"""
from __future__ import annotations
import argparse, csv, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data", "candles")
BASE = "https://api.wallex.ir/v1/udf/history"
EXTRA = ["BTCUSDT", "USDTTMN"]


def core_symbols() -> list[str]:
    p = os.path.join(ROOT, "research", "combos", "coins_eligibility.json")
    return [c["k"] for c in json.load(open(p, encoding="utf-8")) if c["status"] == "core"]


def get(url: str, tries: int = 5) -> dict:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wallex-agent/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def fetch_day(sym: str, day: datetime) -> tuple[str, str, int]:
    folder = os.path.join(DATA, sym)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, day.strftime("%Y-%m-%d") + ".csv")
    today = datetime.now(timezone.utc).date()
    if os.path.exists(path) and day.date() < today:
        return sym, day.strftime("%Y-%m-%d"), -1  # cached
    frm = int(day.timestamp())
    to = int((day + timedelta(days=1)).timestamp()) - 1
    j = get(f"{BASE}?symbol={sym}&resolution=1&from={frm}&to={to}")
    rows = []
    if j.get("s") == "ok":
        for t, o, h, l, c, v in zip(j["t"], j["o"], j["h"], j["l"], j["c"], j["v"]):
            if frm <= int(t) <= to:
                rows.append((int(t), o, h, l, c, v))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        w.writerows(rows)
    os.replace(tmp, path)
    return sym, day.strftime("%Y-%m-%d"), len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    syms = a.symbols.split(",") if a.symbols else core_symbols() + EXTRA
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [start - timedelta(days=i) for i in range(a.days, -1, -1)]
    jobs = [(s, d) for s in syms for d in days]
    print(f"{len(syms)} symbols x {len(days)} days = {len(jobs)} requests")
    done, empty, t0 = 0, [], time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(fetch_day, s, d) for s, d in jobs]
        for f in as_completed(futs):
            try:
                s, d, n = f.result()
                if n == 0:
                    empty.append(f"{s} {d}")
            except Exception as e:
                print("ERROR", e)
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(jobs)}  {time.time() - t0:.0f}s")
    print(f"done in {time.time() - t0:.0f}s; empty days: {len(empty)}")
    if empty:
        print("  e.g.", empty[:10])


if __name__ == "__main__":
    main()
