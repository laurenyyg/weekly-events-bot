# CLAUDE.md — Project Conventions

## Build & Run Commands
- Activate environment: `source venv/bin/activate`
- Run main script: `python main.py`
- Install dependencies: `pip install -r requirements.txt`
- Check syntax: `python -m py_compile main.py`

## Python Style Guidelines
- **Type hints**: All function signatures must include type hints.
- **Data structures**: Use `dataclasses` for internal data modeling.
- **Documentation**: Use Google-style docstrings for all public methods.
- **Error handling**: Every fetcher must implement a safe-fail mechanism using try-except. Log errors with source context; never crash the main orchestrator.
- **Logging**: Use the `logging` module instead of `print()`.

## Project Architecture
- **Modular fetchers**: All scrapers/API clients must reside in `fetchers/` and inherit from `BaseEventFetcher`.
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
