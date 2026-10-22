"""T2 — LLM budget ledgers. Run: python tests/test_llm_budget.py  (or python -m pytest -q tests/test_llm_budget.py)
Every test writes ledgers into its own temporary LLM_LEDGER_DIR; the real .cache ledgers are never touched."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.llm.adapter import (LLM, Backend, LLMError, Ledger, _RateLimiter, cost_usd, load_config,
                                      resolve_chain, ROOT, PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW)

DEFAULTS = {"json_retries": 0, "ledgers": {"codecraft": {"token_cap": 900_000},
                                           "boxapi": {"usd_cap": 2.7, "low_priority_stop_frac": 0.8}}}
PRICE = {"input": 2.0, "cached_input": 0.10, "output": 10.0}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300,
         "prompt_tokens_details": {"cached_tokens": 200}}
OK_JSON = json.dumps({"action": "skip", "thesis": "test"})


class Fake(Backend):
    def __init__(self, name, ledger="boxapi", fail=False):
        super().__init__(name, {"ledger": ledger, "price_usd_per_m": PRICE, "cache": False}, DEFAULTS)
        self.fail, self.calls = fail, 0

    def complete(self, system, prompt):
        self.calls += 1
        if self.fail:
            raise LLMError("HTTP 502: bad gateway")
        return OK_JSON, dict(USAGE)


def fresh_dir():
    d = tempfile.mkdtemp(prefix="ledger_test_")
    os.environ["LLM_LEDGER_DIR"] = d
    os.environ.pop("LLM_BUDGET_USD", None)
    os.environ.pop("LLM_TOKEN_CAP", None)
    return d


def mk(*backends):
    return LLM(list(backends), DEFAULTS, None, _RateLimiter(10_000, 4))


def seed_usd(d, usd):
    with open(os.path.join(d, "ledger_boxapi.txt"), "a", encoding="utf-8") as f:
        f.write(f"1000 {usd:.6f} box_sol61 0 decide\n")


def test_cost_formula_exact():
    # ((1000-200)*2 + 200*0.10 + 300*10) / 1e6
    assert abs(cost_usd(USAGE, PRICE) - (800 * 2.0 + 200 * 0.10 + 300 * 10.0) / 1e6) < 1e-12
    assert cost_usd({"prompt_tokens": 100, "completion_tokens": 50}, PRICE) == (100 * 2 + 50 * 10) / 1e6
    est = cost_usd({}, PRICE, "x" * 35, "y" * 35)     # no usage -> len/3.5 input + 600 output
    assert abs(est - (20 * 2.0 + 600 * 10.0) / 1e6) < 1e-12
    assert cost_usd(USAGE, None) == 0.0


def test_call_is_booked_with_cost():
    d = fresh_dir()
    r = mk(Fake("box_sol61")).decide("s", "p", role="decide")
    assert r.ok and abs(r.cost_usd - cost_usd(USAGE, PRICE)) < 1e-12
    line = open(os.path.join(d, "ledger_boxapi.txt"), encoding="utf-8").read().split()
    assert line[0] == "1300" and line[2] == "box_sol61" and line[4] == "decide"
    assert abs(float(line[1]) - cost_usd(USAGE, PRICE)) < 1e-6


def test_codecraft_cap_does_not_block_boxapi():
    d = fresh_dir()
    with open(os.path.join(d, "token_ledger.txt"), "w", encoding="utf-8") as f:
        f.write("900000 sol56 0\n")
    cc = mk(Fake("sol56", ledger="codecraft"))
    r = cc.decide("s", "p")
    assert not r.ok and "token budget" in r.error
    box = mk(Fake("box_sol61"))
    assert box.decide("s", "p").ok


def test_80pct_stops_normal_and_low_but_not_decide():
    d = fresh_dir()
    seed_usd(d, 0.8 * 2.7 + 0.001)
    llm = mk(Fake("box_sol61"))
    assert not llm.decide("s", "p", priority=PRIORITY_NORMAL).ok
    assert not llm.decide("s", "p", priority=PRIORITY_LOW).ok
    assert llm.decide("s", "p", priority=PRIORITY_HIGH).ok


def test_100pct_stops_everything():
    d = fresh_dir()
    seed_usd(d, 2.7)
    be = Fake("box_sol61")
    llm = mk(be)
    for pr in (PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW):
        r = llm.decide("s", "p", priority=pr)
        assert not r.ok and "USD budget" in r.error
    assert be.calls == 0                                  # blocked before any network call


def test_env_budget_override():
    d = fresh_dir()
    seed_usd(d, 2.7)
    os.environ["LLM_BUDGET_USD"] = "5.0"
    try:
        assert mk(Fake("box_sol61")).decide("s", "p").ok
    finally:
        os.environ.pop("LLM_BUDGET_USD", None)


def test_broken_backend_falls_through_to_next():
    fresh_dir()
    a, b = Fake("box_sol61", fail=True), Fake("box_sol6")
    r = mk(a, b).decide("s", "p")
    assert r.ok and r.profile == "box_sol6" and a.calls == 1 and b.calls == 1


def test_chain_resolution():
    cfg = load_config()
    assert resolve_chain(cfg, "decide") == ["box_sol61", "box_sol6", "box_sol56"]
    assert resolve_chain(cfg, "reflect", "box_sol61") == ["box_sol61", "box_sol6", "box_sol56"]
    assert resolve_chain(cfg, "decide", "box_sol6") == ["box_sol6", "box_sol61", "box_sol56"]
    assert resolve_chain(cfg, "decide", "box_sol61,box_sol6") == ["box_sol61", "box_sol6"]
    assert resolve_chain(cfg, "decide", "sol56") == ["sol56"]                 # other ledger: no box fallback
    assert resolve_chain(cfg, "risk_manager", "sol56") == ["sol56", "terra56"]
    assert resolve_chain(cfg, "risk_manager", "mock") == ["mock"]
    p = cfg["profiles"]["box_sol61"]
    assert p["ledger"] == "boxapi" and p["cache"] is False and p["max_output_tokens"] + p["completion_headroom"] == 3200


def test_files_only_in_temp_dir():
    real = os.path.join(ROOT, ".cache")
    before = {f: os.path.getsize(os.path.join(real, f)) for f in os.listdir(real)} if os.path.isdir(real) else {}
    d = fresh_dir()
    mk(Fake("box_sol61")).decide("s", "p")
    mk(Fake("sol56", ledger="codecraft")).decide("s", "p")
    after = {f: os.path.getsize(os.path.join(real, f)) for f in os.listdir(real)} if os.path.isdir(real) else {}
    assert before == after
    assert sorted(os.listdir(d)) == ["ledger_boxapi.txt", "token_ledger.txt"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for t in tests:
        try:
            t(); ok += 1; print("PASS", t.__name__)
        except AssertionError as e:
            print("FAIL", t.__name__, e)
    print(f"{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
