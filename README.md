# Vancouver Events Bot

A weekly Slack digest of curated Vancouver events, filtered by an LLM for relevance.

## How it works

1. **Fetchers** collect events from configured sources for the upcoming two-week window
2. **Keyword pre-filter** — events with a verbatim keyword match in title, description, or tags are auto-included and sent to Haiku (summary-only, no thinking overhead)
3. **LLM filtering** (Claude Sonnet by default) evaluates the remaining ambiguous events against profile-specific keywords with extended thinking enabled, and writes a one- or two-sentence recommendation for each approved event. The system prompt is cached across both LLM calls to reduce input token cost.
4. **Slack digest** is posted in three parts: curated events (Part A), system diagnostics (Part B), and a metadata footer showing model ID, total tokens used, and whether the output budget was hit

## Profiles

| Profile | Sources | Schedule | Slack channel |
|---------|---------|----------|---------------|
| `tech` | Luma iCal | Every Sunday 04:00 UTC (Sat 9:00 PM PDT) — automated, manual trigger also available | Tech events |
| `fun` | Chan Centre, VIFF, Cinematheque | Every Sunday 08:05 UTC (Sun 00:05 PST / 01:05 PDT) — even weeks only, manual trigger also available | Fun events |

## Running locally

```bash
venv/bin/python main.py --profile tech
venv/bin/python main.py --profile fun
```

## Configuration

Keywords and model selection live in `config.json`:

```json
{
  "tech_model": "claude-sonnet-4-6",
  "tech_keywords": ["AI", "Machine Learning", "Startup", ...],
  "fun_model": "claude-sonnet-4-6",
  "fun_keywords": ["Film Noir", "Jazz", "Art House Cinema", ...]
}
```

## Adding a new fetcher

1. Create `fetchers/<name>_fetcher.py` inheriting from `BaseEventFetcher`
2. Implement `fetch_events(start_date, end_date) -> list[Event]` with a try/except safe-fail
3. Register it in `load_profile_config()` in `main.py`
4. Add a parsing test in `tests/test_<name>.py`

## Environment variables

| Variable | Required by |
|----------|------------|
| `ANTHROPIC_API_KEY` | Both profiles |
| `TECH_WEBHOOK_URL` | `tech` profile |
| `FUN_WEBHOOK_URL` | `fun` profile |
