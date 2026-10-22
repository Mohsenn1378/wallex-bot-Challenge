"""
Decision policies. Every policy returns the SAME decision dict (the LLM schema), so the
engine/guardrails treat a rule baseline and an LLM identically.
"""
from __future__ import annotations
import math

DECISION_SCHEMA = {
    "type": "object",
    "required": ["action", "thesis"],
    "properties": {
        "action": {"enum": ["enter", "skip"]},
        "size_tmn": {"type": "number", "minimum": 0},
        "sl_pct": {"type": "number", "minimum": 0, "maximum": 10},   # skips often send 0; guard clamps enters to the band
        "tp_plan": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "required": ["pct", "portion"],
            "properties": {"pct": {"type": "number", "minimum": 0.2, "maximum": 20},
                           "portion": {"type": "number", "minimum": 0, "maximum": 1}}}},
        "time_stop_min": {"type": "number", "minimum": 0, "maximum": 1440},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "thesis": {"type": "string", "minLength": 3},
        "invalidation": {"type": "string"},
        "profile": {"enum": ["C1", "C2", "C3", "C4", "off"]},
        # v2 (C7). Optional; a skip may send null. An enter without a tier is sized as "standard" by the guard.
        "size_tier": {"enum": ["probe", "standard", "high", None]},
        "setup_quality": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    },
}

NATIVE_SCHEMA = {"type": "object", "required": ["buys"], "properties": {"buys": {"type": "array", "maxItems": 2,
    "items": {"type": "object", "required": ["symbol", "size_tier", "sl_pct", "thesis"],
              "properties": {"symbol": {"type": "string"}, "size_tier": {"enum": ["probe", "standard", "high"]},
                             "setup_quality": {"type": "number", "minimum": 0, "maximum": 100},
                             "sl_pct": {"type": "number", "minimum": 0.2, "maximum": 10}}}}}}


def _sl_from_hint(card, band):
    if card.stop_hint and card.stop_hint < card.price and math.isfinite(card.stop_hint):
        sl = (card.price - card.stop_hint) / card.price * 100
    else:
        sl = sum(band) / 2
    return min(max(sl, band[0]), band[1])


class BaselinePolicy:
    """Fixed, explainable rules per combo (the 'no-LLM' twin)."""
    name = "baseline"

    def __init__(self, combo_id: str, cfg: dict):
        self.cid, self.cfg = combo_id, cfg

    def decide(self, card, ctx) -> dict:
        prof = ctx.get("profile", self.cid)
        return baseline_plan(prof, card, ctx)


def baseline_plan(prof: str, card, ctx) -> dict:
    from .combos import COMBOS
    c = COMBOS[prof]
    d = {"action": "enter", "confidence": 0.5, "profile": prof,
         "thesis": f"{card.scanner} signal ({prof} rules)", "invalidation": f"price < {card.stop_hint:.6g}"}
    if prof == "C3":
        sl = _sl_from_hint(card, c["sl_band"])
        d.update(sl_pct=sl, tp_plan=[{"pct": sl * c["tp_r"][0], "portion": 0.5}, {"pct": sl * c["tp_r"][1], "portion": 0.5}],
                 time_stop_min=c["time_stop"])
    else:
        d.update(size_tmn=c["size"], sl_pct=c["base_sl"],
                 tp_plan=[{"pct": p, "portion": q} for p, q in c["base_tps"]], time_stop_min=c["time_stop"])
    return d
