# Vancouver Events Bot

A weekly Slack digest of curated Vancouver events, filtered by an LLM for relevance.

## How it works

1. **Fetchers** collect events from configured sources for the upcoming two-week window
2. **Keyword pre-filter** — events with a verbatim keyword match in title, description, or tags are auto-included and sent to the LLM for a summary only (thinking disabled)
3. **LLM filtering** evaluates the remaining ambiguous events against profile-specific keywords with thinking enabled, and writes a one- or two-sentence recommendation for each approved event. Both LLM calls use the profile's DeepSeek model from `config.json` (`deepseek-v4-pro` by default) through DeepSeek's Anthropic-compatible API. DeepSeek caches the shared system prompt automatically.
4. **Slack digest** is posted in three parts: curated events (Part A), system diagnostics (Part B, including any LLM warnings such as invalid JSON, dropped titles, or retries), and a metadata footer showing the model, token counts (input, cached input, output), estimated cost, and whether the output budget was hit

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

Keywords and each profile's model live in `config.json`:

```json
{
  "tech_model": "deepseek-v4-pro",
  "fun_model": "deepseek-v4-pro",
  "tech_keywords": ["AI", "Machine Learning", "Startup", ...],
  "fun_keywords": ["Film Noir", "Jazz", "Art House Cinema", ...]
}
```

If a profile's model is missing, it falls back to `deepseek-v4-pro`. Only DeepSeek models listed in `_DEEPSEEK_PRICING` in `main.py` (`deepseek-v4-pro`, `deepseek-flash`) are accepted; anything else, including Claude model IDs, stops the run at startup.

## Cost

The footer's cost estimate uses DeepSeek list prices from `_DEEPSEEK_PRICING` in `main.py`. Update that table if DeepSeek changes its prices. Peak hours (Mon–Fri 01:00–04:00 and 06:00–10:00 UTC) cost twice as much as off-peak. Both scheduled runs fall on Sunday, which is entirely off-peak. Avoid manual `workflow_dispatch` runs during peak hours, and keep any new cron schedule off-peak.

## Adding a new fetcher

1. Create `fetchers/<name>_fetcher.py` inheriting from `BaseEventFetcher`
2. Implement `fetch_events(start_date, end_date) -> list[Event]` with a try/except safe-fail
3. Register it in `load_profile_config()` in `main.py`
4. Add a parsing test in `tests/test_<name>.py`

## Environment variables

| Variable | Required by |
|----------|------------|
| `DEEPSEEK_API_KEY` | Both profiles |
| `TECH_WEBHOOK_URL` | `tech` profile |
| `FUN_WEBHOOK_URL` | `fun` profile |
