# CLAUDE.md — Project Conventions

## Commands
- Run: `venv/bin/python main.py --profile [tech|fun]`
- Install deps: `venv/bin/pip install -r requirements.txt`
- Check syntax: `venv/bin/python -m py_compile main.py`

## Python Style
- Type hints on all function signatures.
- `dataclasses` for internal data modeling.
- Google-style docstrings on all public methods.
- `logging` module only — never `print()`.
- Every fetcher must safe-fail via try-except; never crash the main orchestrator.

## Architecture
- All fetchers live in `fetchers/` and inherit from `BaseEventFetcher`.
- Fetchers are assembled at runtime by `load_profile_config()` — not a static global:
  - `tech` → `[LumaFetcher()]`
  - `fun`  → `[ChanCentreFetcher(), ViffFetcher(), CinemathequeFetcher()]`
- Credentials via `os.getenv` only. Never hardcode.

## LLM Configuration
- **Filter+summary call**: Sonnet-class with extended thinking. Upgrade to Opus only if recall degrades; never downgrade to Haiku — precision will suffer.
- **Summary-only call** (keyword-matched events): `claude-haiku-4-5-20251001`. No relevance judgment is made, so lower precision is acceptable.
- **Thinking**: always enabled for the filter step (improves recall). Opus: `{"type": "adaptive"}`; Sonnet: `{"type": "enabled", "budget_tokens": <N>}`.
- **Goal**: High Recall at fetch/filter; High Precision in the final Slack digest.

## Slack Formatting
- Block Kit JSON payload. Event line format:
  ```
  *<url|Event Title>*
  :date: YYYY-MM-DD (Weekday)  :clock1: HH:MM AM/PM  :round_pushpin: Location
  _LLM-generated summary_
  ```
- Three parts: **Part A** (curated events), **Part B** (diagnostics), **footer** (metadata).
- Footer format: `_Model: [model_id] | Tokens: [total_used]/[max_tokens] | Budget: [OK/Hit]_`
- Tech profile footer also includes the Luma iCal completeness reminder.
- Language: professional, concise, English only.

## Testing & Validation
- Every new fetcher needs a parsing test in `tests/test_<fetcher_name>.py` using a mock payload.
- A fetcher returning 0 events after a successful HTTP response must be flagged in Part B diagnostics (distinct from a hard error).
- Log and display raw event count (pre-filter) and post-filter count for recall visibility.
