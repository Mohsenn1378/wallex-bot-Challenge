"""
Build the comparison + per-account reports (static HTML, Persian UI).
    python -m wallex_agent.report --bt runs/<bt_id> --cards runs/<cards_id> --shadow runs/<shadow_id> --out runs/<id>/report
"""
from __future__ import annotations
import argparse, csv, html, json, os
from datetime import datetime, timezone, timedelta
import numpy as np
from .combos import COMBOS
from .metrics import jsp

TEH = timezone(timedelta(hours=3, minutes=30))
NAMES = {"C1": "پرریسک", "C2": "کم‌ریسک (بازگشت به میانگین)", "C3": "تحلیلگر + مدیر ریسک", "C4": "سپر (کمترین افت)",
         "C5": "LLM آزاد / مومنتوم", "C6": "ارکستراتور با حافظه"}
CSS = """
:root{--bg:#f5f6f4;--panel:#fff;--ink:#18211d;--muted:#5d6a63;--line:#dde2de;--ok:#137a4f;--okb:#dcf2e6;--warn:#9a6a06;--warnb:#f8edcf;--bad:#a4322a;--badb:#f7e1de;--acc:#0f6b52}
@media (prefers-color-scheme:dark){:root{--bg:#0f1412;--panel:#161d1a;--ink:#e4ebe7;--muted:#93a29a;--line:#26302c;--ok:#5fd39c;--okb:#173326;--warn:#e0b453;--warnb:#352b14;--bad:#f08a80;--badb:#3a1e1b;--acc:#4fc59c}}
body{background:var(--bg);color:var(--ink);font-family:Vazirmatn,Tahoma,sans-serif;direction:rtl;margin:0;font-size:14px;line-height:1.7}
.w{max-width:1300px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-left))}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:22px 0 8px}
.sub{color:var(--muted)} .box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600}td.l{direction:ltr;text-align:left;white-space:normal}
.n{font-family:ui-monospace,Menlo,monospace;direction:ltr;unicode-bidi:isolate}
.ok{background:var(--okb);color:var(--ok)}.warn{background:var(--warnb);color:var(--warn)}.bad{background:var(--badb);color:var(--bad)}
.chip{display:inline-block;padding:0 8px;border-radius:99px;font-weight:700;font-size:12px}
a{color:var(--acc)} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
svg{max-width:100%}
"""


def jl(path):
    if not path or not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def jget(path):
    return json.load(open(path, encoding="utf-8")) if path and os.path.exists(path) else None


API = "https://api.wallex.ir"
_PX_CACHE = {}


def _http(url, timeout=10):
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wallex-agent/1.0"}), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def live_prices():
    """Current bid/ask/last of all markets (None if offline)."""
    if "markets" not in _PX_CACHE:
        j = _http(f"{API}/v1/markets")
        out = {}
        for sym, v in (((j or {}).get("result") or {}).get("symbols") or {}).items():
            st = v.get("stats") or {}
            try:
                out[sym] = {"bid": float(st.get("bidPrice") or 0), "ask": float(st.get("askPrice") or 0),
                            "last": float(st.get("lastPrice") or 0)}
            except (TypeError, ValueError):
                pass
        _PX_CACHE["markets"] = out if out else None
    return _PX_CACHE["markets"]


def entry_open(sym, t_entry):
    """Open of the 1m bar the simulated market order filled on (first bar with a trade at/after t_entry)."""
    key = (sym, t_entry)
    if key not in _PX_CACHE:
        j = _http(f"{API}/v1/udf/history?symbol={sym}&resolution=1&from={t_entry}&to={t_entry + 3 * 3600}")
        px = None
        if j and j.get("s") == "ok":
            for t, o, v in zip(j["t"], j["o"], j["v"]):
                if int(t) >= t_entry and float(v) > 0:
                    px = float(o); break
        _PX_CACHE[key] = px
    return _PX_CACHE[key]


