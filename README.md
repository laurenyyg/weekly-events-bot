# Vancouver Events Bot

A weekly Slack digest of curated Vancouver events, filtered by an LLM for relevance.

## How it works

1. **Fetchers** collect events from configured sources for the upcoming two-week window
2. **Keyword pre-filter** — events with a whole-word keyword match in title, description, or tags are auto-included and sent to the LLM for a summary only (thinking disabled)
3. **LLM filtering** evaluates the remaining ambiguous events against profile-specific keywords with thinking enabled, and writes a one- or two-sentence recommendation for each approved event. Both LLM calls use the profile's DeepSeek model from `config.json` (`deepseek-v4-pro` by default) through DeepSeek's Anthropic-compatible API. DeepSeek caches the shared system prompt automatically. The summary call puts its instructions after the event list, because with thinking disabled the model tends to copy a long trailing list back instead of summarizing it. It retries once if the reply is invalid JSON or any event comes back without a summary. If the last reply still lacks summaries, those events stay in Part A without a blurb and Part B names them; if it is still invalid JSON, the auto-included events are left out and Part B says so.
4. **Slack digest** is posted in three parts: curated events (Part A), system diagnostics (Part B, including fetcher problems, LLM warnings such as invalid JSON, missing summaries, dropped titles, or retries, and cost or balance alerts), and a metadata footer showing the model, token counts (input, cached input, output), estimated cost, whether the output budget was hit, and the remaining DeepSeek balance. The `tech` footer also notes which sources were polled and how to find more Luma calendars. If anything went wrong, a one-line banner under the header and the Slack notification text say how many issues to check in Part B.

## Alerts

| Situation | Slack | GitHub Actions |
|---|---|---|
| Transient DeepSeek error that a retry fixes | Digest posts, with a retry warning in Part B | Green |
| Summary reply still missing summaries after the retry | Digest posts; affected events have no blurb and are named in Part B | Green |
| Summary reply still invalid JSON after the retry | Digest posts without the auto-included events, with a Part B warning | Green |
| Permanent DeepSeek error (bad key, empty balance) or all retries fail | FATAL ERROR alert, no digest | Red (failure email) |
| Config mistake (unreadable `config.json`, missing `DEEPSEEK_API_KEY`, non-DeepSeek model, no keywords, no Luma calendars for `tech`) | CONFIG ERROR alert, no digest | Red (failure email) |
| Missing Slack webhook, or Slack itself is down | Nothing | Red (failure email) |
| Run cost above `cost_alarm_usd` / balance below `low_balance_usd` | Digest posts, with a Part B alert | Green |

## Profiles

| Profile | Sources | Schedule | Slack channel |
|---------|---------|----------|---------------|
| `tech` | Luma community calendars (`tech_luma_calendars`), Cerebral Valley remote AI events | Every Sunday 04:00 UTC (Sat 8:00 PM PST / 9:00 PM PDT) — automated, manual trigger also available | Tech events |
| `fun` | Chan Centre, VIFF, Cinematheque | Every Sunday 08:05 UTC (Sun 00:05 PST / 01:05 PDT) — every other week (next: Nov 15, 2026); manual triggers always run | Fun events |

### Keywords

Each profile's keywords live in `config.json` (`tech_keywords`, `fun_keywords`); that file is the source of truth if this list drifts.

| Profile | Keywords |
|---------|----------|
| `tech` | AI, Machine Learning, Startup, Data Science, Analytics, Product Management, Notion, Claude, OpenAI, Google, artificial intelligence |
| `fun` | Film Noir, Restored Classic, World Cinema, Jazz, Classical Music, Concert, Theatre, Avant-garde, Criterion Collection, Art House Cinema, Ballet, Tango, Feminism |

Keywords are used twice:

