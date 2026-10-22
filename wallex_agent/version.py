"""Code version stamped on every decision / LLM call / order record (git short hash, '-dirty' if uncommitted)."""
from __future__ import annotations
import functools, os, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@functools.lru_cache(maxsize=1)
def code_version() -> str:
    env = os.environ.get("WALLEX_CODE_VERSION", "").strip()
    if env and env != "unknown":                     # Docker image: no .git inside, version baked at build time
        return env
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                           timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (h or "nogit") + ("-dirty" if h and dirty else "")
    except Exception:
        return "nogit"
