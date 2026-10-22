# Wallex Agentic Trading Bot — Starkoch Hackathon

Algorithmic, AI-assisted spot trading engine for the Wallex challenge of the Starkoch Agentic AI hackathon.

- Capital: 5,000,000 TMN (Wallex prop/credit account), spot only, 2-day trading window
- All trades are placed by the algorithm; every trade is logged with its reason
- Project notes, constraints and verified Wallex facts: [NOTES.md](NOTES.md)
- Strategy research (236 strategies, filtered and scored): [research/](research/)

## Setup
1. Python 3.10+
2. Copy `.env.example` to `.env` and put your Wallex API key in it (`WALLEX_API_KEY=...`). Never commit `.env`.
3. Read-only API check: `python wallex_check.py` → writes `wallex_check_result.json` (git-ignored)

## Structure
| Path | What |
|---|---|
| `NOTES.md` | Everything learned so far (rules, fees, limits, open questions) |
| `wallex_check.py` | Read-only API checker (no orders) |
| `research/` | Strategy catalogue, scores, allowed/disabled markets |
