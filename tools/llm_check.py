"""
Preflight for any LLM profile:  python tools/llm_check.py --llm sol_opencode [--n 3]
Sends a small trading-decision task, checks JSON validity, prints latency. Never places orders.
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.llm.adapter import LLM

SCHEMA = {"type": "object", "required": ["action", "sl_pct", "thesis"],
          "properties": {"action": {"enum": ["enter", "skip"]},
                         "sl_pct": {"type": "number", "minimum": 0.3, "maximum": 5},
                         "thesis": {"type": "string", "minLength": 5}}}
SYSTEM = "You are a disciplined crypto spot trader. Answer with ONE JSON object only."
PROMPT = ("Setup card: ASSET_07 swept its 15m pivot low (wick -0.9%) and closed back above it; "
          "volume z-score 2.1; BTC regime: range; round-trip fee 0.6%. "
          'Decide. JSON: {"action":"enter|skip","sl_pct":number,"thesis":"short reason"}')

ap = argparse.ArgumentParser()
ap.add_argument("--llm", required=True, help="profile name(s) from config/llm.yaml, comma-separated = fallback chain")
ap.add_argument("--n", type=int, default=2)
a = ap.parse_args()
llm = LLM.from_config(override=a.llm)
ok = 0
for i in range(a.n):
    r = llm.decide(SYSTEM, PROMPT + f"\n(check #{i})", SCHEMA, use_cache=False)
    ok += r.ok
    print(f"#{i}: ok={r.ok} profile={r.profile} latency={r.latency_s:.1f}s attempts={r.attempts} "
          f"tokens={(r.usage or {}).get('total_tokens', '?')} "
          f"{'data=' + json.dumps(r.data, ensure_ascii=False) if r.ok else 'error=' + str(r.error)}")
print(f"ledger total so far: {llm.ledger.used_total():,} tokens")
print(f"\n{ok}/{a.n} valid. ", "READY" if ok == a.n else "NOT READY — fix the profile before running tests")
sys.exit(0 if ok == a.n else 1)
