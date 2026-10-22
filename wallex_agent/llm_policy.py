"""
LLM policies: the LLM makes the decision; code only supplies context and enforces guardrails.
Works with any profile in config/llm.yaml (model-agnostic).
"""
from __future__ import annotations
import json, math, os, time
import numpy as np

from .combos import COMBOS
from .policies import DECISION_SCHEMA, baseline_plan
from .llm.adapter import LLM, ROOT

PROMPTS = os.path.join(ROOT, "prompts")
PROMPT_VERSION = "v1"


def _p(name):
    return open(os.path.join(PROMPTS, f"{name}_{PROMPT_VERSION}.md"), encoding="utf-8").read()


def log_call(path, role, r, payload, extra=None):
    """Append one LLM call (input, output, latency, errors) to a JSONL decision log."""
    if not path:
        return
    rec = {"t": time.time(), "role": role, "ok": r.ok, "latency_s": round(r.latency_s, 2), "profile": r.profile,
           "cached": r.cached, "attempts": r.attempts, "error": r.error, "output": r.data, "input": payload}
    if extra:
        rec.update(extra)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _r(x, n=2):
    try:
        return None if x is None or not math.isfinite(float(x)) else round(float(x), n)
    except Exception:
        return None


class ContextBuilder:
    """Turns market arrays into a compact, optionally anonymised JSON the LLM can read."""

    def __init__(self, m, F, R, anonymize=False):
        self.m, self.F, self.R, self.anon = m, F, R, anonymize
        self.alias = {s: f"ASSET_{i:02d}" for i, s in enumerate(m.symbols)}

    def name(self, s):
        sym = self.m.symbols[s]
        return self.alias[sym] if self.anon else sym

    def _bar(self, k, j):
        return int(np.searchsorted(self.F.tf[k].end_idx, j, side="right") - 1)

    def coin_view(self, s, j, bars=12):
        out = {}
        for k in (5, 15, 60):
            b = self._bar(k, j)
            if b < 1:
                continue
            tf, x = self.F.tf[k], self.F.x[k]
            c = tf.c[s, max(0, b - bars + 1):b + 1]
            ref = c[-1]
            bbw = x["bb_up"][s, b] - x["bb_lo"][s, b]
            out[f"{k}m"] = {
                "closes_idx100": [_r(v / ref * 100, 2) for v in c],
                "rsi": _r(x["rsi"][s, b], 1), "atr_pct": _r(x["atr"][s, b] / ref * 100, 2),
                "bb_pctb": _r((ref - x["bb_lo"][s, b]) / bbw, 2) if bbw and bbw > 0 else None,
                "vs_ema50_pct": _r((ref / x["ema50"][s, b] - 1) * 100, 2),
                "vs_ema200_pct": _r((ref / x["ema200"][s, b] - 1) * 100, 2),
                "adx": _r(x["adx"][s, b], 1), "volume_z": _r(x["vz"][s, b], 1),
                "share_of_minutes_traded": _r(x["liq"][s, b], 2),
            }
        b5 = self._bar(5, j)
        if b5 >= 0 and "vwap" in self.F.x[5]:
            vw = self.F.x[5]["vwap"][s, b5]
            out["vs_daily_vwap_pct"] = _r((self.F.tf[5].c[s, b5] / vw - 1) * 100, 2)
        c = self.m.c[s]
        for h, lab in ((60, "ret_1h_pct"), (240, "ret_4h_pct"), (1440, "ret_24h_pct")):
            if j - h >= 0:
                out[lab] = _r((c[j] / c[j - h] - 1) * 100, 2)
        out["minutes_since_last_trade"] = int(np.argmax(self.m.tradable[s, :j + 1][::-1])) if self.m.tradable[s, :j + 1].any() else None
        return out

    def market_view(self, j):
        syms = self.m.symbols
        res = {}
        for ref, lab in (("BTCUSDT", "btc_usd"), ("USDTTMN", "usdt_tmn")):
            if ref in syms:
                s = syms.index(ref); c = self.m.c[s]
                res[lab] = {f"ret_{h}m_pct": _r((c[j] / c[j - h] - 1) * 100, 2) for h in (15, 60, 240, 1440) if j - h >= 0}
        b = self._bar(60, j)
        if b >= 0:
            res["btc_above_1h_ema50"] = bool(self.R.btc_bull_60[b])
        return res

    def card(self, card, j, acct_state, profile, bands):
        price = card.price
        return {
            "asset": self.name(card.sym), "scanner": card.scanner, "scanner_meaning": SCANNER_TEXT.get(card.scanner, ""),
            "signal_timeframe": f"{card.tf}m",
            "suggested_stop_pct_below": _r((price - card.stop_hint) / price * 100, 2) if card.stop_hint and card.stop_hint < price else None,
            "natural_target_pct_above": _r((card.tp_hint / price - 1) * 100, 2) if card.tp_hint == card.tp_hint and card.tp_hint > price else None,
            "regime": self.R.at(j, card.sym), "coin": self.coin_view(card.sym, j), "market": self.market_view(j),
            "portfolio": acct_state, "playbook": profile, "allowed_bands": bands,
        }


