"""
Vancouver Weekly Events Aggregator
===================================
Run every Sunday to fetch, filter, and post events for the upcoming week
(Monday – Sunday) to Slack.

Usage:
    python main.py [--profile {tech,fun}]

    --profile tech  (default) — tech/professional digest via TECH_WEBHOOK_URL
    --profile fun              — lifestyle/arts digest via FUN_WEBHOOK_URL

Environment variables (in .env):
    ANTHROPIC_API_KEY   — required for LLM filtering (both profiles)
    TECH_WEBHOOK_URL    — Slack webhook for the tech profile
    TECH_KEYWORDS       — comma-separated keywords for tech filtering
    FUN_WEBHOOK_URL     — Slack webhook for the fun profile
    FUN_KEYWORDS        — comma-separated keywords for fun filtering
"""

import argparse
import json
import logging
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
import os

from fetchers import (
    BaseEventFetcher,
    ChanCentreFetcher,
    CinemathequeFetcher,
    Event,
    LumaFetcher,
    ViffFetcher,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

_CONFIG_PATH = Path(__file__).parent / "config.json"
try:
    _CONFIG: dict = json.loads(_CONFIG_PATH.read_text())
except (FileNotFoundError, json.JSONDecodeError) as _e:
    logging.getLogger("vancouver-events-bot").warning(
        "Could not load config.json: %s — falling back to empty keyword lists.", _e
    )
    _CONFIG = {}

# ---------------------------------------------------------------------------
# Profile configuration
# ---------------------------------------------------------------------------

PROFILE_SYSTEM_PROMPTS: dict[str, str] = {
    "tech": (
        "You are a professional curator assembling a weekly tech events digest for "
        "Vancouver professionals. Evaluate each event strictly on its technical value: "
        "Is it relevant to software engineering, AI/ML, data science, startups, product "
        "management, or the broader tech industry? Prioritise hands-on workshops, "
        "founder networking, and talks from industry practitioners. Write recommendation "
        "summaries in a concise, professional tone — explain the specific technical or "
        "career benefit an attendee will gain. Be rigorous: only include events with "
        "clear relevance to the keywords provided."
    ),
    "fun": (
        "You are a discerning Vancouver arts curator — think New Yorker film critic meets "
        "passionate cinephile — assembling a refined weekly cultural digest. 🎬\n\n"
        "Evaluate each event with an eye for artistic distinction: Is this a rare screening "
        "of a restored classic? 🎞️ A challenging work of world cinema? A film noir gem? An "
        "art house auteur? A live jazz or classical performance of uncommon quality? 🏛️ "
        "Prioritise events that reward a cultivated palate — Film Noir, Art House, Restored "
        "Classics, and serious live performance are the gold standard.\n\n"
        "Write recommendation summaries in an elegant, sophisticated tone — evocative, "
        "precise, and never breathless. Two sentences maximum. Use emojis sparingly but "
        "purposefully (🎞️ for film, 🏛️ for cultural institutions, 🎷 for jazz/music, "
        "🌟 for standout picks). Be selective: only events with genuine artistic merit "
        "or clear keyword alignment qualify."
    ),
}


def _load_keywords(profile: str) -> list[str]:
    """Return the keyword list for the given profile from config.json.

    Args:
        profile: One of 'tech' or 'fun'.

    Returns:
        A list of keyword strings, or an empty list if the key is missing.
    """
    return _CONFIG.get(f"{profile}_keywords", [])


def load_profile_config(profile: str) -> tuple[str, str, list[str], list[BaseEventFetcher]]:
    """Return (webhook_url, model, keywords, fetchers) for the given profile.

    Args:
        profile: One of 'tech' or 'fun'.

    Returns:
        A tuple of the Slack webhook URL, model ID, keyword list, and active
        fetchers for the selected profile.

    Raises:
        SystemExit: If the required webhook URL env var is not set.
    """
    if profile == "tech":
        webhook_url = os.getenv("TECH_WEBHOOK_URL", "")
        fetchers: list[BaseEventFetcher] = [LumaFetcher()]
    else:  # fun
        webhook_url = os.getenv("FUN_WEBHOOK_URL", "")
        fetchers = [ChanCentreFetcher(), ViffFetcher(), CinemathequeFetcher()]

    keywords = _load_keywords(profile)
    model = _CONFIG.get(f"{profile}_model", "claude-sonnet-4-6")

    if not webhook_url:
        env_var = "TECH_WEBHOOK_URL" if profile == "tech" else "FUN_WEBHOOK_URL"
        logging.getLogger("vancouver-events-bot").error(
            "Missing required environment variable: %s", env_var
        )
        sys.exit(1)

    return webhook_url, model, keywords, fetchers


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
    raw_count: int = 0   # in-range events before geo/area filtering


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
    days_until_monday = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_until_monday)
    end_date = monday + timedelta(days=13)
    return monday, end_date


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
            raw_count = getattr(fetcher, "raw_count", len(events))
            if events:
                log.info("  → %d event(s) found (raw=%d)", len(events), raw_count)
            else:
                log.warning("  → 0 events returned by '%s' — possible scraping breakage", fetcher.name)
            results.append(FetcherResult(name=fetcher.name, success=True, events=events, raw_count=raw_count))
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
    """Build the user-turn prompt for the LLM filtering step.

    Args:
        events:   All candidate events to be evaluated.
        keywords: The profile-specific keywords to match against.

    Returns:
        A formatted prompt string ready to be sent as the user message.
    """
    events_json = json.dumps([e.to_dict() for e in events], indent=2)
    keywords_str = ", ".join(f'"{k}"' for k in keywords)

    return f"""Your task:
1. Review the list of events below.
2. For each event, decide if it is relevant to ANY of these keywords: {keywords_str}.
   - Be inclusive: partial matches, related themes, and implied relevance all count.
3. For each RELEVANT event, write a one- or two-sentence recommendation explaining
   why someone interested in those topics should attend.
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
    profile: str,
    api_key: str,
    model: str,
) -> tuple[list[FilteredEvent], str]:
    """Filter events using an Anthropic LLM with a profile-specific system prompt.

    Args:
        events:   Candidate events collected by the fetchers.
        keywords: Profile-specific keywords to match against.
        profile:  Active profile name ('tech' or 'fun'); selects the system prompt.
        api_key:  Anthropic API key.
        model:    Model ID to use for filtering.

    Returns:
        A (filtered_events, model_id) tuple.
    """
    if not events:
        return [], ""

    # Opus supports adaptive thinking; all other models require explicit budget.
    thinking = (
        {"type": "adaptive"}
        if model.startswith("claude-opus")
        else {"type": "enabled", "budget_tokens": 8000}
    )

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = PROFILE_SYSTEM_PROMPTS[profile]
    prompt = build_filter_prompt(events, keywords)

    log.info("Sending %d event(s) to LLM for filtering (profile=%s, model=%s)...", len(events), profile, model)

    with client.messages.stream(
        model=model,
        max_tokens=32768,
        system=system_prompt,
        thinking=thinking,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    raw_text = next(
        (block.text for block in response.content if block.type == "text"), ""
    ).strip()

    log.debug("LLM raw response:\n%s", raw_text)

    # Strip markdown code fences if the LLM wraps the output in ```json ... ```
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```", 2)[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("LLM returned invalid JSON: %s\nRaw output:\n%s", exc, raw_text)
        return [], model

    filtered: list[FilteredEvent] = []
    for item in parsed:
        original = next((e for e in events if e.title == item.get("title")), None)
        if original is None:
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
    keywords: list[str],
    profile: str,
) -> dict:
    """Build a Slack Block Kit payload.

    Args:
        filtered_events: LLM-approved events with summaries.
        fetcher_results: Raw results from all fetchers (for diagnostics).
        start_date:      Start of the event window.
        end_date:        End of the event window.
        model:           Model ID used for filtering (shown in diagnostics).
        keywords:        Active keyword list (shown in Part A header).
        profile:         Active profile name (shown in digest header).

    Returns:
        A dict representing the Slack Block Kit payload.
    """
    week_str = f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"
    profile_label = profile.capitalize()

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Vancouver Events ({profile_label}): {week_str}",
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
                + ", ".join(f"`{k}`" for k in keywords)
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

    total_raw = sum(r.raw_count for r in fetcher_results)
    total_area = sum(len(r.events) for r in fetcher_results)
    approved = len(filtered_events)

    diag_lines = [
        f"*Profile:* `{profile}`  |  "
        f"*Sources polled:* {len(fetcher_results)}  |  "
        f"*Raw (in range):* {total_raw}  →  "
        f"*In area:* {total_area}  →  "
        f"*LLM approved:* {approved}"
    ]

    zero_event_fetchers: list[str] = []
    for r in fetcher_results:
        if not r.success:
            diag_lines.append(f":x: *{r.name}* — FAILED: `{r.error}`")
        elif len(r.events) == 0:
            zero_event_fetchers.append(r.name)
            diag_lines.append(
                f":rotating_light: *{r.name}* — "
                f"0 events returned | raw={r.raw_count} | *CHECK IMMEDIATELY*"
            )
        else:
            diag_lines.append(
                f":white_check_mark: *{r.name}* — "
                f"{len(r.events)} in-area event(s)  _(raw: {r.raw_count})_"
            )

    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(diag_lines)},
        }
    )

    # Urgent alert block when any fetcher returns 0 events.
    if zero_event_fetchers:
        names = ", ".join(zero_event_fetchers)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":rotating_light::rotating_light: *URGENT — FETCHER RETURNED 0 EVENTS* :rotating_light::rotating_light:\n"
                        f"*Affected source(s): {names}*\n"
                        "The scraper may be broken or the API may have changed. "
                        "No events were collected — this week's digest is likely incomplete. "
                        "*Please investigate immediately.*"
                    ),
                },
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

def post_fatal_error_to_slack(error: Exception, webhook_url: str) -> None:
    """Send a minimal fatal-error alert to Slack.

    Args:
        error:       The unhandled exception that caused the run to abort.
        webhook_url: Slack incoming webhook URL.
    """
    tb = traceback.format_exc()
    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":rotating_light: *Vancouver Events Bot — FATAL ERROR* :rotating_light:\n"
                        f"The bot crashed and no digest was posted.\n"
                        f"*Error:* `{type(error).__name__}: {error}`"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Traceback:*\n```{tb[-2800:]}```",
                },
            },
        ]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10,
                      headers={"Content-Type": "application/json"})
    except Exception as slack_exc:  # noqa: BLE001
        log.error("Could not send fatal-error alert to Slack: %s", slack_exc)


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
# Weekend filter (VIFF and Cinematheque only)
# ---------------------------------------------------------------------------

def _passes_weekend_filter(event: Event) -> bool:
    """Return True if an event falls on Fri ≥18:00, Saturday, or Sunday.

    Used by the 'fun' profile to surface only weekend/evening picks across
    all sources.

    Args:
        event: The Event to evaluate.

    Returns:
        True if the event should be kept.
    """
    try:
        dow = datetime.strptime(event.date, "%Y-%m-%d").weekday()  # 0=Mon … 6=Sun
    except ValueError:
        return True  # unparseable date — pass through

    if dow in (5, 6):  # Saturday, Sunday
        return True
    if dow == 4:  # Friday — check time
        m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", event.time, re.IGNORECASE)
        if not m:
            return False  # time unknown — exclude
        hour = int(m.group(1))
        if m.group(3).upper() == "PM" and hour != 12:
            hour += 12
        elif m.group(3).upper() == "AM" and hour == 12:
            hour = 0
        return hour >= 18
    return False  # Mon–Thu


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed namespace with a 'profile' attribute set to 'tech' or 'fun'.
    """
    parser = argparse.ArgumentParser(
        description="Vancouver Weekly Events Aggregator — post a curated Slack digest."
    )
    parser.add_argument(
        "--profile",
        choices=["tech", "fun"],
        default="tech",
        help="Which events profile to run (default: tech).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    profile = args.profile

    # Validate shared env vars.
    if not ANTHROPIC_API_KEY:
        log.error("Missing required environment variable: ANTHROPIC_API_KEY")
        sys.exit(1)

    # Load profile-specific config (exits on missing webhook).
    webhook_url, model, keywords, active_fetchers = load_profile_config(profile)

    log.info("Starting run — profile=%s", profile)

    try:
        start_date, end_date = get_upcoming_week()
        log.info("Fetching events for %s to %s", start_date, end_date)

        # Step 1 — Collect events from all sources.
        fetcher_results = run_fetchers(active_fetchers, start_date, end_date)

        all_events: list[Event] = [
            event for result in fetcher_results for event in result.events
        ]
        raw_count = len(all_events)
        log.info("Total events collected (raw): %d", raw_count)

        # Apply weekend/Friday-evening filter for the fun profile.
        if profile == "fun":
            all_events = [e for e in all_events if _passes_weekend_filter(e)]
            weekend_removed = raw_count - len(all_events)
            if weekend_removed:
                log.info(
                    "Weekend filter: removed %d weekday event(s) (%d remaining).",
                    weekend_removed, len(all_events),
                )

        # Step 2 — LLM filtering.
        filtered_events, model = filter_events_with_llm(
            all_events, keywords, profile, ANTHROPIC_API_KEY, model
        )
        log.info(
            "Recall: %d raw → %d LLM-approved (%.0f%%)",
            raw_count,
            len(filtered_events),
            (len(filtered_events) / raw_count * 100) if raw_count else 0,
        )

        # Step 3 — Build and send Slack message.
        slack_payload = format_slack_message(
            filtered_events, fetcher_results, start_date, end_date, model, keywords, profile
        )
        post_to_slack(slack_payload, webhook_url)

        log.info("Done. %d relevant event(s) posted.", len(filtered_events))

    except Exception as exc:
        log.critical("Fatal error — bot aborted: %s", exc, exc_info=True)
        post_fatal_error_to_slack(exc, webhook_url)
        sys.exit(1)


if __name__ == "__main__":
    main()
