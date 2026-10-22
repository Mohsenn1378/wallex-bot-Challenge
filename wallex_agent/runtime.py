"""
Runtime control file (config/runtime.yaml): parameters an operator may change while the bot runs.
    ctl = Control("config/runtime.yaml", log_dir="runs/<run>")
    ctl.load()            # cheap when the file did not change (mtime cache)
    ctl.pause_entries, ctl.size_mult, ctl.flatten_start_ts, ...
Times are "YYYY-MM-DD HH:MM" Tehran with a fixed +03:30 offset (no zoneinfo: Windows has no tz database).
An invalid file never replaces the last valid one; every accepted change is appended to control_changes.jsonl.
"""
from __future__ import annotations
import json, os, time
from datetime import datetime, timedelta, timezone

TEHRAN = timezone(timedelta(hours=3, minutes=30))
TIME_KEYS = ("probe_only_after", "no_new_entries_after", "flatten_start", "hard_deadline")
DEFAULTS = {"llm_profile": "box_sol61,box_sol6,box_sol56", "pause_entries": False, "size_mult": 1.0,
            "native_every_min": 30, "max_positions": 6, "probe_only_after": "2026-10-23 14:00",
            "no_new_entries_after": "2026-10-23 20:00", "flatten_start": "2026-10-23 22:00",
            "hard_deadline": "2026-10-23 22:45", "flatten_now": False}


def parse_tehran(s: str) -> float:
    """'YYYY-MM-DD HH:MM' (Tehran) -> unix seconds."""
    return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TEHRAN).timestamp()


def fmt_tehran(ts: float) -> str:
    return datetime.fromtimestamp(ts, TEHRAN).strftime("%Y-%m-%d %H:%M")


def validate(raw) -> tuple[dict | None, str | None]:
    """Returns (clean values, None) or (None, reason). Unknown keys are an error (a typo must not be ignored silently)."""
    if not isinstance(raw, dict):
        return None, "not a YAML mapping"
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        return None, f"unknown keys: {sorted(unknown)}"
    v = dict(DEFAULTS, **raw)
    if not isinstance(v["llm_profile"], str) or not v["llm_profile"].strip():
        return None, "llm_profile must be a non-empty string"
    v["llm_profile"] = ",".join(p.strip() for p in v["llm_profile"].split(",") if p.strip())
    for k in ("pause_entries", "flatten_now"):
        if not isinstance(v[k], bool):
            return None, f"{k} must be true/false"
    for k, lo, hi in (("size_mult", 0.0, 1.5), ("native_every_min", 15, 240), ("max_positions", 0, 20)):
        if isinstance(v[k], bool) or not isinstance(v[k], (int, float)) or not lo <= v[k] <= hi:
            return None, f"{k} must be a number in [{lo}, {hi}]"
    v["size_mult"] = float(v["size_mult"])
    v["native_every_min"], v["max_positions"] = int(v["native_every_min"]), int(v["max_positions"])
    for k in TIME_KEYS:
        try:
            v[k + "_ts"] = parse_tehran(v[k])
        except (ValueError, TypeError):
            return None, f"{k} must be 'YYYY-MM-DD HH:MM' (Tehran)"
    if not v["no_new_entries_after_ts"] <= v["flatten_start_ts"] <= v["hard_deadline_ts"]:
        return None, "need no_new_entries_after <= flatten_start <= hard_deadline"
    return v, None


class Control:
    def __init__(self, path: str, log_dir: str | None = None):
        self.path, self.log_dir = path, log_dir
        self.cur: dict | None = None
        self._sig = None
        self.warnings: list = []

    def __getattr__(self, k):
        cur = self.__dict__.get("cur")
        if cur is not None and k in cur:
            return cur[k]
        raise AttributeError(k)

    def _warn(self, msg):
        self.warnings.append({"t": time.time(), "warning": msg})
        print(f"[control] {msg}")
        self._append({"t": time.time(), "event": "invalid", "warning": msg})

    def _append(self, rec):
        if not self.log_dir:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        for i in range(5):                                  # a file open in Excel must not kill the bot
            try:
                with open(os.path.join(self.log_dir, "control_changes.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                return
            except OSError:
                time.sleep(0.2 * (i + 1))

    def load(self) -> dict:
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError as e:
            sig = None
            if self.cur is None:
                return self._fallback(f"cannot read {self.path}: {e}")
            return self.cur
        if sig == self._sig and self.cur is not None:
            return self.cur
        self._sig = sig
        try:
            import yaml
            with open(self.path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except Exception as e:
            raw, err = None, f"YAML error: {str(e)[:200]}"
        else:
            err = None
        new, why = (None, err) if err else validate(raw)
        if new is None:
            if self.cur is None:
                return self._fallback(why)
            self._warn(f"invalid control file ({why}); keeping last valid version")
            return self.cur
        old = self.cur or {}
        changes = {k: [old.get(k), new[k]] for k in DEFAULTS if old.get(k) != new[k]}
        self.cur = new
        if changes:
            self._append({"t": time.time(), "event": "loaded" if not old else "changed", "changes": changes})
        return self.cur

    def _fallback(self, why) -> dict:
        """No valid version yet: built-in defaults but with entries paused (safest choice)."""
        self._warn(f"invalid control file at start ({why}); using defaults with pause_entries=true")
        self.cur, _ = validate(dict(DEFAULTS, pause_entries=True))
        return self.cur