SCANNER_TEXT = {
    "M1": "close fell below the lower Bollinger band (20,2) with RSI<30 under EMA50 (deep dip)",
    "M2": "price reclaimed after closing below daily VWAP-2sd (VWAP reversion)",
    "M3": "volatility breakout above session open + k*previous range",
    "M4": "wick swept below a 15m swing low and closed back above with high volume (liquidity sweep)",
    "M5": "pullback in uptrend: MACD cross up after RSI dipped below 41, above EMA200, 1h uptrend",
    "M6": "Bollinger/Keltner squeeze released upward with volume",
    "M7": "volume spike (z>=3) at a local low, close in upper third (capitulation reversal)",
    "M8": "relative-strength leader vs BTC pulling back to EMA21",
    "MOM": "top 4h momentum coin",
}


def bands_of(profile):
    c = COMBOS[profile]
    out = {"size_tmn": c.get("size_band"), "sl_pct": c.get("sl_band"), "tp_pct": c.get("tp_band"),
           "max_time_stop_min": c.get("time_stop")}
    if "risk_tmn" in c:
        out["sizing"] = f"size is computed by code from risk {c['risk_tmn']} TMN / sl_pct"
    return out


class LLMPolicy:
    """C1/C2 (full decision), C4 (veto only), C3 (analyst + risk manager), C6 (decide inside assigned playbook)."""

    def __init__(self, cid: str, llm_profile: str | None = None, anonymize=False, log_path=None):
        self.cid = cid
        self.name = f"llm:{llm_profile or 'default'}"
        self.llm = LLM.from_config(role="decide", override=llm_profile)
        self.llm_risk = LLM.from_config(role="risk_manager", override=llm_profile)
        self.anonymize = anonymize
        self.cb = None
        self.calls = []        # per-call log (prompt hash, latency, ok, error)
        self.log_path = log_path
        self.sys_decide, self.sys_risk = _p("decide"), _p("risk_manager")
        self.lessons = []          # (asset, scanner, tag, lesson)
        self.use_memory = cid == "C6"

    def bind(self, m, F, R):
        self.cb = ContextBuilder(m, F, R, self.anonymize)

    def _log(self, role, r, payload):
        rec = {"t": time.time(), "role": role, "ok": r.ok, "latency_s": round(r.latency_s, 2), "profile": r.profile,
               "cached": r.cached, "attempts": r.attempts, "error": r.error, "output": r.data, "input": payload}
        self.calls.append({k: rec[k] for k in ("role", "ok", "latency_s", "cached", "error")})
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def decide(self, card, ctx):
        j, profile = ctx["idx"], ctx.get("profile", self.cid)
        state = {"equity_tmn": round(ctx["equity"]), "open_positions": ctx["open_positions"]}
        payload = self.cb.card(card, j, state, profile, bands_of(profile))
        if self.use_memory and self.lessons:
            asset = self.cb.name(card.sym)
            rel = [l for l in self.lessons if l[0] == asset or l[1] == card.scanner][-5:]
            payload["lessons_from_past_trades"] = [f"[{t}] {txt}" for _, _, t, txt in rel]
        prompt = "Opportunity card and context (JSON):\n" + json.dumps(payload, ensure_ascii=False)
        r = self.llm.decide(self.sys_decide, prompt, DECISION_SCHEMA)
        self._log("decide", r, payload)
        if not r.ok:
            raise RuntimeError(f"LLM decide failed: {r.error}")
        dec = dict(r.data)
        dec["profile"] = profile
        dec["_latency_min"] = 1 if r.latency_s > 45 else 0
        base = baseline_plan(profile, card, ctx)
        if profile == "C4":                              # veto-only: LLM cannot change parameters
            keep = dec.get("action")
            dec = dict(base, action=keep, thesis=dec.get("thesis", ""), invalidation=dec.get("invalidation", ""),
                       confidence=dec.get("confidence", 0.5))
        for k in ("sl_pct", "tp_plan", "time_stop_min", "size_tmn"):
            dec.setdefault(k, base.get(k))
        if dec.get("action") == "enter" and profile == "C3":
            prop = {"proposal": {k: dec.get(k) for k in ("sl_pct", "tp_plan", "time_stop_min", "thesis", "confidence")},
                    "context": payload}
            rr = self.llm_risk.decide(self.sys_risk, "Analyst proposal and context (JSON):\n" + json.dumps(prop, ensure_ascii=False),
                                      {"type": "object", "required": ["approve", "reason"],
                                       "properties": {"approve": {"type": "boolean"},
                                                      "risk_mult": {"type": "number", "minimum": 0.25, "maximum": 1}}})
            self._log("risk_manager", rr, prop)
            if not rr.ok:
                raise RuntimeError(f"LLM risk manager failed: {rr.error}")
            dec["risk_manager"] = rr.data.get("reason", "")
            if not rr.data.get("approve"):
                dec["action"] = "skip"
            else:
                dec["risk_mult"] = float(rr.data.get("risk_mult", 1.0))
                if rr.data.get("sl_pct"):
                    dec["sl_pct"] = max(float(rr.data["sl_pct"]), float(dec.get("sl_pct") or 0))
                if rr.data.get("tp_plan"):
                    dec["tp_plan"] = rr.data["tp_plan"]
        return dec