def open_positions(shadow, key):
    """Open positions of a shadow account.
    Prefers positions.json (written by the shadow engine); otherwise reconstructs them from
    accepted decisions that have no closed trade yet (entry price = approx. fill bar open)."""
    if not shadow:
        return []
    folder = os.path.join(shadow, key, "shadow")
    pj = jget(os.path.join(folder, "positions.json"))
    if pj is not None:
        return pj
    decs = jl(os.path.join(folder, "decisions.jsonl"))
    trades = list(csv.DictReader(open(os.path.join(folder, "trades.csv"), encoding="utf-8"))) if os.path.exists(os.path.join(folder, "trades.csv")) else []
    expired = {(d["sym"], d["idx"]) for d in decs if d.get("result") == "limit_expired"}
    out = []
    for d in decs:
        if d.get("result") != "accepted":
            continue
        t_dec = int(d.get("t", 0))
        if any(tr["sym"] == d["sym"] and int(tr["entry_t"]) >= t_dec for tr in trades):
            continue
        if any(s == d["sym"] and i > d["idx"] for s, i in expired):
            continue
        dec = d.get("decision") or {}
        prof = d.get("profile") or key.split("_")[0]
        c = COMBOS.get(prof, {})
        band = c.get("sl_band", (0, 100))
        sl = min(max(float(dec.get("sl_pct") or sum(band) / 2), band[0]), band[1])
        out.append({"sym": d["sym"], "scanner": d.get("scanner"), "profile": prof, "entry_t": t_dec + 60,
                    "entry_px": None, "notional": d.get("notional"), "sl_pct": sl, "tp_plan": dec.get("tp_plan"),
                    "time_stop_min": dec.get("time_stop_min") or c.get("time_stop"), "thesis": dec.get("thesis", ""),
                    "approx": True})
    # keep the latest accepted per symbol (one position per coin)
    last = {}
    for p in out:
        last[p["sym"]] = p
    return list(last.values())


def enrich_positions(pos):
    px = live_prices()
    now = datetime.now(TEH).timestamp()
    for p in pos:
        if p.get("entry_px") is None:
            p["entry_px"] = entry_open(p["sym"], int(p["entry_t"]))
        cur = (px or {}).get(p["sym"], {}).get("bid") or (px or {}).get(p["sym"], {}).get("last")
        p["cur_px"] = cur
        e = p.get("entry_px")
        if e and cur:
            p["unreal_pct"] = (cur / e - 1) * 100
            p["unreal_tmn"] = float(p.get("notional") or 0) * (cur / e - 1) - float(p.get("notional") or 0) * 0.006
        if e:
            p["sl_px"] = p.get("stop_px") or e * (1 - float(p["sl_pct"]) / 100)
            p["tp_px"] = [round(e * (1 + float(t["pct"]) / 100), 8) for t in (p.get("tp_plan") or [])]
        if p.get("time_stop_min"):
            p["time_left_min"] = (int(p["entry_t"]) + int(p["time_stop_min"]) * 60 - now) / 60
    return pos


def positions_table(pos, with_account=False):
    if not pos:
        return "<p class='sub'>پوزیشن بازی نیست</p>"
    def t(x):
        return datetime.fromtimestamp(int(x), TEH).strftime("%H:%M")
    def f(x, d=0):
        return "—" if x is None else (f"{x:,.{d}f}")
    head = ("<th>حساب</th>" if with_account else "") + ("<th>ارز</th><th>اسکنر</th><th>پروفایل</th><th>ورود</th><th>قیمت ورود</th><th>قیمت فعلی</th>"
            "<th>سود/زیان باز ٪</th><th>سود/زیان باز (تومان، با کارمزد)</th><th>سایز</th><th>حد ضرر</th><th>حد سود</th><th>زمان باقی‌مانده</th><th>دلیل تصمیم</th>")
    rows = []
    for p in pos:
        u = p.get("unreal_pct")
        cls = "" if u is None else ("ok" if u > 0 else "bad")
        tl = p.get("time_left_min")
        tl_s = "—" if tl is None else (f"{int(tl // 60)}h {int(tl % 60)}m" if tl > 0 else "تمام‌شده")
        rows.append("<tr>" + (f"<td><a href='{p['_key']}.html'>{p['_key']}</a></td>" if with_account else "") +
                    f"<td class=n>{p['sym']}</td><td>{p.get('scanner')}</td><td>{p.get('profile')}</td><td class=n>{t(p['entry_t'])}</td>"
                    f"<td class=n>{f(p.get('entry_px'), 2)}</td><td class=n>{f(p.get('cur_px'), 2)}</td>"
                    f"<td class='n {cls}'>{'—' if u is None else f'{u:+.2f}'}</td><td class='n {cls}'>{f(p.get('unreal_tmn'))}</td>"
                    f"<td class=n>{f(float(p.get('notional') or 0))}</td>"
                    f"<td class=n>{f(p.get('sl_px'), 2)} ({float(p['sl_pct']):.1f}٪)</td>"
                    f"<td class=n>{html.escape(', '.join(f'{x:,.2f}' for x in p.get('tp_px', [])) or '—')}</td>"
                    f"<td class=n>{tl_s}</td><td class=l>{html.escape(str(p.get('thesis') or ''))}</td></tr>")
    note = "<p class='sub'>قیمت ورود تقریبی است (open کندل اجرا، قبل از اسپرد/لغزش)؛ سود/زیان تومانی با ۰٫۶٪ کارمزد رفت‌وبرگشت. قیمت فعلی = بهترین خرید (bid) لحظه ساخت گزارش.</p>" \
        if any(p.get("approx") for p in pos) else ""
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>{note}"


