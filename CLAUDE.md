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
  - `tech` → one `LumaFetcher(name, calendar_id)` per entry in `config.json` → `tech_luma_calendars`, then `CerebralValleyFetcher()`
  - `fun`  → `[ChanCentreFetcher(), ViffFetcher(), CinemathequeFetcher()]`
- Cerebral Valley: reads `api.cerebralvalley.ai/v1/public/event/pull` (the undocumented API behind cerebralvalley.ai/events and /hackathons; no key) with `approved=true&locations=…&startDateTime=…&endDateTime=…` (ISO UTC), paging `limit`/`offset` until `lastPage`. Two pulls, deduped by `id`: `locations=REMOTE` and `locations=Vancouver` (a free-text match, so Vancouver, WA listings are dropped). Hackathons need no extra pull — `hackathon=true` only narrows the same results, and the /hackathons page server-renders just a gallery of past ones. `startDateTime` is UTC as `YYYY-MM-DD HH:MM:SS`, ignore the `timeZone` field. The API also returns events that began before the window (ongoing hackathons) — dropped. Remote pull: `location: "Other"` listings have turned out to be in-person elsewhere — dropped; events must start 10:00–20:00 Vancouver time (inclusive) unless they run 24h+ (multi-day hackathons). Vancouver pull: no start-hour rule; location is the `venue` string. All kept events are marked `Event.pre_approved`.
- `Event.pre_approved`: set by sources whose events are all on-topic. Stage 1 sends them to the summary-only call with keyword-matched events, so they're never judged or rejected by the LLM.
- Luma: each calendar's iCal feed is `api2.luma.com/ics/get?entity=calendar&id=cal-…`. The `cal-…` ID is in the calendar page's HTML (`luma.com/<slug>`). An event on a host's "Personal" calendar has no slug, but its event page shows that calendar's ID, which works the same way. Events outside the Metro Vancouver bounding box (from GEO) and events marked postponed/cancelled (in STATUS or the title) are dropped. Calendar feeds omit the description body (only URL, address, hosts), so `LumaFetcher` calls `fill_descriptions()` with `parse_luma_description()`, which reads `description_mirror` from the event page's `__NEXT_DATA__` and falls back to the truncated meta description. Don't use the `luma.com/vancouver` discover feed — it lists only Luma's featured picks, mostly social events.
- A download or parse failure raises, so Part B shows the calendar as FAILED. A calendar whose feed loads but has nothing in the window sets `empty_ok` and shows as a `:white_circle:` line, not an alert; only an empty feed triggers the 0-events alert.
- Credentials via `os.getenv` only. Never hardcode.
- Listing pages without descriptions (Cinematheque, ChanCentre) call `fill_descriptions()` from `fetchers/base.py`: one GET per unique detail URL, parsed by a per-site `parse_<site>_description(html)`, capped at 500 chars. A failed page is logged and leaves `""`. Part B warns when over half of a fetcher's events have no description.
- VIFF: the listing is paginated (`/whats-on/page/N/`) and cards show only a few dates ("View N more dates" → `#book`). `ViffFetcher` reads every page, then replaces those films' card dates with the full list from the film page. A failed listing page fails the fetcher; a failed film page keeps the card dates.

