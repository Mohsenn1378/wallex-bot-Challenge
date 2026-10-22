"""Tiny JSON extraction + validation (subset of JSON Schema) — no dependencies."""
from __future__ import annotations
import json
import re

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "number": (int, float), "integer": int, "null": type(None)}


def extract_json(text: str):
    """Return the first JSON object found in text (handles ```json fences and prose around it)."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                esc = (c == "\\" and not esc)
                if c == '"' and not esc:
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except json.JSONDecodeError:
                        break
                    break
        start = t.find("{", start + 1)
    return None


def validate(v, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    typ = schema.get("type")
    if typ:
        types = typ if isinstance(typ, list) else [typ]
        ok = any(isinstance(v, _TYPES[t]) and not (t in ("number", "integer") and isinstance(v, bool)) for t in types)
        if not ok:
            return [f"{path}: expected {typ}, got {type(v).__name__}"]
    if "enum" in schema and v not in schema["enum"]:
        errs.append(f"{path}: {v!r} not in {schema['enum']}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            errs.append(f"{path}: {v} < {schema['minimum']}")
        if "maximum" in schema and v > schema["maximum"]:
            errs.append(f"{path}: {v} > {schema['maximum']}")
    if isinstance(v, str) and "minLength" in schema and len(v) < schema["minLength"]:
        errs.append(f"{path}: string shorter than {schema['minLength']}")
    if isinstance(v, dict):
        for k in schema.get("required", []):
            if k not in v:
                errs.append(f"{path}: missing '{k}'")
        for k, sub in schema.get("properties", {}).items():
            if k in v:
                errs += validate(v[k], sub, f"{path}.{k}")
    if isinstance(v, list) and "items" in schema:
        for i, item in enumerate(v):
            errs += validate(item, schema["items"], f"{path}[{i}]")
        if "maxItems" in schema and len(v) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
    return errs
