# CLAUDE.md — Project Conventions

## Build & Run Commands
- Run main script: `venv/bin/python main.py`
- Install dependencies: `venv/bin/pip install -r requirements.txt`
- Check syntax: `venv/bin/python -m py_compile main.py`
- Run script with profiles: `venv/bin/python main.py --profile tech` or `venv/bin/python main.py --profile fun`

## Python Style Guidelines
- **Type hints**: All function signatures must include type hints.
- **Data structures**: Use `dataclasses` for internal data modeling.
- **Documentation**: Use Google-style docstrings for all public methods.
- **Error handling**: Every fetcher must implement a safe-fail mechanism using try-except. Log errors with source context; never crash the main orchestrator.
- **Logging**: Use the `logging` module instead of `print()`.

## Project Architecture
- **Modular fetchers**: All scrapers/API clients must reside in `fetchers/` and inherit from `BaseEventFetcher`.
- **Profile-driven fetcher loading**: `ACTIVE_FETCHERS` is NOT a static global. It is dynamically
  assembled at runtime by `load_profile_config()` based on the `--profile` argument:
  - `tech` profile → `[LumaFetcher()]`
  - `fun` profile  → `[ChanCentreFetcher()]`
  Adding a fetcher to a profile means updating `load_profile_config()` in `main.py`, not a top-level list.
- **Environment**: Sensitive keys must only be read from `.env` via `os.getenv`. Never hardcode credentials.

## LLM Configuration
- **Model selection**: Prefer Sonnet-class models for the best cost/quality balance. Upgrade to
  Opus only if filtering quality degrades noticeably. Do not silently downgrade to Haiku —
  filtering precision will suffer.
- **Thinking**: Always enable extended thinking for the LLM filter step; it meaningfully improves
  recall accuracy when matching events to keywords.
  - Opus 4.6: use `thinking={"type": "adaptive"}`
  - Sonnet and below: use `thinking={"type": "enabled", "budget_tokens": <N>}`
- **Goal**: High Recall at the fetch/filter stage; High Precision in the final Slack digest.

## Slack Message Formatting
- Uses Slack Block Kit (JSON blocks payload).
- Event lines follow this format:
  ```
  *<url|Event Title>*
  :date: YYYY-MM-DD (Weekday)  :clock1: HH:MM AM/PM  :round_pushpin: Location
  _LLM-generated recommendation summary_
  ```
- The digest has two sections:
  - **Part A — Curated Events**: LLM-filtered events with summaries.
  - **Part B — System Diagnostics**: fetcher success/failure stats.
- Keep language professional and concise. English only.

## Testing & Validation
- **Fetcher tests**: Every new fetcher must include a basic parsing test using a mock HTML snippet (or mock JSON payload for API-based fetchers). Place tests in `tests/test_<fetcher_name>.py`.
- **Threshold check**: If a fetcher returns 0 events after a successful HTTP response, it must be flagged as a potential failure (`⚠️ 0 events — possible scraping breakage`) in the Part B diagnostics block, distinct from a hard error.
- **Recall transparency**: Log and display both the raw event count (all fetched events before geo/date filtering) and the post-filter count so regressions in recall are immediately visible in Slack diagnostics.