class Reflector:
    """After a closed trade, the LLM writes one reusable lesson (C6 memory)."""
    def __init__(self, llm_profile=None, log_path=None):
        self.llm = LLM.from_config(role="reflect", override=llm_profile)
        self.sys = _p("reflect")
        self.log_path = log_path

    def reflect(self, trade, asset_name):
        data = {"asset": asset_name, "scanner": trade.scanner, "profile": trade.profile, "pnl_pct": round(trade.pnl_pct, 2),
                "exit_reason": trade.exit_reason, "hold_min": round((trade.exit_t - trade.entry_t) / 60),
                "thesis": trade.decision.get("thesis"), "regime_at_entry": trade.regime}
        r = self.llm.decide(self.sys, json.dumps(data, ensure_ascii=False),
                            {"type": "object", "required": ["lesson"], "properties": {"lesson": {"type": "string"}}})
        log_call(self.log_path, "reflect", r, data)
        return (r.data or {}).get("lesson"), (r.data or {}).get("tag", "neutral")


class Orchestrator:
    """C6: every 30 min the LLM assigns each coin a playbook (C2/C3/C4/off)."""
    def __init__(self, llm_profile=None, log_path=None):
        self.llm = LLM.from_config(role="orchestrator", override=llm_profile)
        self.sys = _p("orchestrator")
        self.log_path = log_path
        self.assign, self.risk_mult, self.note = {}, 1.0, ""

    def update(self, cb: ContextBuilder, j, coins, lessons):
        table = {}
        for s in coins:
            v = cb.coin_view(s, j, bars=4)
            table[cb.name(s)] = {"regime": cb.R.at(j, s)["coin"], "ret_1h_pct": v.get("ret_1h_pct"),
                                 "ret_24h_pct": v.get("ret_24h_pct"),
                                 "traded_share_15m": (v.get("15m") or {}).get("share_of_minutes_traded"),
                                 "rsi_15m": (v.get("15m") or {}).get("rsi")}
        payload = {"coins": table, "market": cb.market_view(j), "recent_lessons": [l[3] for l in lessons[-10:]]}
        r = self.llm.decide(self.sys, json.dumps(payload, ensure_ascii=False),
                            {"type": "object", "required": ["assign"], "properties": {"assign": {"type": "object"}}})
        log_call(self.log_path, "orchestrator", r, payload)
        if r.ok:
            inv = {cb.name(s): cb.m.symbols[s] for s in coins}
            self.assign = {inv.get(k, k): v for k, v in r.data["assign"].items() if v in ("C2", "C3", "C4", "off")}
            self.risk_mult = float(r.data.get("risk_mult", 1.0) or 1.0)
            self.note = r.data.get("note", "")
        return r


class Native:
    """C5: the LLM sees a table of liquid coins every 15 min and picks up to 2 buys (no scanner)."""
    def __init__(self, llm_profile=None, log_path=None):
        self.llm = LLM.from_config(role="native", override=llm_profile)
        self.sys = _p("native")
        self.log_path = log_path

    def pick(self, cb: ContextBuilder, j, coins, state, bands):
        table = {}
        for s in coins:
            v = cb.coin_view(s, j, bars=6)
            table[cb.name(s)] = {k: v.get(k) for k in ("ret_1h_pct", "ret_4h_pct", "ret_24h_pct", "vs_daily_vwap_pct")}
            table[cb.name(s)].update({"15m": v.get("15m"), "regime": cb.R.at(j, s)["coin"]})
        payload = {"coins": table, "market": cb.market_view(j), "portfolio": state, "allowed_bands": bands}
        r = self.llm.decide(self.sys, json.dumps(payload, ensure_ascii=False),
                            {"type": "object", "required": ["buys"], "properties": {"buys": {"type": "array", "maxItems": 2}}})
        log_call(self.log_path, "native", r, payload)
        inv = {cb.name(s): s for s in coins}
        out = []
        if r.ok:
            for b in r.data["buys"]:
                s = inv.get(b.get("symbol"))
                if s is not None:
                    out.append((s, b))
        return r, out