def spark(eq, w=520, h=110):
    if eq is None or len(eq) < 2:
        return "<p class='sub'>داده‌ای نیست</p>"
    eq = np.asarray(eq, dtype=float); lo, hi = eq.min(), eq.max(); hi = hi if hi > lo else lo + 1
    xs = np.linspace(4, w - 4, len(eq)); ys = h - 6 - (eq - lo) / (hi - lo) * (h - 12)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    base = h - 6 - (2_500_000 - lo) / (hi - lo) * (h - 12)
    return (f"<svg viewBox='0 0 {w} {h}' width='{w}' height='{h}' role='img' aria-label='equity'>"
            f"<line x1='0' x2='{w}' y1='{base:.1f}' y2='{base:.1f}' stroke='currentColor' stroke-opacity='.25' stroke-dasharray='4 4'/>"
            f"<polyline fill='none' stroke='currentColor' stroke-width='1.6' points='{pts}'/></svg>"
            f"<div class='sub n'>min {lo:,.0f} · max {hi:,.0f} · end {eq[-1]:,.0f}</div>")


def live_eq(a):
    if not a:
        return "—"
    return f"{(a.get('equity', 2_500_000) / 2_500_000 - 1) * 100:+.2f}"


def verdict(cid, mx, ai=None):
    if not mx:
        return "warn", "داده کافی نیست"
    r, dd = mx["R1_net_return_pct"], mx["D2_dd_from_start_pct"]
    cap = COMBOS[cid]["dd_cap"] * 100
    why = []
    if mx["trades"] < 5:
        return "warn", "تعداد معامله خیلی کم؛ قابل قضاوت نیست"
    if r > 0 and dd <= cap and (ai is None or (ai.get("AI3_p_value") is not None and ai["AI3_p_value"] < 0.15)):
        return "ok", f"سودده ({r:+.2f}٪) با افت {dd:.2f}٪ زیر سقف {cap:.0f}٪"
    if r <= 0:
        why.append(f"بازده منفی ({r:+.2f}٪)")
    if dd > cap:
        why.append(f"افت {dd:.2f}٪ بالاتر از سقف {cap:.0f}٪")
    if ai is not None and not (ai.get("AI3_p_value") is not None and ai["AI3_p_value"] < 0.15):
        why.append("انتخاب LLM از انتخاب تصادفی بهتر نبوده (کنترل منفی)")
    return ("warn" if r > -0.5 and dd <= cap else "bad"), "؛ ".join(why)


