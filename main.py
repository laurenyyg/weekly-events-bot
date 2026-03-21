"""
Vancouver Weekly Events Aggregator
===================================
Run every Sunday to fetch, filter, and post events for the upcoming week
(Monday – Sunday) to Slack.

Usage:
    python main.py

Environment variables (in .env):
    ANTHROPIC_API_KEY   — required for LLM filtering
    SLACK_WEBHOOK_URL   — required to post to Slack
"""

import json
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import anthropic
import requests
from dotenv import load_dotenv
import os

from fetchers import BaseEventFetcher, Event, MeetupFetcher

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "AI,startup,data science,meditation").split(",") if k.strip()]

# Register all active fetchers here. Add new fetcher classes to this list.
ACTIVE_FETCHERS: list[BaseEventFetcher] = [
    MeetupFetcher(),
    # EventbriteFetcher(),  # <-- add future fetchers here
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vancouver-events-bot")

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class FetcherResult:
    name: str
    success: bool
    events: list[Event]
    error: str = ""


@dataclass
class FilteredEvent:
    event: Event
    summary: str       # LLM-generated recommendation


# ---------------------------------------------------------------------------
# Step 1: Compute the upcoming week (Monday – Sunday)
# ---------------------------------------------------------------------------

def get_upcoming_week() -> tuple[date, date]:
    """Return (monday, sunday) for the week starting this coming Monday."""
    today = date.today()
    # days_until_monday: 0 if today is Monday, else distance to next Monday
    days_until_monday = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_until_monday)
    sunday = monday + timedelta(days=6)
    return monday, sunday


# ---------------------------------------------------------------------------
# Step 2: Run all fetchers, isolating failures
# ---------------------------------------------------------------------------

def run_fetchers(
    fetchers: list[BaseEventFetcher],
    start_date: date,
    end_date: date,
) -> list[FetcherResult]:
    results: list[FetcherResult] = []

    for fetcher in fetchers:
        log.info("Running fetcher: %s", fetcher.name)
        try:
            events = fetcher.fetch_events(start_date, end_date)
            log.info("  → %d event(s) found", len(events))
            results.append(FetcherResult(name=fetcher.name, success=True, events=events))
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            log.error("  → Fetcher '%s' failed: %s", fetcher.name, error_msg)
            log.debug(traceback.format_exc())
            results.append(
                FetcherResult(name=fetcher.name, success=False, events=[], error=error_msg)
            )

    return results


# ---------------------------------------------------------------------------
# Step 3: LLM filtering via Anthropic API
# ---------------------------------------------------------------------------

def build_filter_prompt(events: list[Event], keywords: list[str]) -> str:
    events_json = json.dumps([e.to_dict() for e in events], indent=2)
    keywords_str = ", ".join(f'"{k}"' for k in keywords)

    return f"""You are a helpful assistant curating a weekly events digest for a Vancouver professional.

Your task:
1. Review the list of events below.
2. For each event, decide if it is relevant to ANY of these keywords: {keywords_str}.
   - Be inclusive: partial matches, related themes, and implied relevance all count.
   - An "AI" event is relevant even if it says "machine learning" or "LLM".
   - A "startup" event is relevant if it covers entrepreneurship, founders, or early-stage companies.
3. For each RELEVANT event, write a one- or two-sentence recommendation explaining why a Vancouver professional interested in those topics should attend.
4. Return a JSON array. Include ONLY relevant events. Each object must have:
   - "title": string (exact title from input)
   - "date": string (exact date from input)
   - "time": string
   - "location": string
   - "url": string
   - "source": string
   - "summary": string (your recommendation)

Respond with ONLY the JSON array, no markdown fences, no extra text.

Events:
{events_json}"""


