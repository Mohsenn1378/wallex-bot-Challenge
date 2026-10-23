"""T5 — prompt v2 + calibrated context. Run: python tests/test_prompt_v2.py  (or pytest)
Builds real context from data/candles (fixed window) and writes runs/test_prompt_v2/sample_payload.json."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_LEDGER_DIR", tempfile.mkdtemp(prefix="ledger_test_"))
import numpy as np
from wallex_agent.llm.adapter import ROOT
from wallex_agent.llm.jsonschema_lite import validate
from wallex_agent.policies import DECISION_SCHEMA, NATIVE_SCHEMA
from wallex_agent.llm_policy import LLMPolicy, Native, Reflector, prompt_versions, _p, contest_block, bands_of

OUT = os.path.join(ROOT, "runs", "test_prompt_v2")
_CTX = {}


def tok(s):
    return len(s) / 3.5


def ctx():
    """Real market from the fixed regression window + a C7 account; cached across tests."""
    if _CTX:
        return _CTX
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import regress_c1_c6 as rg
    from wallex_agent.engine import Account
    from wallex_agent.runtime import parse_tehran
    c = rg._Ctx()
    os.makedirs(OUT, exist_ok=True)
    log = os.path.join(OUT, "llm_C7.jsonl")
    if os.path.exists(log):
        os.remove(log)
    pol = LLMPolicy("C7", "mock", log_path=log)
    pol.bind(c.m, c.F, c.R)
    acc = Account("C7", pol, c.m, c.f, c.F, c.R, start_cash=5_000_000)
    j = c.m.T - 30
    acc.window = {"flatten_start_ts": float(c.m.t[j]) + 20 * 3600, "no_new_entries_after_ts": float(c.m.t[j]) + 18 * 3600,
                  "probe_only_after_ts": float(c.m.t[j]) + 12 * 3600}
    s = c.m.symbols.index("PEPETMN")
    from wallex_agent.scanners import Card
    card = Card(j, s, "M1", 15, float(c.m.c[s, j]), float(c.m.c[s, j]) * 0.97, float(c.m.c[s, j]) * 1.02, {})
    acc.m1_hist[s].add(j - 100)                           # earlier signal -> card reaches the policy
    acc.on_card(card, j)
    _CTX.update(c=c, acc=acc, pol=pol, log=log, j=j, card=card)
    return _CTX


def logged(role):
    return [json.loads(l) for l in open(ctx()["log"], encoding="utf-8") if json.loads(l)["role"] == role]


def test_system_prompts_small():
    for name in ("decide", "native"):
        assert tok(_p(name, "v2")) <= 1000, name


def test_versions_per_prompt():
    assert prompt_versions("C7") == {"decide": "v2", "native": "v2", "reflect": "v1", "risk_manager": "v1",
                                     "orchestrator": "v1"}
    assert prompt_versions("C1")["decide"] == "v1"
    assert not os.path.exists(os.path.join(ROOT, "prompts", "reflect_v2.md"))
    pol = LLMPolicy("C1", "mock")
    assert pol.sys_decide == _p("decide", "v1") and Reflector("mock").sys == _p("reflect", "v1")


def test_decide_payload_v2():
    x = ctx()
    rec = logged("decide")[-1]
    p = rec["input"]
    assert rec["prompt_version"] == "v2" and "code_version" in rec and "cost_usd" in rec
    assert "natural_target_pct_above" not in p and "nearby_resistance_pct" in p
    ev = p["evidence_features"]
    assert set(ev) == {"liquidity_rank_pct", "m1_prev_6h", "dd_from_6h_high_pct", "btc_above_1h_ema50",
                       "btc_ret_15m_pct", "tehran_hour", "hours_to_flatten", "coin_ret_24h_pct"}
    assert ev["m1_prev_6h"] == 1 and 0 <= ev["liquidity_rank_pct"] <= 100 and ev["hours_to_flatten"] == 20.0
    assert set(p["portfolio"]) == {"equity_tmn", "positions", "trades_24h", "gov_state"}
    assert p["contest"]["target_trades_24h"] == "10-15" and p["contest"]["hours_left"] == 20.0
    assert p["allowed_bands"]["size_tiers_tmn"] == {"probe": 65_000, "standard": 150_000, "high": 220_000}
    assert p["allowed_bands"]["max_time_stop_min"] == 1195
    prompt = "Opportunity card and context (JSON):\n" + json.dumps(p, ensure_ascii=False)
    assert tok(prompt) <= 1200, round(tok(prompt))
    json.dump({"decide": p, "decide_tokens_est": round(tok(prompt))}, open(os.path.join(OUT, "sample_payload.json"), "w"),
              indent=1, ensure_ascii=False)


def test_v1_payload_unchanged_shape():
    x = ctx()
    p = x["pol"].cb.card(x["card"], x["j"], {}, "C1", bands_of("C1"))
    assert "natural_target_pct_above" in p and "evidence_features" not in p


def test_native_payload_v2():
    x = ctx()
    c, acc, j = x["c"], x["acc"], x["j"]
    from wallex_agent.combos import COMBOS
    coins = [c.m.symbols.index(s) for s in COMBOS["C7"]["coins"]]
    nat = Native("mock", log_path=x["log"], prompt_version="v2")
    extra = {s: {"m1_signals_6h": len(acc.m1_hist.get(s, ())), "held": s in acc.positions} for s in coins}
    state = {"equity_tmn": 5_000_000, "positions": [], "trades_24h": 0, "gov_state": "normal"}
    r, picks = nat.pick(x["pol"].cb, j, coins, state, bands_of("C7"), extra=extra,
                        contest=contest_block(acc.context(x["card"], j, "C7")))
    rec = logged("native")[-1]
    p = rec["input"]
    assert rec["prompt_version"] == "v2" and r.ok
    row = next(iter(p["coins"].values()))
    assert {"dd_from_6h_high_pct", "m1_signals_6h", "liquidity_rank_pct", "held"} <= set(row)
    assert tok(json.dumps(p, ensure_ascii=False)) <= 1800, round(tok(json.dumps(p, ensure_ascii=False)))
    d = json.load(open(os.path.join(OUT, "sample_payload.json")))
    d.update(native=p, native_tokens_est=round(tok(json.dumps(p, ensure_ascii=False))))
    json.dump(d, open(os.path.join(OUT, "sample_payload.json"), "w"), indent=1, ensure_ascii=False)


def test_schema_skip_null_and_enter_tiers():
    skip = {"action": "skip", "size_tier": None, "setup_quality": None, "thesis": "weak bounce"}
    assert validate(skip, DECISION_SCHEMA) == []
    enter = {"action": "enter", "size_tier": "probe", "setup_quality": 47, "sl_pct": 5.0, "thesis": "repeat dip"}
    assert validate(enter, DECISION_SCHEMA) == []
    assert validate(dict(enter, size_tier="huge"), DECISION_SCHEMA)
    assert validate(dict(enter, setup_quality=140), DECISION_SCHEMA)
    buys = {"buys": [{"symbol": "PEPETMN", "size_tier": "standard", "sl_pct": 5.0, "thesis": "x"}]}
    assert validate(buys, NATIVE_SCHEMA) == []
    assert validate({"buys": [{"symbol": "PEPETMN", "sl_pct": 5.0, "thesis": "x"}]}, NATIVE_SCHEMA)
    assert validate({"buys": [buys["buys"][0]] * 3}, NATIVE_SCHEMA)


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