## LLM Configuration
- **Provider**: DeepSeek only, via its Anthropic-compatible endpoint (`DEEPSEEK_BASE_URL` in `main.py`) using the `anthropic` SDK. Key: `DEEPSEEK_API_KEY`. Never use Claude model IDs — DeepSeek silently remaps `claude-*` names to its own models instead of erroring.
- **Model**: one per profile in `config.json` (currently `deepseek-v4-pro` for both; fallback `deepseek-v4-pro`), used for **both** LLM calls. Allowed IDs are the keys of `_DEEPSEEK_PRICING`; anything else exits at startup. Don't move the filter call to `deepseek-flash` without comparing recall first.
- **Prompt payload**: `_slim_events_json` sends title, venue, description, tags. Venue tells the model the event type. The keyword pre-filter must not search venue — "Chan Shun Concert Hall" would match "Concert".
- **Keyword pre-filter matches whole words only** (case-insensitive, plural "s"/"es" allowed). A substring match let "AI" hit "painting", "available", "Fairways", etc., and keyword-matched events skip relevance judging entirely — so a false match goes straight into Part A.
- **No title guessing**: `_GROUNDING_RULES` (in both prompts) forbids judging or summarizing an event from its title alone; the venue alone is not a keyword match. This only works if events have descriptions — see below.
- **Summary-only call** (keyword-matched events): thinking must be explicitly `{"type": "disabled"}` — DeepSeek thinks by default.
- **Filter+summary call**: `{"type": "enabled"}` (improves recall). DeepSeek ignores `budget_tokens` and treats `adaptive` as `enabled`; `max_tokens` caps thinking + answer together.
- **Prompt caching**: automatic on DeepSeek; no `cache_control` markup. Both calls share the system prompt prefix and model, so the second call can hit the cache.
- **Retries**: `_stream_with_retry` owns all retries (SDK `max_retries=0`). It retries 408/409/429/5xx, connection errors, and incomplete streams; it fails fast on 400/401/402/422. Final failures reach the fatal-error Slack alert.
- **No silent drops**: invalid JSON, unmatched titles, missing or blank summaries, and retries are added to `LLMUsage.warnings` and shown in Part B.
- **Parsing**: `_parse_llm_json` takes the first JSON array of objects anywhere in the reply (tolerates fences and prose). Titles are matched via `_title_key` (case, punctuation, quotes, dashes, emoji ignored); a title with no exact match falls back to the closest source title at ≥90% similarity (`difflib`), so LLM typos don't drop events. Repeat titles claim distinct events.
- **Summary length**: both calls use `_SUMMARY_LENGTH` (one or two sentences, 20–30 words) so blurbs read evenly whichever call wrote them.
- **Summary-call retry**: the summary call runs without thinking and occasionally returns malformed JSON or echoes the input events without summaries, so either is retried once (`_SUMMARY_ATTEMPTS = 2`). Only a second failure becomes a Part B warning; a second echo still keeps the events (flagged as blank summaries). `build_summary_prompt` puts the events first and the instructions last — a prompt ending in a long JSON array (e.g. 500-char Luma descriptions) gets echoed back. The filter call isn't retried; it thinks first and costs ~10× more.
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
- Part A is sorted by date, then time (`TBD` last within its day). Repeat showings of the same title merge into one entry: one `:date:` line per showing, then the earliest showing's summary.
- Three parts: **Part A** (curated events), **Part B** (diagnostics), **footer** (metadata).
- **Splitting (all profiles)**: Slack rejects a whole message over 50 blocks, so `post_to_slack()` runs every payload through `split_slack_payload()`. Blocks are never cut (one event = one block); everything from the last divider on (Part B + footer) stays together in the last message; follow-ups start with a `(cont.) <title>` header and the same preview text. Messages post ~1s apart; a failed part raises with the count already posted (→ fatal alert). Keep each Part A event a single block so this holds.
- Part B's funnel line shows `Auto-included: N · LLM-judged: M` before `LLM approved` (Auto-included = keyword matches + pre-approved events), so a pre-filter that matches too much is visible at a glance.
- Footer format: `:robot_face: \`[model_id]\` | Tokens: [total] (in [miss] · cached [hit] · out [output]) | Est. cost: $[usd] ([off-peak/peak]) | Budget: [OK/Hit] | Balance: $[usd]`
  - All counts sum both calls. `out` includes thinking tokens. Cost is an estimate from list prices. Balance is omitted if it couldn't be fetched.
- Every issue (failed or 0-event fetcher, LLM warning, cost alarm, low balance) is one line in Part B. When there are any, a `:warning: *N issue(s) this run — see Part B.*` banner sits under the header, and the payload's top-level `text` (used for Slack notifications) says the same.
- Tech profile footer also notes that events come from the configured Luma calendars (with the count) plus Cerebral Valley's remote and Vancouver AI events and hackathons, and reminds the reader how to discover more: browse luma.com/vancouver or a liked event, open the host's calendar page, add its `cal-…` ID.
- Language: professional, concise, English only.

## Testing & Validation
- Every new fetcher needs a parsing test in `tests/test_<fetcher_name>.py` using a mock payload.
- A fetcher returning 0 events after a successful HTTP response must be flagged in Part B diagnostics (distinct from a hard error).
- Log and display raw event count (pre-filter) and post-filter count for recall visibility.
