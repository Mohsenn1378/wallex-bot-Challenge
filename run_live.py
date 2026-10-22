"""
Supervisor for the real run (docs/IMPROVEMENT_PROPOSAL.md P0-08). Restarts the C7 shadow/live process on crashes.
    python run_live.py --run-id live_20261022 --accounts C7 --llm box_sol61,box_sol6,box_sol56 --end "2026-10-23 22:50"
- always passes --live; passes --resume whenever runs/<run-id>/live_state.json exists
- refuses to start unless config/broker.yaml has a boolean stop_supported and runs/smoke/ holds a smoke-test file
- refreshes candles before every start (python -m wallex_agent.data.fetch --days 2)
- child output -> runs/<run-id>/child.log, own log -> runs/<run-id>/supervisor.log
- restarts on a non-zero exit (max 30, 10 s apart) until --end. Ctrl+C: the child stops safely (no selling), no restart.
"""
import argparse, glob, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from wallex_agent.runtime import parse_tehran

MAX_RESTARTS, RESTART_DELAY_S = 30, 10


def log(path, msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    for i in range(5):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except OSError:
            time.sleep(0.2 * (i + 1))


def preflight() -> str | None:
    """Reason not to start, or None."""
    import yaml
    p = os.path.join(ROOT, "config", "broker.yaml")
    try:
        cfg = yaml.safe_load(open(p, encoding="utf-8")) or {}
    except Exception as e:
        return f"cannot read {p}: {e}"
    if not isinstance(cfg.get("stop_supported"), bool):
        return "config/broker.yaml: stop_supported must be true/false (run the smoke test, P0-09)"
    ok = []
    for f in glob.glob(os.path.join(ROOT, "runs", "smoke", "*.json")):
        try:
            r = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if r.get("part") == 1 and not r.get("error") and r.get("broker_written") and not r.get("open_orders_end"):
            ok.append(f)
    if not ok:
        return "no successful smoke test (part 1 without error, broker.yaml written) in runs/smoke/ (P0-09)"
    return None


def child_cmd(a, extra, resume):
    cmd = [sys.executable, "-m", "wallex_agent.shadow", "--live", "--run-id", a.run_id, "--accounts", a.accounts,
           "--end", a.end]
    if a.llm:
        cmd += ["--llm", a.llm]
    if resume:
        cmd.append("--resume")
    return cmd + extra


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--accounts", default="C7")
    ap.add_argument("--llm", default=None)
    ap.add_argument("--end", required=True, help='"YYYY-MM-DD HH:MM" Tehran')
    ap.add_argument("--no-fetch", action="store_true", help="skip the candle refresh before each start")
    a, extra = ap.parse_known_args()
    end_ts = parse_tehran(a.end)
    out = os.path.join(ROOT, "runs", a.run_id)
    os.makedirs(out, exist_ok=True)
    slog, clog = os.path.join(out, "supervisor.log"), os.path.join(out, "child.log")
    why = preflight()
    if why:
        log(slog, "REFUSED: " + why)
        sys.exit(2)
    restarts = 0
    while time.time() < end_ts:
        if not a.no_fetch:
            log(slog, "refreshing candles (2 days)")
            with open(clog, "a", encoding="utf-8") as f:
                subprocess.run([sys.executable, "-m", "wallex_agent.data.fetch", "--days", "2"], cwd=ROOT,
                               stdout=f, stderr=subprocess.STDOUT)
        resume = os.path.exists(os.path.join(out, "live_state.json"))
        cmd = child_cmd(a, extra, resume)
        log(slog, f"start #{restarts} resume={resume}: {' '.join(cmd[1:])}")
        with open(clog, "a", encoding="utf-8") as f:
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"))
            try:
                rc = p.wait()
            except KeyboardInterrupt:
                log(slog, "Ctrl+C: waiting for the child to save its state (it sends no sell orders)")
                try:
                    rc = p.wait(timeout=60)
                except (KeyboardInterrupt, subprocess.TimeoutExpired):
                    p.kill()
                    rc = -1
                log(slog, f"child exited rc={rc}; supervisor stops (no restart)")
                sys.exit(130)
        log(slog, f"child exited rc={rc}")
        if rc == 0:
            break
        restarts += 1
        if restarts > MAX_RESTARTS:
            log(slog, f"giving up after {MAX_RESTARTS} restarts")
            sys.exit(1)
        time.sleep(RESTART_DELAY_S)
    log(slog, "supervisor done")


if __name__ == "__main__":
    main()
