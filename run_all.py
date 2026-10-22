"""
One command for all tests in parallel:
    python run_all.py --llm sol56 --until 19:00
    python run_all.py --llm sol56 --until 19:00 --cards-llm luna56 --cards-per-combo 120
Starts: (A) backtest of no-LLM baselines, (B) per-card LLM evaluation, (C) live shadow test of 12 accounts.
Builds a report every --report-every minutes and a final report at the end (runs/<id>/report/index.html).
"""
import argparse, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TEH = timezone(timedelta(hours=3, minutes=30))


def sh(args, log, cap=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if cap:
        env["LLM_TOKEN_CAP"] = str(cap)
    return subprocess.Popen([PY, "-m"] + args, cwd=ROOT, env=env, stdout=open(log, "a", encoding="utf-8"), stderr=subprocess.STDOUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", required=True, help="LLM profile for the live agent (config/llm.yaml)")
    ap.add_argument("--cards-llm", default=None, help="profile for test B (default = --llm); comma list = compare models")
    ap.add_argument("--until", required=True, help="HH:MM Tehran time when everything stops")
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--cards-per-combo", type=int, default=30)
    ap.add_argument("--cap-shadow", type=int, default=450000, help="max LLM tokens for the shadow test (C)")
    ap.add_argument("--cap-cards", type=int, default=350000, help="max LLM tokens per test-B process")
    ap.add_argument("--report-every", type=int, default=60)
    ap.add_argument("--skip", default="", help="comma list of A,B,C to skip")
    ap.add_argument("--run-id", default=time.strftime("run_%Y%m%d_%H%M"))
    a = ap.parse_args()
    out = os.path.join(ROOT, "runs", a.run_id); logs = os.path.join(out, "logs"); os.makedirs(logs, exist_ok=True)
    skip = set(a.skip.split(",")) if a.skip else set()

    print("preflight 1/3: simulator golden tests ...")
    if subprocess.call([PY, os.path.join("tests", "test_fill_model.py")], cwd=ROOT, stdout=subprocess.DEVNULL) != 0:
        sys.exit("simulator tests FAILED - not starting")
    print("preflight 2/3: refreshing market data ...")
    subprocess.call([PY, "-m", "wallex_agent.data.fetch", "--days", str(a.days)], cwd=ROOT)
    print("preflight 3/3: LLM check ...")
    profiles = [a.llm] + ([p for p in (a.cards_llm or "").split(",") if p and p != a.llm])
    for p in profiles:
        if subprocess.call([PY, os.path.join("tools", "llm_check.py"), "--llm", p, "--n", "1"], cwd=ROOT) != 0:
            sys.exit(f"LLM profile '{p}' is NOT ready - fix config/.env first")

    led = os.path.join(ROOT, ".cache", "token_ledger.txt")
    try:
        used = sum(int(l.split()[0]) for l in open(led, encoding="utf-8") if l.strip())
    except OSError:
        used = 0
    print(f"LLM tokens used so far (ledger): {used:,}  | caps this run: shadow {a.cap_shadow:,}, cards {a.cap_cards:,}/process")
    procs = {}
    if "C" not in skip:
        procs["C_shadow"] = (["wallex_agent.shadow", "--llm", a.llm, "--until", a.until, "--run-id", f"{a.run_id}/shadow"], a.cap_shadow)
    if "A" not in skip:
        procs["A_backtest"] = (["wallex_agent.backtest", "--days", str(a.days), "--run-id", f"{a.run_id}/backtest"], 0)
    if "B" not in skip:
        for p in (a.cards_llm or a.llm).split(","):
            procs[f"B_cards_{p}"] = (["wallex_agent.eval_cards", "--llm", p, "--days", str(a.days), "--anonymize",
                                      "--per-combo", str(a.cards_per_combo), "--run-id", f"{a.run_id}/cards_{p}"], a.cap_cards)
    running = {k: sh(v[0], os.path.join(logs, f"{k}.log"), v[1]) for k, v in procs.items()}
    restarts = {k: 0 for k in procs}
    print(f"started {list(running)}  -> logs in {logs}")

    now = datetime.now(TEH)
    hh, mm = map(int, a.until.split(":"))
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    next_report = time.time() + a.report_every * 60
    first_cards = f"{a.run_id}/cards_{(a.cards_llm or a.llm).split(',')[0]}"

    def report(final=False):
        args = ["wallex_agent.report", "--out", os.path.join(out, "report"), "--bt", os.path.join(out, "backtest"),
                "--cards", os.path.join(ROOT, "runs", first_cards), "--shadow", os.path.join(out, "shadow")]
        subprocess.call([PY, "-m"] + args, cwd=ROOT, stdout=open(os.path.join(logs, "report.log"), "a"), stderr=subprocess.STDOUT)
        print(f"{datetime.now(TEH):%H:%M} report {'(final) ' if final else ''}-> {os.path.join(out, 'report', 'index.html')}")

    while True:
        time.sleep(20)
        for k, p in list(running.items()):
            rc = p.poll()
            if rc is None:
                continue
            if k == "C_shadow" and rc != 0 and datetime.now(TEH) < end and restarts[k] < 5:
                restarts[k] += 1
                print(f"{k} crashed (rc={rc}); restart #{restarts[k]}")
                running[k] = sh(procs[k][0], os.path.join(logs, f"{k}.log"), procs[k][1])
            else:
                print(f"{datetime.now(TEH):%H:%M} {k} finished rc={rc}")
                del running[k]
        if time.time() >= next_report:
            report(); next_report = time.time() + a.report_every * 60
        if not running or datetime.now(TEH) >= end + timedelta(minutes=3):
            break
    for k, p in running.items():
        p.terminate()
    report(final=True)


if __name__ == "__main__":
    main()