def trades_table(path):
    if not os.path.exists(path):
        return "<p class='sub'>معامله‌ای ثبت نشده</p>"
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if not rows:
        return "<p class='sub'>معامله‌ای ثبت نشده</p>"
    def t(x):
        return datetime.fromtimestamp(int(x), TEH).strftime("%m-%d %H:%M")
    out = ["<table><tr><th>ورود</th><th>خروج</th><th>ارز</th><th>اسکنر</th><th>پروفایل</th><th>سایز</th><th>SL٪</th><th>TP</th>"
           "<th>دلیل خروج</th><th>کارمزد</th><th>سود/زیان</th><th>٪</th><th>R</th><th>رژیم</th><th>دلیل تصمیم</th></tr>"]
    for r in rows:
        cls = "ok" if float(r["pnl"]) > 0 else "bad"
        out.append(f"<tr><td class=n>{t(r['entry_t'])}</td><td class=n>{t(r['exit_t'])}</td><td class=n>{r['sym']}</td>"
                   f"<td>{r['scanner']}</td><td>{r['profile']}</td><td class=n>{float(r['notional']):,.0f}</td><td class=n>{float(r['sl_pct']):.2f}</td>"
                   f"<td class=n>{html.escape(r['tp_plan'])}</td><td>{r['exit_reason']}</td><td class=n>{float(r['fees']):,.0f}</td>"
                   f"<td class='n {cls}'>{float(r['pnl']):,.0f}</td><td class=n>{float(r['pnl_pct']):+.2f}</td><td class=n>{float(r['r_mult']):+.2f}</td>"
                   f"<td>{r['regime_coin']}</td><td class=l>{html.escape(r['thesis'])}</td></tr>")
    out.append("</table>")
    return "".join(out)


def metrics_table(cols: dict):
    keys = ["R1_net_return_pct", "D2_dd_from_start_pct", "D1_maxdd_peak_pct", "trades", "A1_trades_per_day", "A2_hour_coverage_pct",
            "A3_max_gap_h", "win_rate_pct", "R3_profit_factor", "R4_expectancy_tmn", "R5_fee_ratio", "fees_tmn", "avg_hold_min",
            "K1_sl_coverage_pct", "K2_max_pos_pct", "S1_order_error_rate", "S2_policy_error_rate", "sharpe_hourly_ann"]
    head = "".join(f"<th>{k}</th>" for k in cols)
    rows = []
    for k in keys:
        rows.append(f"<tr><td class=n>{k}</td>" + "".join(f"<td class=n>{(v or {}).get(k, '—')}</td>" for v in cols.values()) + "</tr>")
    return f"<table><tr><th>متریک</th>{head}</tr>{''.join(rows)}</table>"


