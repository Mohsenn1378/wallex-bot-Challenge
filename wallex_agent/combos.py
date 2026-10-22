"""
Combo definitions C1..C6 (v2 structure, approved) with bands tuned on evidence (2026-10-22):
fast momentum/breakout scanners had no edge after costs on Wallex; dip-buying (M1) and
liquidity sweeps (M4) on liquid coins had, but only with multi-hour holds and wider stops.
All money in TMN. Bands = limits the LLM must stay inside. base_* = the no-LLM baseline choice.
"""

MEMES = {"PEPETMN", "SHIBTMN", "FLOKITMN", "PUMPTMN", "PENGUTMN"}
MAJORS = {"BTCTMN", "ETHTMN", "SOLTMN", "BNBTMN", "LTCTMN", "BCHTMN"}
LIQUID = ["BTCTMN", "PEPETMN", "PUMPTMN", "ETHTMN", "ARBTMN", "DOGETMN", "SOLTMN", "STMN", "BNBTMN"]
LIQUID_PLUS = LIQUID + ["ADATMN", "SHIBTMN", "FILTMN", "AVAXTMN", "LTCTMN", "SANDTMN"]

COMBOS = {
    "C1": dict(name="High risk: aggressive dip & reversal", scanners=["M1", "M4", "M7"], min_liq=0.15, coins=LIQUID_PLUS,
               size=137_500, size_band=(55_000, 165_000), sl_band=(2.5, 5.0), tp_band=(2.0, 8.0), base_sl=4.0,
               base_tps=[(3.0, 0.5), (6.0, 0.5)], max_pos=5, max_exposure=0.30, cluster_caps={"meme": 2},
               daily_stop=0.06, entries_per_hour=6, sl_cooldown=(2, 120), entry="market", time_stop=1440,
               regime_ok=None, need_btc_bull=False, dd_cap=0.15),
    "C2": dict(name="Low risk: mean-reversion confirmer", scanners=["M1", "M2"], min_liq=0.25, coins=LIQUID,
               size=55_000, size_band=(55_000, 82_500), sl_band=(2.0, 4.0), tp_band=(1.5, 4.0), base_sl=3.0,
               base_tps=[(2.0, 1.0)], max_pos=4, max_exposure=0.12, cluster_caps={}, daily_stop=0.02,
               entries_per_hour=8, sl_cooldown=(2, 120), entry="limit", limit_valid=15, time_stop=720,
               regime_ok=("range", "trend_up"), need_btc_bull=False, dd_cap=0.04),
    "C3": dict(name="Analyst + risk manager (risk-based sizing)", scanners=["M4", "M1", "M5"], min_liq=0.15,
               coins=LIQUID_PLUS, risk_tmn=2_500, size_band=(55_000, 200_000), sl_band=(1.5, 4.0), tp_r=(1.0, 2.0),
               max_pos=6, max_exposure=0.30, heat_cap=7_500, cluster_caps={"meme": 2, "major": 2, "other": 2},
               daily_stop=0.05, entries_per_hour=6, loss_pause=(3, 60), entry="market", be_at_r=1.0,
               time_stop=1440, regime_ok=None, need_btc_bull=False, dd_cap=0.05),
    "C4": dict(name="Shield: min drawdown", scanners=["M4", "M1"], min_liq=0.30,
               coins=["BTCTMN", "SOLTMN", "DOGETMN", "PEPETMN", "ETHTMN", "BNBTMN"],
               size=55_000, size_band=(55_000, 55_000), sl_band=(2.0, 3.0), tp_band=(1.5, 3.0), base_sl=2.5,
               base_tps=[(2.0, 1.0)], max_pos=2, max_exposure=0.05, cluster_caps={}, daily_stop=0.01,
               entries_per_hour=4, sl_cooldown=(1, 240), entry="limit", limit_valid=15, time_stop=480, be_pct=1.0,
               regime_ok=("range", "trend_up"), need_btc_bull=False, circuit_btc15=-0.012, ml_gate=0.6, dd_cap=0.02),
    "C5": dict(name="LLM-native benchmark", scanners=["MOM"], min_liq=0.25, coins=LIQUID,
               size=82_500, size_band=(55_000, 110_000), sl_band=(1.5, 4.0), tp_band=(1.5, 6.0), base_sl=2.0,
               base_tps=[(4.0, 1.0)], max_pos=4, max_exposure=0.18, cluster_caps={}, daily_stop=0.05,
               entries_per_hour=4, sl_cooldown=(2, 120), entry="market", time_stop=240, mom_every=240, mom_top=2,
               regime_ok=None, need_btc_bull=False, dd_cap=0.10),
    "C6": dict(name="Orchestrator + memory", scanners=["M1", "M2", "M4", "M5", "M7"], min_liq=0.15,
               coins=LIQUID_PLUS, max_pos=6, max_exposure=0.30, cluster_caps={"meme": 2}, daily_stop=0.04,
               entries_per_hour=6, sl_cooldown=(2, 120), router_every=30, dd_cap=0.06,
               profile_by_regime={"trend_up": "C3", "range": "C2", "down": "C4", "dead": None}),
    # Live run 2026-10-22/23 (docs/IMPROVEMENT_PROPOSAL.md §5.2): one agent on the full 5M. Arm on the first M1
    # signal, trigger on a repeat signal or a >= 8% drop from the 6h high; the LLM picks the size tier.
    "C7": dict(name="Unified evidence-calibrated dip agent (LLM decides)", scanners=["M1"], native=True,
               min_liq=0.0, coins=LIQUID,
               size=150_000, size_band=(65_000, 220_000),
               size_tiers={"probe": 65_000, "standard": 150_000, "high": 220_000},
               risk_cap_pct=0.003, heat_cap_pct=0.015, stop_limit_offset=0.015,   # eff = sl + 1.5% + 0.6% fees
               sl_band=(4.5, 5.0), base_sl=5.0, tp_band=(5.0, 7.0), base_tps=[(6.0, 1.0)], time_stop=1440,
               be_pct=0, trail_atr=0, time_stop_min_profit=0, limit_valid=10,     # explicit: no BE / trailing
               max_pos=6, max_exposure=0.50, cluster_caps={"meme": 3}, entries_per_hour=3, sl_cooldown=(1, 240),
               daily_stop=0.99, dd_soft=0.02, dd_hard=0.04, dd_resume=0.03,
               btc15_block=-0.005, arm_window_min=360, arm_dd_pct=-8.0,
               night_hours=(0, 6), night_tier_cap="standard",
               entry="market", regime_ok=None, need_btc_bull=False, dd_cap=0.04),
}

PROFILE_SCANNERS = {"C1": {"M1", "M4", "M7"}, "C2": {"M1", "M2"}, "C3": {"M4", "M1", "M5"}, "C4": {"M4", "M1"},
                    "C7": {"M1"}}


def cluster(sym: str) -> str:
    return "meme" if sym in MEMES else ("major" if sym in MAJORS else "other")
