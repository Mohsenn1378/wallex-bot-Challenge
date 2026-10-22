"""
Model-agnostic LLM layer.

    llm = LLM.from_config("config/llm.yaml", role="decide", override="sol_api")
    result = llm.decide(system, prompt, schema)   # -> LLMResult(data=dict|None, ok, error, latency_s, profile, raw)

Backends: openai_compat | anthropic | opencode_server | command
Only plain text-in / JSON-out is used, so any model that can follow instructions works.
No third-party packages required (stdlib + optional pyyaml for config).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .jsonschema_lite import extract_json, validate

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- env / config
def load_env(path: str | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | None = None) -> dict:
    path = path or os.path.join(ROOT, "config", "llm.yaml")
    import yaml  # pyyaml ships with Anaconda
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class LLMResult:
    ok: bool
    data: dict | None
    error: str | None
    latency_s: float
    profile: str
    raw: str = ""
    cached: bool = False
    attempts: int = 1
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0


class LLMError(Exception):
    pass


# ---------------------------------------------------------------- HTTP helper
def _http_json(url: str, body: dict | None, headers: dict, timeout: float, method: str = "POST") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": os.environ.get("LLM_USER_AGENT", "opencode/1.19.3"), **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        if e.code in (403, 503) and "Just a moment" in body:
            raise LLMError(f"HTTP {e.code}: blocked by Cloudflare bot check (try another user_agent in config/llm.yaml)") from None
        raise LLMError(f"HTTP {e.code}: {body[:400]}") from None
    except Exception as e:  # timeout, DNS, connection refused
        raise LLMError(f"{type(e).__name__}: {e}") from None
    try:
        return json.loads(txt) if txt.strip() else {}
    except json.JSONDecodeError:
        raise LLMError(f"non-JSON HTTP body: {txt[:300]}") from None


def _node_json(url: str, body: dict | None, headers: dict, timeout: float, method: str = "POST") -> dict:
    """Same as _http_json but through Node's fetch (different TLS fingerprint; passes Cloudflare where urllib fails)."""
    node = shutil.which("node")
    if not node:
        raise LLMError("node not found (install Node.js LTS) - needed for transport: node")
    script = os.path.join(ROOT, "tools", "node_fetch.js")
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": os.environ.get("LLM_USER_AGENT", "opencode/1.19.3"), **headers}
    payload = json.dumps({"url": url, "method": method, "headers": h, "body": body, "timeout_ms": int(timeout * 1000)})
    try:
        p = subprocess.run([node, script], input=payload, capture_output=True, text=True, encoding="utf-8", timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        raise LLMError(f"node fetch timed out after {timeout}s") from None
    try:
        r = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        raise LLMError(f"node fetch bad output: {p.stdout[:200]} {p.stderr[:200]}") from None
    if "error" in r:
        raise LLMError(f"node fetch: {r['error'][:300]}")
    txt = r.get("text", "")
    if r.get("status", 0) >= 400:
        if "Just a moment" in txt:
            raise LLMError(f"HTTP {r['status']}: blocked by Cloudflare bot check (node)")
        raise LLMError(f"HTTP {r['status']}: {txt[:400]}")
    try:
        return json.loads(txt) if txt.strip() else {}
    except json.JSONDecodeError:
        raise LLMError(f"non-JSON HTTP body: {txt[:300]}") from None


# ---------------------------------------------------------------- backends
class Backend:
    def __init__(self, name: str, cfg: dict, defaults: dict):
        self.name, self.cfg, self.d = name, cfg, defaults
        self.timeout = float(cfg.get("timeout_s", 45))

    def complete(self, system: str, prompt: str) -> tuple[str, dict]:
        raise NotImplementedError

    def _key(self, env_field: str = "api_key_env") -> str:
        env = self.cfg.get(env_field)
        key = os.environ.get(env or "", "")
        if env and not key:
            raise LLMError(f"env var {env} is empty (put it in .env)")
        return key


class OpenAICompat(Backend):
    """Chat Completions. Gateways differ, so on HTTP 400 we retry with progressively simpler bodies."""
    def complete(self, system, prompt):
        mx = int(self.cfg.get("max_output_tokens", self.d.get("max_output_tokens", 1200)))
        mx += int(self.cfg.get("completion_headroom", 4000))   # room for reasoning tokens
        base = {"model": self.cfg["model"],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        variants = []
        v = dict(base, max_completion_tokens=mx, response_format={"type": "json_object"})
        if self.cfg.get("reasoning_effort"):
            v["reasoning_effort"] = self.cfg["reasoning_effort"]
        variants.append(v)
        variants.append({k: x for k, x in v.items() if k != "response_format"})
        variants.append(dict(base, max_tokens=mx))
        variants.append(dict(base))
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        hdr = {"Authorization": f"Bearer {self._key()}", **(self.cfg.get("headers") or {})}
        if self.cfg.get("user_agent"):
            hdr["User-Agent"] = self.cfg["user_agent"]
        last = None
        for body in variants:
            try:
                fn = _node_json if self.cfg.get("transport") == "node" else _http_json
                j = fn(url, body, hdr, self.timeout)
            except LLMError as e:
                last = e
                if "HTTP 400" in str(e) or "HTTP 422" in str(e):
                    continue
                raise
            msg = (j.get("choices") or [{}])[0].get("message") or {}
            txt = msg.get("content") or ""
            if isinstance(txt, list):
                txt = "".join(p.get("text", "") for p in txt if isinstance(p, dict))
            return txt, j.get("usage", {})
        raise last


class Anthropic(Backend):
    def complete(self, system, prompt):
        body = {
            "model": self.cfg["model"],
            "system": system,
            "max_tokens": int(self.cfg.get("max_output_tokens", self.d.get("max_output_tokens", 1200))),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.d.get("temperature", 0),
        }
        url = self.cfg.get("base_url", "https://api.anthropic.com/v1").rstrip("/") + "/messages"
        hdr = {"x-api-key": self._key(), "anthropic-version": "2023-06-01"}
        j = _http_json(url, body, hdr, self.timeout)
        txt = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        return txt, j.get("usage", {})


# opencode is a coding agent: every tool is switched off so it can only answer with text.
_OPENCODE_TOOLS_OFF = {t: False for t in (
    "bash", "edit", "write", "read", "grep", "glob", "list", "patch", "webfetch",
    "websearch", "todowrite", "todoread", "task", "multiedit", "lsp_diagnostics", "lsp_hover")}


class OpencodeServer(Backend):
    """Talks to `opencode serve` (HTTP). One fresh session per call -> no context leakage between decisions."""

    def _hdr(self):
        pw = os.environ.get(self.cfg.get("password_env") or "", "")
        if not pw:
            return {}
        user = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def complete(self, system, prompt):
        base = self.cfg.get("url", "http://127.0.0.1:4096").rstrip("/")
        sess = _http_json(base + "/session", {"title": "wallex-agent"}, self._hdr(), self.timeout)
        sid = sess.get("id") or sess.get("data", {}).get("id")
        if not sid:
            raise LLMError(f"opencode: no session id in {str(sess)[:200]}")
        body = {
            "model": {"providerID": self.cfg["provider_id"], "modelID": self.cfg["model_id"]},
            "system": system,
            "tools": _OPENCODE_TOOLS_OFF,
            "parts": [{"type": "text", "text": prompt}],
        }
        if self.cfg.get("agent"):
            body["agent"] = self.cfg["agent"]
        j = _http_json(f"{base}/session/{sid}/message", body, self._hdr(), self.timeout)
        parts = j.get("parts") or j.get("data", {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        info = j.get("info") or {}
        if not txt and info.get("error"):
            raise LLMError(f"opencode error: {str(info['error'])[:300]}")
        usage = info.get("tokens") or {}
        try:  # best effort cleanup; never fails the call
            _http_json(f"{base}/session/{sid}", None, self._hdr(), 5, method="DELETE")
        except Exception:
            pass
        return txt, usage


class Command(Backend):
    """Any CLI. prompt_via: stdin | arg. Output may be plain text or JSON-lines events (opencode --format json)."""

    def complete(self, system, prompt):
        full = f"{system}\n\n---\n\n{prompt}"
        cmd = list(self.cfg["cmd"])
        exe = shutil.which(cmd[0])          # Windows: resolves opencode -> opencode.cmd
        if exe:
            cmd[0] = exe
        via = self.cfg.get("prompt_via", "stdin")
        if via == "arg":
            cmd.append(full)
        try:
            p = subprocess.run(cmd, input=full if via == "stdin" else None, capture_output=True,
                               text=True, encoding="utf-8", timeout=self.timeout, cwd=self.cfg.get("cwd"))
        except FileNotFoundError:
            raise LLMError(f"command not found: {cmd[0]} (is it installed and on PATH?)") from None
        except subprocess.TimeoutExpired:
            raise LLMError(f"command timed out after {self.timeout}s") from None
        if p.returncode != 0 and not p.stdout.strip():
            raise LLMError(f"exit {p.returncode}: {p.stderr[-400:]}")
        return _collect_text_events(p.stdout), {}


def _collect_text_events(out: str) -> str:
    """If stdout is JSON-lines events, join their text parts; else return as-is."""
    texts, saw_event = [], False
    for line in out.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_event = True
        part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "".join(texts) if (saw_event and texts) else out


class Mock(Backend):
    """Offline stand-in for pipeline tests (no network). Deterministic, NOT a strategy."""
    def complete(self, system, prompt):
        h = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
        if "RISK MANAGER" in system:
            return json.dumps({"approve": h % 3 != 0, "risk_mult": 1.0, "reason": "mock review"}), {}
        if "orchestrator" in system:
            return json.dumps({"assign": {}, "risk_mult": 1.0, "note": "mock"}), {}
        if "Pick AT MOST 2" in system:
            return json.dumps({"buys": [], "market_view": "mock"}), {}
        if "closed trade" in system:
            return json.dumps({"lesson": "mock lesson", "tag": "neutral"}), {}
        act = "enter" if h % 2 == 0 else "skip"
        return json.dumps({"action": act, "sl_pct": 3.0, "tp_plan": [{"pct": 2.0, "portion": 1.0}],
                           "time_stop_min": 600, "confidence": 0.5, "thesis": "mock decision", "invalidation": "n/a"}), {}


BACKENDS = {"mock": Mock, "openai_compat": OpenAICompat, "anthropic": Anthropic,
            "opencode_server": OpencodeServer, "command": Command}


# ---------------------------------------------------------------- cache + rate limit
class _Cache:
    """One small JSON file per key: works on any filesystem (no DB locking), safe across processes."""
    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def _p(self, k):
        return os.path.join(self.folder, k[:2], k + ".json")

    def get(self, k):
        try:
            with open(self._p(k), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def put(self, k, v):
        p = self._p(k)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(v)
        os.replace(tmp, p)


class _RateLimiter:
    def __init__(self, per_hour: int, parallel: int):
        self.per_hour, self.calls, self.lock = per_hour, [], threading.Lock()
        self.sem = threading.Semaphore(parallel)

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                self.calls = [t for t in self.calls if now - t < 3600]
                if len(self.calls) < self.per_hour:
                    self.calls.append(now)
                    break
            time.sleep(2)
        self.sem.acquire()

    def release(self):
        self.sem.release()


PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW = "high", "normal", "low"


class Ledger:
    """Budget ledger of one gateway, shared across processes via an append-only text file.
    One instance per (folder, name). Folder: env LLM_LEDGER_DIR (default .cache).
      codecraft -> token_ledger.txt, lines "tokens profile unix_ts" (legacy format, token cap)
      other     -> ledger_<name>.txt, lines "tokens usd profile unix_ts role" (USD cap + priority)
    A full ledger only blocks profiles that write to it (CodeCraft's token cap never blocks BoxAPI)."""
    _instances: dict = {}
    _ilock = threading.Lock()

    @classmethod
    def get(cls, name: str, defaults: dict) -> "Ledger":
        folder = os.environ.get("LLM_LEDGER_DIR") or os.path.join(ROOT, ".cache")
        with cls._ilock:
            key = (os.path.abspath(folder), name)
            if key not in cls._instances:
                cfg = dict((defaults.get("ledgers") or {}).get(name) or {})
                if name == "codecraft" and "token_cap" not in cfg:
                    cfg["token_cap"] = int(defaults.get("token_budget_total", 0) or 0)
                cls._instances[key] = cls(name, folder, cfg)
            return cls._instances[key]

    def __init__(self, name: str, folder: str, cfg: dict):
        self.name, self.cfg = name, cfg
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "token_ledger.txt" if name == "codecraft" else f"ledger_{name}.txt")
        self.token_cap = int(cfg.get("token_cap", 0) or 0)
        self.low_frac = float(cfg.get("low_priority_stop_frac", 0.8))
        rc = os.environ.get("LLM_TOKEN_CAP")
        self.run_cap, self.run_used = (int(rc) if rc else None), 0
        hc = os.environ.get("LLM_TOKENS_PER_HOUR")
        self.hour_cap = int(hc) if hc else None        # pacing: spread the budget over the whole run
        self.recent = []                                # (time, tokens) of this process
        self.lock = threading.Lock()

    @property
    def usd_cap(self) -> float:
        """Read on every check so the cap can be raised at runtime (env LLM_BUDGET_USD wins)."""
        if not self.cfg.get("usd_cap"):
            return 0.0
        env = os.environ.get("LLM_BUDGET_USD")
        return float(env) if env else float(self.cfg["usd_cap"])

    def _rows(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [l.split() for l in f if l.strip()]
        except OSError:
            return []

    def used_total(self) -> int:
        return sum(int(r[0]) for r in self._rows())

    def used_usd(self) -> float:
        if self.name == "codecraft":
            return 0.0
        return sum(float(r[1]) for r in self._rows() if len(r) > 1)

    def check(self, priority: str = PRIORITY_HIGH):
        if self.run_cap and self.run_used >= self.run_cap:
            raise LLMError(f"token cap for this run reached ({self.run_used}/{self.run_cap})")
        if self.token_cap and self.used_total() >= self.token_cap:
            raise LLMError(f"total token budget of ledger '{self.name}' reached ({self.token_cap})")
        cap = self.usd_cap
        if cap:
            used = self.used_usd()
            if used >= cap:
                raise LLMError(f"USD budget of ledger '{self.name}' reached ({used:.3f}/{cap:.2f})")
            if priority != PRIORITY_HIGH and used >= self.low_frac * cap:
                raise LLMError(f"USD budget of ledger '{self.name}' at {used / cap:.0%}: "
                               f"{priority}-priority calls stopped (decide still allowed)")
        if self.hour_cap:
            now = time.time()
            with self.lock:
                self.recent = [(t, n) for t, n in self.recent if now - t < 3600]
                used = sum(n for _, n in self.recent)
            if used >= self.hour_cap:
                raise LLMError(f"hourly token cap reached ({used}/{self.hour_cap}); call skipped")

    def add(self, n: int, usd: float, who: str, role: str = "-"):
        with self.lock:
            self.run_used += n
            self.recent.append((time.time(), n))
            line = (f"{int(n)} {who} {int(time.time())}\n" if self.name == "codecraft"
                    else f"{int(n)} {usd:.6f} {who} {int(time.time())} {role or '-'}\n")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)


TokenLedger = Ledger   # old name


def cost_usd(usage: dict, price: dict | None, system: str = "", prompt: str = "") -> float:
    """Gateway-reported cost (BoxAPI: usage.cost, USD) when present. Otherwise OpenAI usage semantics:
    prompt_tokens includes cached tokens; completion_tokens includes reasoning. No usage -> estimate input as
    len/3.5 and output as 600 tokens."""
    if not price:
        return 0.0
    rep = (usage or {}).get("cost")
    if isinstance(rep, (int, float)) and not isinstance(rep, bool) and rep >= 0:
        return float(rep)
    if usage and (usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None):
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    else:
        pt, ct, cached = int((len(system) + len(prompt)) / 3.5), 600, 0
    return ((pt - cached) * float(price.get("input", 0)) + cached * float(price.get("cached_input", price.get("input", 0)))
            + ct * float(price.get("output", 0))) / 1e6


def _tokens(usage: dict, system: str, prompt: str, raw: str) -> int:
    for k in ("total_tokens",):
        if usage.get(k):
            return int(usage[k])
    if usage.get("input") or usage.get("output"):
        return int(usage.get("input", 0)) + int(usage.get("output", 0)) + int(usage.get("reasoning", 0))
    return int((len(system) + len(prompt) + len(raw or "")) / 3.5)


# ---------------------------------------------------------------- public facade
def resolve_chain(cfg: dict, role: str, override: str | list | None = None) -> list[str]:
    """Profile names for a role. Comma list / list override -> exactly that list. A single-name override ->
    [override] + the role chain's members that write to the same ledger (fallbacks stay on that gateway)."""
    profiles = cfg["profiles"]
    role_chain = list(cfg["roles"].get(role) or [])
    if not override:
        return role_chain
    if isinstance(override, str) and "," in override:
        return [n.strip() for n in override.split(",") if n.strip()]
    if not isinstance(override, str):
        return list(override)
    p = profiles.get(override)
    if not p or p.get("backend") == "mock":
        return [override]
    led = p.get("ledger", "codecraft")
    return [override] + [n for n in role_chain if n != override and n in profiles
                         and profiles[n].get("backend") != "mock" and profiles[n].get("ledger", "codecraft") == led]


class LLM:
    def __init__(self, chain: list[Backend], defaults: dict, cache: _Cache | None, limiter: _RateLimiter):
        self.chain, self.d, self.cache, self.limiter = chain, defaults, cache, limiter
        for be in chain:
            if not hasattr(be, "ledger"):
                be.ledger = None if isinstance(be, Mock) else Ledger.get(be.cfg.get("ledger", "codecraft"), defaults)
        led = [be.ledger for be in chain if be.ledger is not None]
        self.ledger = led[0] if led else Ledger.get("codecraft", defaults)
        self.ledgers = list(dict.fromkeys(led))

    @classmethod
    def from_config(cls, path: str | None = None, role: str = "decide", override: str | list | None = None):
        load_env()
        cfg = load_config(path)
        d = cfg.get("defaults", {})
        chain = []
        for n in resolve_chain(cfg, role, override):
            p = cfg["profiles"].get(n)
            if not p:
                raise LLMError(f"unknown LLM profile '{n}' (see config/llm.yaml)")
            chain.append(BACKENDS[p["backend"]](n, p, d))
        cache = _Cache(os.path.join(ROOT, ".cache", "llm")) if d.get("cache", True) else None
        lim = _RateLimiter(int(d.get("calls_per_hour", 600)), int(d.get("max_parallel_calls", 4)))
        return cls(chain, d, cache, lim)

    @property
    def names(self) -> str:
        return ",".join(be.name for be in self.chain)

    def decide(self, system: str, prompt: str, schema: dict | None = None, use_cache: bool = True,
               priority: str = PRIORITY_HIGH, role: str | None = None) -> LLMResult:
        """Try each backend in the chain; each gets json_retries extra attempts for invalid JSON.
        priority: high (entry decisions) may spend up to 100% of a USD ledger, normal/low stop at low_priority_stop_frac."""
        last_err, t0, attempts, spent = "no backend configured", time.time(), 0, 0.0
        for be in self.chain:
            cache_on = use_cache and self.cache and be.cfg.get("cache", self.d.get("cache", True))
            key = hashlib.sha256(f"{be.name}\n{system}\n{prompt}".encode()).hexdigest()
            if cache_on:
                hit = self.cache.get(key)
                if hit is not None:
                    data = json.loads(hit)
                    return LLMResult(True, data, None, 0.0, be.name, hit, cached=True, attempts=0)
            p = prompt
            for i in range(1 + int(self.d.get("json_retries", 2))):
                attempts += 1
                if be.ledger is not None:
                    try:
                        be.ledger.check(priority)
                    except LLMError as e:
                        last_err = f"{be.name}: {e}"
                        break  # budget closed for this gateway -> next backend (if on another ledger)
                limited = not isinstance(be, Mock)        # the offline mock is never rate-limited
                if limited:
                    self.limiter.acquire()
                try:
                    raw, usage = be.complete(system, p)
                    if be.ledger is not None:
                        usd = cost_usd(usage, be.cfg.get("price_usd_per_m"), system, p)
                        spent += usd
                        be.ledger.add(_tokens(usage, system, p, raw), usd, be.name, role or "-")
                except LLMError as e:
                    last_err = f"{be.name}: {e}"
                    break  # transport error -> next backend in chain
                finally:
                    if limited:
                        self.limiter.release()
                data = extract_json(raw)
                errs = validate(data, schema) if (schema and data is not None) else ([] if data is not None else ["no JSON object found"])
                if not errs:
                    if cache_on:
                        self.cache.put(key, json.dumps(data, ensure_ascii=False))
                    return LLMResult(True, data, None, time.time() - t0, be.name, raw, attempts=attempts, usage=usage,
                                     cost_usd=spent)
                last_err = f"{be.name}: invalid JSON ({'; '.join(errs)[:200]})"
                p = prompt + "\n\nYour previous answer was invalid: " + "; ".join(errs)[:300] + \
                    "\nReturn ONLY one JSON object that matches the schema."
        return LLMResult(False, None, last_err, time.time() - t0, self.chain[-1].name if self.chain else "-",
                         attempts=attempts, cost_usd=spent)