def build(bt, cards, shadow, out):
    os.makedirs(out, exist_ok=True)
    cards_sum = jget(os.path.join(cards, "cards_summary.json")) if cards else {}
    index_rows, pages = [], []
    hb = jget(os.path.join(shadow, "heartbeat.json")) if shadow else None
    hb_acc = (hb or {}).get("accounts", {})
    all_pos = []
    for cid in ["C1", "C2", "C3", "C4", "C5", "C6"]:
        for kind in ("baseline", "llm"):
            key = f"{cid}_{kind}"
            btm = jget(os.path.join(bt, f"{key}", "full", "metrics.json")) if bt else None
            btw = jget(os.path.join(bt, f"{key}", "windows.json")) if bt else None
            sh = jget(os.path.join(shadow, key, "shadow", "metrics.json")) if shadow else None
            ai = (cards_sum or {}).get(cid) if kind == "llm" else None
            shm = sh["metrics"] if sh else None
            main_mx = shm or (btm or {}).get("metrics")
            score = jsp(main_mx, (btw or {}).get("R2", {}).get("median") if not shm else None,
                        {"score": 0.5 * (ai.get("AI3_p_value", 1) < 0.15) + 0.5} if ai else ({"score": 0.0} if kind == "baseline" else None)) if main_mx else None
            v, why = verdict(cid, main_mx, ai)
            index_rows.append((key, cid, kind, btm, btw, shm, ai, score, v, why))
            pos = enrich_positions(open_positions(shadow, key))
            for p in pos:
                p["_key"] = key
            all_pos.extend(pos)
            live = hb_acc.get(key) or {}
            live_html = ""
            if live:
                eqv = live.get("equity", 2_500_000)
                live_html = (f"<p>ارزش لحظه‌ای حساب (از موتور سایه): <b class=n>{eqv:,.0f}</b> تومان "
                             f"(<span class=n>{(eqv / 2_500_000 - 1) * 100:+.2f}%</span>) · پوزیشن باز: <b class=n>{live.get('open', 0)}</b>"
                             f" · معامله بسته‌شده: <b class=n>{live.get('trades', 0)}</b> · خطا: <b class=n>{live.get('errors', 0)}</b></p>")
            # page
            eq = None
            p = os.path.join(shadow or "", key, "shadow", "equity.npy")
            if shadow and os.path.exists(p):
                eq = np.load(p)
            elif bt and os.path.exists(os.path.join(bt, key, "full", "equity.npy")):
                eq = np.load(os.path.join(bt, key, "full", "equity.npy"))
            llm_samples = ""
            if kind == "llm" and shadow:
                calls = [c for c in jl(os.path.join(shadow, f"llm_{cid}.jsonl")) if c.get("ok") and c.get("role") == "decide"][-10:]
                if calls:
                    llm_samples = "<table><tr><th>تصمیم</th><th>اطمینان</th><th>دلیل</th><th>نقطه ابطال</th></tr>" + "".join(
                        f"<tr><td>{(c['output'] or {}).get('action')}</td><td class=n>{(c['output'] or {}).get('confidence')}</td>"
                        f"<td class=l>{html.escape(str((c['output'] or {}).get('thesis')))}</td><td class=l>{html.escape(str((c['output'] or {}).get('invalidation')))}</td></tr>"
                        for c in calls) + "</table>"
            rej = (main_mx or {}).get("rejections", {})
            rej_html = "".join(f"<tr><td>{html.escape(k)}</td><td class=n>{n}</td></tr>" for k, n in sorted(rej.items(), key=lambda x: -x[1])[:15])
            tr_path = os.path.join(shadow, key, "shadow", "trades.csv") if shadow and os.path.exists(os.path.join(shadow, key, "shadow", "trades.csv")) \
                else os.path.join(bt or "", key, "full", "trades.csv")
            win_html = ""
            if btw:
                r2 = btw["R2"]
                win_html = (f"<p>پنجره‌های ۲ روزه بک‌تست (n={r2['n']}): میانه <b class=n>{r2['median']:+.2f}%</b> · چارک پایین <span class=n>{r2['p25']:+.2f}%</span>"
                            f" · بدترین <span class=n>{r2['worst']:+.2f}%</span> · بهترین <span class=n>{r2['best']:+.2f}%</span> · سهم پنجره‌های مثبت <span class=n>{r2['positive_share']:.0%}</span></p>")
            ai_html = ""
            if ai:
                ai_html = ("<div class=box><h2>ارزیابی آفلاین LLM روی کارت‌ها (تست B)</h2><table>" + "".join(
                    f"<tr><td class=n>{k}</td><td class=n>{v}</td></tr>" for k, v in ai.items()) + "</table></div>")
            page = f"""<!doctype html><html lang=fa><head><meta charset=utf-8><meta name=viewport content='width=device-width'><title>{key}</title><style>{CSS}</style></head>
<body><div class=w><a href='index.html'>← مقایسه همه حساب‌ها</a><h1>{key} — {NAMES[cid]} ({'با LLM' if kind == 'llm' else 'بدون LLM'})</h1>
<div class=box><h2>ارزیابی کلی</h2><p><span class='chip {v}'>{ {'ok': 'مناسب', 'warn': 'مشروط', 'bad': 'نامناسب'}[v] }</span> {html.escape(why)}</p>
<p>امتیاز تقریبی داور (JSP): <b class=n>{(score or {}).get('total', '—')}</b> از ۳۰۰ · {html.escape(json.dumps(score, ensure_ascii=False)) if score else ''}</p>{win_html}</div>
<div class=box><h2>پوزیشن‌های باز (لحظه ساخت گزارش)</h2>{live_html}{positions_table(pos)}</div>
<div class=grid><div class=box><h2>منحنی سرمایه</h2>{spark(eq)}</div><div class=box><h2>دلایل رد شدن فرصت‌ها</h2><table>{rej_html}</table></div></div>
<div class=box><h2>متریک‌ها</h2>{metrics_table({'بک‌تست ۳۵ روز': (btm or {}).get('metrics'), 'تست سایه': shm})}</div>
{ai_html}
{'<div class=box><h2>۱۰ تصمیم آخر LLM</h2>' + llm_samples + '</div>' if llm_samples else ''}
<div class=box><h2>همه معاملات</h2>{trades_table(tr_path)}</div></div></body></html>"""
            open(os.path.join(out, f"{key}.html"), "w", encoding="utf-8").write(page)
    # index
    rows = []
    for key, cid, kind, btm, btw, shm, ai, score, v, why in sorted(index_rows, key=lambda r: -((r[7] or {}).get("total", 0))):
        b = (btm or {}).get("metrics") or {}
        r2 = (btw or {}).get("R2") or {}
        rows.append(f"<tr><td><a href='{key}.html'>{key}</a></td><td>{NAMES[cid]}</td><td><span class='chip {v}'>{ {'ok': 'مناسب', 'warn': 'مشروط', 'bad': 'نامناسب'}[v] }</span></td>"
                    f"<td class=n>{(score or {}).get('total', '—')}</td>"
                    f"<td class=n>{b.get('R1_net_return_pct', '—')}</td><td class=n>{r2.get('median', '—') if r2 else '—'}</td><td class=n>{b.get('D2_dd_from_start_pct', '—')}</td>"
                    f"<td class=n>{live_eq(hb_acc.get(key))}</td><td class=n>{(hb_acc.get(key) or {}).get('open', '—')}</td>"
                    f"<td class=n>{(shm or {}).get('R1_net_return_pct', '—')}</td><td class=n>{(shm or {}).get('D2_dd_from_start_pct', '—')}</td><td class=n>{(shm or {}).get('trades', '—')}</td>"
                    f"<td class=n>{(ai or {}).get('AI2_lift_per_card_tmn', '—') if ai else '—'}</td><td class=n>{(ai or {}).get('AI3_p_value', '—') if ai else '—'}</td>"
                    f"<td class=l>{html.escape(why)}</td></tr>")
    hb_line = ""
    if hb:
        age = datetime.now(TEH).timestamp() - hb.get("t", 0)
        hb_line = (f"<p class=sub>آخرین ضربان موتور سایه: <span class=n>{age:.0f}</span> ثانیه پیش · آخرین کندل: "
                   f"<span class=n>{datetime.fromtimestamp(hb.get('last_bar_t', 0), TEH):%H:%M}</span> · خطای داده: "
                   f"<span class=n>{hb.get('feed_errors', 0)}</span> · خطای چرخه: <span class=n>{hb.get('cycle_errors', 0)}</span></p>")
    idx = f"""<!doctype html><html lang=fa><head><meta charset=utf-8><meta name=viewport content='width=device-width'><title>گزارش تست کمبوها</title><style>{CSS}</style></head>
<body><div class=w><h1>گزارش تست ۱۲ حساب</h1><p class=sub>ساخته‌شده {datetime.now(TEH):%Y-%m-%d %H:%M} تهران · بک‌تست: {html.escape(str(bt))} · کارت‌ها: {html.escape(str(cards))} · سایه: {html.escape(str(shadow))}</p>
<div class=box><table><tr><th>حساب</th><th>کمبو</th><th>حکم</th><th>JSP</th><th>بک‌تست ۳۵ روز ٪</th><th>میانه ۲ روزه ٪</th><th>افت بک‌تست ٪</th>
<th>ارزش لحظه‌ای ٪</th><th>پوزیشن باز</th><th>سایه (بسته‌شده) ٪</th><th>افت سایه ٪</th><th>معاملات سایه</th><th>AI lift/کارت (تومان)</th><th>p کنترل منفی</th><th>توضیح</th></tr>{''.join(rows)}</table></div>
<div class=box><h2>همه پوزیشن‌های باز تست سایه</h2>{hb_line}{positions_table(all_pos, with_account=True)}</div>
<p class=sub>JSP = امتیاز تقریبی داور از ۳۰۰ (تعریف در docs/BACKTEST_PLAN.md بخش ۷). حکم «مناسب» یعنی سودده، افت زیر سقف کمبو، و برای نسخه LLM انتخاب بهتر از تصادفی.</p></div></body></html>"""
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(idx)
    json.dump([{"key": r[0], "verdict": r[8], "why": r[9], "jsp": r[7]} for r in index_rows], open(os.path.join(out, "summary.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bt"); ap.add_argument("--cards"); ap.add_argument("--shadow"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print(build(a.bt, a.cards, a.shadow, a.out))