1. **Pre-filter.** An event whose title, description, or tags contain a keyword as a whole word (case-insensitive; a plural "s"/"es" also counts) is accepted without LLM judging and only gets a summary. "AI" matches "AI-assisted" but not "painting". The venue is not searched, so "Chan Shun Concert Hall" doesn't match "Concert".
2. **LLM filter.** Every other event goes to the LLM, which keeps it if it relates to any keyword, including partial or implied matches. The `tech` prompt errs on the side of inclusion; the `fun` prompt asks for genuine artistic merit or clear keyword alignment.

Cerebral Valley events skip both steps: every event there is AI-related, so they are marked pre-approved and only get a summary. Part B's "Auto-included" count covers keyword matches plus pre-approved events.

### Date and time conditions

| Profile | Date window | Time / day condition | Location |
|---------|-------------|----------------------|----------|
| `tech` (Luma) | Two weeks: the next Monday through the Sunday 13 days later | None — any day, any time | Metro Vancouver only (Luma events outside the bounding box, e.g. Seoul or Nanaimo, are dropped). Postponed or cancelled events are dropped. |
| `tech` (Cerebral Valley) | Same two-week window; events that started before it (ongoing hackathons) are dropped | Must start between 10:00 AM and 8:00 PM, inclusive; how long it runs doesn't matter | Remote only (`locations=REMOTE`); listings with location "Other" are dropped because they have turned out to be in-person |
| `fun` | Same two-week window | Friday from 6:00 PM, or any time Saturday or Sunday. Monday–Thursday events are dropped, and so are Friday events with no listed time. | The three Vancouver venues only |

The window always starts on the Monday after the run date, so a Saturday-night `tech` run covers the following Monday through Sunday a week later. Times are Vancouver local time.

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
  "cost_alarm_usd": 0.10,
  "low_balance_usd": 1.00,
  "tech_luma_calendars": {"BC + AI Events": "cal-9QLMVT9CQVtX1u0", ...},
  "tech_keywords": ["AI", "Machine Learning", "Startup", ...],
  "fun_keywords": ["Film Noir", "Jazz", "Art House Cinema", ...]
}
```

To add a Luma calendar, open its page (`luma.com/<name>`), find the `cal-…` ID in the page source, and add it under `tech_luma_calendars` with a display name. For an event that isn't on a named calendar, use the `cal-…` ID shown on the event page (the host's personal calendar).

If a profile's model is missing, it falls back to `deepseek-v4-pro`. Only DeepSeek models listed in `_DEEPSEEK_PRICING` in `main.py` (`deepseek-v4-pro`, `deepseek-flash`) are accepted; anything else, including Claude model IDs, stops the run at startup.

## Cost

`cost_alarm_usd` flags any single run whose estimated cost exceeds it (a normal run is well under a cent). `low_balance_usd` flags a DeepSeek balance below it; this check only applies to USD-denominated accounts.

The footer's cost estimate uses DeepSeek list prices from `_DEEPSEEK_PRICING` in `main.py`. Update that table if DeepSeek changes its prices. Peak hours (Mon–Fri 01:00–04:00 and 06:00–10:00 UTC) cost twice as much as off-peak. Both scheduled runs fall on Sunday, which is entirely off-peak. Avoid manual `workflow_dispatch` runs during peak hours, and keep any new cron schedule off-peak.

## Adding a new fetcher

1. Create `fetchers/<name>_fetcher.py` inheriting from `BaseEventFetcher`
2. Implement `fetch_events(start_date, end_date) -> list[Event]`. Let exceptions propagate: the orchestrator catches them and reports the source as failed in Part B.
3. Optionally set `self.raw_count` (in-range events before area filtering) and `self.empty_ok = True` once the source has loaded, so a quiet window shows as a neutral line in Part B instead of a "check immediately" alert
4. Export it from `fetchers/__init__.py` and register it in `load_profile_config()` in `main.py`
5. Add a parsing test in `tests/test_<name>_fetcher.py`

## Environment variables

| Variable | Required by |
|----------|------------|
| `DEEPSEEK_API_KEY` | Both profiles |
| `TECH_WEBHOOK_URL` | `tech` profile |
| `FUN_WEBHOOK_URL` | `fun` profile |
