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
- **Provider**: DeepSeek only, via its Anthropic-compatible endpoint (`DEEPSEEK_BASE_URL` in `main.py`) using the `anthropic` SDK. Key: `DEEPSEEK_API_KEY`. Never use Claude model IDs — DeepSeek silently remaps `claude-*` names to its own models instead of erroring.
- **Model**: one per profile in `config.json` (currently `deepseek-v4-pro` for both; fallback `deepseek-v4-pro`), used for **both** LLM calls. Allowed IDs are the keys of `_DEEPSEEK_PRICING`; anything else exits at startup. Don't move the filter call to `deepseek-flash` without comparing recall first.
- **Summary-only call** (keyword-matched events): thinking must be explicitly `{"type": "disabled"}` — DeepSeek thinks by default.
- **Filter+summary call**: `{"type": "enabled"}` (improves recall). DeepSeek ignores `budget_tokens` and treats `adaptive` as `enabled`; `max_tokens` caps thinking + answer together.
- **Prompt caching**: automatic on DeepSeek; no `cache_control` markup. Both calls share the system prompt prefix and model, so the second call can hit the cache.
- **Retries**: `_stream_with_retry` owns all retries (SDK `max_retries=0`). It retries 408/409/429/5xx, connection errors, and incomplete streams; it fails fast on 400/401/402/422. Final failures reach the fatal-error Slack alert.
- **No silent drops**: invalid JSON, unmatched titles, missing summaries, and retries are added to `LLMUsage.warnings` and shown in Part B.
- **Parsing**: `_parse_llm_json` takes the first JSON array of objects anywhere in the reply (tolerates fences and prose). Titles are matched via `_title_key` (case, punctuation, quotes, dashes, emoji ignored); repeat titles claim distinct events.
- **Cost**: `_DEEPSEEK_PRICING` holds peak list prices (USD/1M tokens); off-peak is half. Keep scheduled runs off-peak (outside Mon–Fri 01:00–04:00 and 06:00–10:00 UTC). Update the table when DeepSeek changes prices.
- **Spend alerts** (thresholds in `config.json`): a run whose estimated cost exceeds `cost_alarm_usd` gets a `:rotating_light: *Cost*` line; a balance below `low_balance_usd` (via free `GET /user/balance`, USD accounts only) gets a `:warning: *Balance*` line. A failed balance fetch is logged and skipped, never fatal.
- **Config errors** (`config_errors()`: unreadable `config.json`, missing `DEEPSEEK_API_KEY`, unsupported model, empty keywords) post a CONFIG ERROR alert to Slack and exit 1. Only a missing webhook exits without a Slack alert.
- **Goal**: High Recall at fetch/filter; High Precision in the final Slack digest.

## Slack Formatting
- Block Kit JSON payload. Event line format:
  ```
  *<url|Event Title>*
  :date: YYYY-MM-DD (Weekday)  :clock1: HH:MM AM/PM  :round_pushpin: Location
  _LLM-generated summary_
  ```
- Three parts: **Part A** (curated events), **Part B** (diagnostics), **footer** (metadata).
- Footer format: `:robot_face: \`[model_id]\` | Tokens: [total] (in [miss] · cached [hit] · out [output]) | Est. cost: $[usd] ([off-peak/peak]) | Budget: [OK/Hit] | Balance: $[usd]`
  - All counts sum both calls. `out` includes thinking tokens. Cost is an estimate from list prices. Balance is omitted if it couldn't be fetched.
- Every issue (failed or 0-event fetcher, LLM warning, cost alarm, low balance) is one line in Part B. When there are any, a `:warning: *N issue(s) this run — see Part B.*` banner sits under the header, and the payload's top-level `text` (used for Slack notifications) says the same.
- Tech profile footer also includes the Luma iCal completeness reminder.
- Language: professional, concise, English only.

## Testing & Validation
- Every new fetcher needs a parsing test in `tests/test_<fetcher_name>.py` using a mock payload.
- A fetcher returning 0 events after a successful HTTP response must be flagged in Part B diagnostics (distinct from a hard error).
- Log and display raw event count (pre-filter) and post-filter count for recall visibility.