def filter_events_with_llm(
    events: list[Event],
    keywords: list[str],
    api_key: str,
) -> tuple[list[FilteredEvent], str]:
    if not events:
        return [], ""

    model = "claude-opus-4-6"
    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_filter_prompt(events, keywords)

    log.info("Sending %d event(s) to LLM for filtering...", len(events))

    with client.messages.stream(
        model=model,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    raw_text = next(
        (block.text for block in response.content if block.type == "text"), ""
    ).strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("LLM returned invalid JSON: %s\nRaw output:\n%s", exc, raw_text)
        return []

    filtered: list[FilteredEvent] = []
    for item in parsed:
        # Find the original Event object by title so we preserve all fields.
        original = next((e for e in events if e.title == item.get("title")), None)
        if original is None:
            # LLM hallucinated a title — skip it.
            log.warning("LLM returned unknown event title: %s", item.get("title"))
            continue
        filtered.append(FilteredEvent(event=original, summary=item.get("summary", "")))

    log.info("LLM approved %d / %d event(s).", len(filtered), len(events))
    return filtered, model


# ---------------------------------------------------------------------------
# Step 4: Format Slack message
# ---------------------------------------------------------------------------

def format_slack_message(
    filtered_events: list[FilteredEvent],
    fetcher_results: list[FetcherResult],
    start_date: date,
    end_date: date,
    model: str,
) -> dict:
    """Build a Slack Block Kit payload."""

    week_str = f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Vancouver Events: {week_str}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    # --- Part A: Curated Events ---
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Part A — Curated Events*  _(filtered for: "
                + ", ".join(f"`{k}`" for k in KEYWORDS)
                + ")_",
            },
        }
    )

    if not filtered_events:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No relevant events found this week._"},
            }
        )
    else:
        for fe in filtered_events:
            e = fe.event
            day_of_week = datetime.strptime(e.date, "%Y-%m-%d").strftime("%A")
            event_text = (
                f"*<{e.url}|{e.title}>*\n"
                f":date: {e.date} ({day_of_week})  :clock1: {e.time}  :round_pushpin: {e.location}\n"
                f"_{fe.summary}_"
            )
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": event_text}}
            )

    blocks.append({"type": "divider"})

    # --- Part B: System Diagnostics ---
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Part B — System Diagnostics*"},
        }
    )

    total_events = sum(len(r.events) for r in fetcher_results)
    approved = len(filtered_events)

    diag_lines = [
        f"*Sources polled:* {len(fetcher_results)}  |  "
        f"*Events collected:* {total_events}  |  "
        f"*Events approved:* {approved}"
    ]

    for r in fetcher_results:
        if r.success:
            diag_lines.append(f"*{r.name}* — {len(r.events)} event(s) fetched")
        else:
            diag_lines.append(f"*{r.name}* — FAILED: `{r.error}`")

    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(diag_lines)},
        }
    )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":robot_face: Filtered by `{model}`"}
            ],
        }
    )

    return {"blocks": blocks}


# ---------------------------------------------------------------------------
# Step 5: Post to Slack
# ---------------------------------------------------------------------------

def post_to_slack(payload: dict, webhook_url: str) -> None:
    log.info("Posting to Slack...")
    resp = requests.post(
        webhook_url,
        json=payload,
        timeout=10,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    log.info("Slack response: %s", resp.text)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    # Validate env vars early.
    missing = [name for name, val in [
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("SLACK_WEBHOOK_URL", SLACK_WEBHOOK_URL),
    ] if not val]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    start_date, end_date = get_upcoming_week()
    log.info("Fetching events for %s to %s", start_date, end_date)

    # Step 1 — Collect events from all sources.
    fetcher_results = run_fetchers(ACTIVE_FETCHERS, start_date, end_date)

    all_events: list[Event] = [
        event for result in fetcher_results for event in result.events
    ]
    log.info("Total events collected: %d", len(all_events))

    # Step 2 — LLM filtering.
    filtered_events, model = filter_events_with_llm(all_events, KEYWORDS, ANTHROPIC_API_KEY)

    # Step 3 — Build and send Slack message.
    slack_payload = format_slack_message(filtered_events, fetcher_results, start_date, end_date, model)
    post_to_slack(slack_payload, SLACK_WEBHOOK_URL)

    log.info("Done. %d relevant event(s) posted.", len(filtered_events))


if __name__ == "__main__":
    main()
