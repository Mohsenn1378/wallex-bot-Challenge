"""T3 — runtime control file + LLM hot-swap. Run: python tests/test_runtime_control.py  (or pytest)"""
import json, os, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_LEDGER_DIR", tempfile.mkdtemp(prefix="ledger_test_"))
from wallex_agent.runtime import Control, parse_tehran, DEFAULTS
from wallex_agent.llm.adapter import ROOT
from wallex_agent.llm_policy import LLMPolicy, Native, Reflector

SRC = os.path.join(ROOT, "config", "runtime.yaml")


def setup():
    d = tempfile.mkdtemp(prefix="ctl_test_")
    p = os.path.join(d, "runtime.yaml")
    shutil.copy(SRC, p)
    return d, p, Control(p, log_dir=d)


def write(p, text):
    st = os.stat(p)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # guarantee a new mtime


def edit(p, old, new):
    s = open(p, encoding="utf-8").read()
    assert old in s
    write(p, s.replace(old, new))


def changes(d):
    path = os.path.join(d, "control_changes.jsonl")
    return [json.loads(l) for l in open(path, encoding="utf-8")] if os.path.exists(path) else []


def test_shipped_file_is_valid_and_complete():
    d, p, c = setup()
    v = c.load()
    assert not c.warnings and set(DEFAULTS) <= set(v)
    assert v["pause_entries"] is False and v["size_mult"] == 1.0 and v["max_positions"] == 6
    assert c.flatten_start_ts == parse_tehran("2026-10-23 22:00")
    assert parse_tehran("2026-10-23 22:00") == 1792780200            # 2026-10-23 18:30 UTC, fixed +03:30


def test_change_seen_on_next_load():
    d, p, c = setup()
    c.load()
    edit(p, "pause_entries: false", "pause_entries: true")
    assert c.load()["pause_entries"] is True and c.pause_entries is True
    ev = changes(d)
    assert ev[-1]["event"] == "changed" and ev[-1]["changes"] == {"pause_entries": [False, True]}


def test_unchanged_file_not_reparsed():
    d, p, c = setup()
    first = c.load()
    assert c.load() is first and len(changes(d)) == 1


def test_broken_yaml_keeps_last_valid():
    d, p, c = setup()
    c.load()
    write(p, "llm_profile: [unclosed\n  pause_entries: ::\n")
    v = c.load()
    assert v["pause_entries"] is False and v["llm_profile"] == "box_sol61,box_sol6,box_sol56"
    assert c.warnings and changes(d)[-1]["event"] == "invalid"


def test_out_of_range_and_unknown_keys_rejected():
    d, p, c = setup()
    c.load()
    edit(p, "size_mult: 1.0", "size_mult: 3.0")
    assert c.load()["size_mult"] == 1.0
    edit(p, "size_mult: 3.0", "size_mult: 0.5\npause_entry: true")         # typo key -> whole file rejected
    assert c.load()["size_mult"] == 1.0
    edit(p, "pause_entry: true", "")
    assert c.load()["size_mult"] == 0.5
    edit(p, '"2026-10-23 22:00"', '"23/10 22:00"')
    assert c.load()["flatten_start"] == "2026-10-23 22:00"
    edit(p, "native_every_min: 30", "native_every_min: 5")
    assert c.load()["native_every_min"] == 30


def test_invalid_at_start_pauses_entries():
    d = tempfile.mkdtemp(prefix="ctl_test_")
    p = os.path.join(d, "runtime.yaml")
    open(p, "w").write("size_mult: lots\n")
    c = Control(p, log_dir=d)
    assert c.load()["pause_entries"] is True and c.warnings


def test_llm_profile_change_rebuilds_chain():
    d, p, c = setup()
    v = c.load()
    pol, nat, ref = (LLMPolicy("C1", v["llm_profile"]), Native(v["llm_profile"]), Reflector(v["llm_profile"]))
    old = pol.llm
    assert pol.llm.names == "box_sol61,box_sol6,box_sol56"
    assert pol.set_profile(v["llm_profile"]) is False and pol.llm is old           # same string: no rebuild
    edit(p, 'llm_profile: "box_sol61,box_sol6,box_sol56"', 'llm_profile: "box_sol6"')
    v = c.load()
    for obj in (pol, nat, ref):
        assert obj.set_profile(v["llm_profile"]) is True
        assert obj.llm.names == "box_sol6,box_sol61,box_sol56"                      # single name keeps box fallbacks
    assert pol.llm is not old
    assert changes(d)[-1]["changes"]["llm_profile"] == ["box_sol61,box_sol6,box_sol56", "box_sol6"]


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
