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
    DEEPSEEK_API_KEY    — required for LLM filtering (both profiles)
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
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# DeepSeek serves an Anthropic-compatible Messages API, so the anthropic SDK is reused.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
_DEFAULT_MODEL = "deepseek-v4-pro"

# USD per 1M tokens at PEAK rates; off-peak is half (see _is_off_peak).
# Doubles as the allow-list of models: DeepSeek silently remaps unknown
# "claude-*" names to its own models instead of rejecting them.
_DEEPSEEK_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 1.32, "cached_input": 0.044, "output": 3.96},
    "deepseek-flash": {"input": 0.30, "cached_input": 0.006, "output": 1.20},
}

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

_CONFIG_PATH = Path(__file__).parent / "config.json"
_CONFIG_LOAD_ERROR = ""
try:
    _CONFIG: dict = json.loads(_CONFIG_PATH.read_text())
except (FileNotFoundError, json.JSONDecodeError) as _e:
    _CONFIG_LOAD_ERROR = f"Could not load config.json: {_e}"
    _CONFIG = {}

# Alert thresholds in USD (see format_slack_message).
_COST_ALARM_USD = float(_CONFIG.get("cost_alarm_usd", 0.10))
_LOW_BALANCE_USD = float(_CONFIG.get("low_balance_usd", 1.00))

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
        "career benefit an attendee will gain. Err on the side of inclusion: "
        "when in doubt, include the event."
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
        "precise, and never breathless. Two sentences maximum.\n\n"
        "Begin every summary with exactly one emoji followed by a space, and use no other "
        "emojis in it: 🎞️ for film, 🏛️ for classical music, theatre, and cultural "
        "institutions, 🎷 for jazz and other music. For the few standout picks — no more "
        "than one in four — use 🌟 instead.\n\n"
        "Be selective: only events with genuine artistic merit or clear keyword alignment "
        "qualify."
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
        SystemExit: If the required webhook URL env var is not set. Other
            config mistakes are reported by config_errors() so they can be
            posted to Slack.
    """
    if profile == "tech":
        webhook_url = os.getenv("TECH_WEBHOOK_URL", "")
        fetchers: list[BaseEventFetcher] = [LumaFetcher()]
    else:  # fun
        webhook_url = os.getenv("FUN_WEBHOOK_URL", "")
        fetchers = [ChanCentreFetcher(), ViffFetcher(), CinemathequeFetcher()]

    keywords = _load_keywords(profile)
    model = _CONFIG.get(f"{profile}_model", _DEFAULT_MODEL)

    if not webhook_url:
        env_var = "TECH_WEBHOOK_URL" if profile == "tech" else "FUN_WEBHOOK_URL"
        logging.getLogger("vancouver-events-bot").error(
            "Missing required environment variable: %s", env_var
        )
        sys.exit(1)

    return webhook_url, model, keywords, fetchers


def config_errors(profile: str, model: str, keywords: list[str]) -> list[str]:
    """Return human-readable config mistakes that should stop the run.

    Args:
        profile:  Active profile name.
        model:    Model ID resolved from config.json.
        keywords: Keyword list resolved from config.json.

    Returns:
        A list of error messages; empty if the config is usable.
    """
    errors: list[str] = []
    if _CONFIG_LOAD_ERROR:
        errors.append(_CONFIG_LOAD_ERROR)
    if not DEEPSEEK_API_KEY:
        errors.append("Missing required environment variable: DEEPSEEK_API_KEY")
    if model not in _DEEPSEEK_PRICING:
        errors.append(
            f"Unsupported model '{model}' for profile '{profile}' in config.json. "
            f"Use one of: {', '.join(_DEEPSEEK_PRICING)}."
        )
    if not keywords and not _CONFIG_LOAD_ERROR:
        errors.append(f"No keywords configured for profile '{profile}' in config.json.")
    return errors


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


@dataclass
class LLMUsage:
    model_id: str
    input_tokens: int = 0          # cache misses only
    cached_input_tokens: int = 0
    output_tokens: int = 0         # includes thinking tokens
    budget_hit: bool = False
    cost_usd: float = 0.0
    off_peak: bool = True
    retries: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cached_input_tokens + self.output_tokens


@dataclass
class DeepSeekBalance:
    currency: str      # "USD" or "CNY"
    total: float
    is_available: bool  # False once the balance can't cover API calls

    @property
    def is_low(self) -> bool:
        if not self.is_available:
            return True
        return self.currency == "USD" and self.total < _LOW_BALANCE_USD


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
# Step 3: LLM filtering via DeepSeek (Anthropic-compatible API)
# ---------------------------------------------------------------------------

_GROUNDING_RULES = (
    "Use each event's venue to tell what kind of event it is: an event at a cinema "
    "is a film screening, even if its title sounds like a concert or a play. Never "
    "guess what an event is about from its title: base every judgment and summary on "
    "the description, the tags, or facts you know for certain about that specific work."
)


def _slim_events_json(events: list[Event]) -> str:
    """Serialize only the fields the LLM needs to judge and describe events.

    Venue is included because some sources (e.g. Cinematheque) provide no
    description, leaving the title as the only other clue to the event type.

    Args:
        events: Events to include in the prompt.

    Returns:
        A JSON array string of title, venue, description, and tags per event.
    """
    slim_events = [
        {"title": e.title, "venue": e.location, "description": e.description, "tags": e.tags}
        for e in events
    ]
    return json.dumps(slim_events, indent=2)


def build_filter_prompt(events: list[Event], keywords: list[str]) -> str:
    """Build the user-turn prompt for the LLM filtering step.

    Only the fields needed for relevance judgment are included (title, venue,
    description, tags). All other fields are stripped to reduce input token cost.

    Args:
        events:   All candidate events to be evaluated.
        keywords: The profile-specific keywords to match against.

    Returns:
        A formatted prompt string ready to be sent as the user message.
    """
    events_json = _slim_events_json(events)
    keywords_str = ", ".join(f'"{k}"' for k in keywords)

    return f"""Your task:
1. Review the list of events below. {_GROUNDING_RULES}
2. For each event, decide if it is relevant to ANY of these keywords: {keywords_str}.
   - Be inclusive: partial matches, related themes, and implied relevance all count.
   - The venue alone is not a match: a film at an art-house cinema still needs its
     own description or known content to fit a keyword. If an event has no
     description and you don't know the specific work, leave it out.
3. For each RELEVANT event, write a one- or two-sentence recommendation explaining
   why someone interested in those topics should attend.
4. Return a JSON array. Include ONLY relevant events. Each object must have:
   - "title": string (exact title from input)
   - "summary": string (your recommendation)

Respond with ONLY the JSON array, no markdown fences, no extra text.

Events:
{events_json}"""


_MAX_TOKENS = 32768
_SUMMARY_MAX_TOKENS = 8192


def _matches_any_keyword(event: Event, keywords: list[str]) -> bool:
    """Return True if any keyword appears verbatim in the event's text fields.

    Args:
        event:    The event to check.
        keywords: Keywords to match against.

    Returns:
        True if at least one keyword is found (case-insensitive).
    """
    haystack = f"{event.title} {event.description} {' '.join(event.tags)}".lower()
    return any(kw.lower() in haystack for kw in keywords)


def build_summary_prompt(events: list[Event]) -> str:
    """Build a summary-only prompt for events whose relevance is already confirmed.

    Args:
        events: Events that passed the keyword pre-filter.

    Returns:
        A prompt string asking the LLM only to write summaries, not to filter.
    """
    return f"""These events are already confirmed relevant. For each, write a one- or two-sentence recommendation explaining why someone interested should attend. {_GROUNDING_RULES}

Return a JSON array where each object has:
- "title": string (exact title from input)
- "summary": string (your recommendation)

Respond with ONLY the JSON array, no markdown fences, no extra text.

Events:
{_slim_events_json(events)}"""


def _parse_llm_json(raw_text: str) -> list[dict] | None:
    """Extract the first JSON array of objects from the LLM's response.

    Tolerates markdown fences, lead-in or trailing prose, and a wrapping
    object such as {"events": [...]}.

    Args:
        raw_text: Raw text content from the LLM response.

    Returns:
        Parsed list of dicts, or None if no such array is found.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", raw_text):
        try:
            value, _ = decoder.raw_decode(raw_text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    log.error("LLM response contained no JSON array of objects. Raw output:\n%s", raw_text)
    return None


def _title_key(title: str) -> str:
    """Normalise a title so cosmetic differences don't break matching.

    Folds case and Unicode compatibility forms, and drops punctuation, quote
    styles, dashes, and emoji, so “Jazz Night” — 🎷 matches "jazz night".

    Args:
        title: An event title.

    Returns:
        The normalised matching key.
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", folded).split()) or title


def _build_filtered_events(
    parsed: list[dict], source_events: list[Event]
) -> tuple[list[FilteredEvent], list[str]]:
    """Match LLM output items back to original Event objects.

    Titles are compared via _title_key. When several source events share a
    title (e.g. repeat screenings), each returned item claims the next unused
    one so later dates aren't lost.

    Args:
        parsed:       List of dicts with 'title' and 'summary' keys from LLM.
        source_events: The original events that were sent to the LLM.

    Returns:
        A (filtered_events, unknown_titles) tuple. Titles that match no
        source event are treated as hallucinations and dropped.
    """
    by_key: dict[str, list[Event]] = {}
    for e in source_events:
        by_key.setdefault(_title_key(e.title), []).append(e)
    claimed: set[int] = set()

    result: list[FilteredEvent] = []
    unknown_titles: list[str] = []
    for item in parsed:
        candidates = by_key.get(_title_key(str(item.get("title", ""))), [])
        if not candidates:
            log.warning("LLM returned unknown event title: %s", item.get("title"))
            unknown_titles.append(str(item.get("title")))
            continue
        original = next((e for e in candidates if id(e) not in claimed), candidates[0])
        claimed.add(id(original))
        result.append(FilteredEvent(event=original, summary=item.get("summary", "")))
    return result, unknown_titles


class IncompleteStreamError(Exception):
    """The response stream closed before the message finished."""


_RETRYABLE_STATUS_CODES = {408, 409, 429}


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient failures worth retrying.

    Retries timeouts, connection drops, rate limits (429), server errors and
    overload (5xx), and error events sent mid-stream after a 200 response.
    Client errors such as a bad key (401), empty balance (402), or invalid
    request (400/422) are permanent and fail immediately.

    Args:
        exc: The exception raised by the API call.

    Returns:
        True if the call should be retried.
    """
    if isinstance(exc, (anthropic.APIConnectionError, IncompleteStreamError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code
        return code in _RETRYABLE_STATUS_CODES or code >= 500 or code < 400
    return False


def _stream_with_retry(
    client: anthropic.Anthropic, max_attempts: int = 3, **kwargs
) -> tuple[anthropic.types.Message, int]:
    """Stream a message with exponential backoff on transient failures.

    Args:
        client:       Anthropic SDK client pointed at DeepSeek.
        max_attempts: Total attempts before giving up.
        **kwargs:     Forwarded to client.messages.stream.

    Returns:
        A (message, retries) tuple, where retries is the number of failed
        attempts before the successful one.

    Raises:
        anthropic.APIError or IncompleteStreamError: If the error is permanent
            or every attempt fails. The caller lets it reach the fatal-error
            Slack alert.
    """
    delay = 30
    for attempt in range(1, max_attempts + 1):
        try:
            with client.messages.stream(**kwargs) as stream:
                try:
                    message = stream.get_final_message()
                except AssertionError:  # SDK asserts when the stream closes before message_start
                    message = None
            if message is None or message.stop_reason is None:
                raise IncompleteStreamError("Response stream closed before the message finished")
            return message, attempt - 1
        except (anthropic.APIError, IncompleteStreamError) as exc:
            if attempt == max_attempts or not _is_retryable(exc):
                log.error(
                    "DeepSeek call failed on attempt %d/%d (not retrying): %s",
                    attempt, max_attempts, exc,
                )
                raise
            log.warning(
                "DeepSeek call failed on attempt %d/%d; retrying in %ds: %s",
                attempt, max_attempts, delay, exc,
            )
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def _is_off_peak(now: datetime) -> bool:
    """Return True if DeepSeek bills off-peak (half price) at the given time.

    Peak is 01:00–04:00 and 06:00–10:00 UTC, Monday–Friday. Chinese public
    holidays are also off-peak but are not modelled, so the estimate can run
    high on those days.

    Args:
        now: A timezone-aware datetime.

    Returns:
        True if the time falls outside DeepSeek's peak windows.
    """
    utc = now.astimezone(timezone.utc)
    if utc.weekday() >= 5:
        return True
    return not (1 <= utc.hour < 4 or 6 <= utc.hour < 10)


def _estimate_cost_usd(
    model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int, off_peak: bool
) -> float:
    """Estimate the USD cost of DeepSeek usage from list prices.

    Args:
        model:               A key of _DEEPSEEK_PRICING.
        input_tokens:        Input tokens that missed the cache.
        cached_input_tokens: Input tokens served from the cache.
        output_tokens:       Output tokens, including thinking.
        off_peak:            Whether off-peak (half-price) rates apply.

    Returns:
        Estimated cost in USD.
    """
    price = _DEEPSEEK_PRICING[model]
    cost = (
        input_tokens * price["input"]
        + cached_input_tokens * price["cached_input"]
        + output_tokens * price["output"]
    ) / 1_000_000
    return cost / 2 if off_peak else cost


def filter_events_with_llm(
    events: list[Event],
    keywords: list[str],
    profile: str,
    api_key: str,
    model: str,
) -> tuple[list[FilteredEvent], str, LLMUsage]:
    """Filter events using a DeepSeek model with a profile-specific system prompt.

    Both LLM calls use the same model. DeepSeek caches repeated prompt
    prefixes automatically, so the shared system prompt needs no cache markup.

    Args:
        events:   Candidate events collected by the fetchers.
        keywords: Profile-specific keywords to match against.
        profile:  Active profile name ('tech' or 'fun'); selects the system prompt.
        api_key:  DeepSeek API key.
        model:    DeepSeek model ID (a key of _DEEPSEEK_PRICING).

    Returns:
        A (filtered_events, model_id, llm_usage) tuple.
    """
    if not events:
        return [], "", LLMUsage(model_id=model)

    # The SDK's own silent retries are disabled so every retry is logged and counted.
    client = anthropic.Anthropic(api_key=api_key, base_url=DEEPSEEK_BASE_URL, max_retries=0)
    system_prompt = PROFILE_SYSTEM_PROMPTS[profile]
    usage = LLMUsage(model_id=model, off_peak=_is_off_peak(datetime.now(timezone.utc)))

    def record(resp: anthropic.types.Message, retries: int, max_tokens: int) -> None:
        usage.model_id = resp.model
        usage.input_tokens += resp.usage.input_tokens
        usage.cached_input_tokens += resp.usage.cache_read_input_tokens or 0
        usage.output_tokens += resp.usage.output_tokens
        usage.budget_hit |= resp.stop_reason == "max_tokens" or resp.usage.output_tokens >= max_tokens
        usage.retries += retries

    # Stage 1: keyword pre-filter — split into confirmed-relevant vs ambiguous.
    keyword_matched = [e for e in events if _matches_any_keyword(e, keywords)]
    unmatched = [e for e in events if not _matches_any_keyword(e, keywords)]
    log.info(
        "Pre-filter: %d keyword-matched (summary only), %d unmatched (LLM filter).",
        len(keyword_matched), len(unmatched),
    )

    filtered: list[FilteredEvent] = []

    # Stage 2a: summary-only call for keyword-matched events. DeepSeek thinks by
    # default, so thinking must be disabled explicitly.
    if keyword_matched:
        log.info("Requesting summaries for %d pre-matched event(s)...", len(keyword_matched))
        resp, retries = _stream_with_retry(
            client,
            model=model,
            max_tokens=_SUMMARY_MAX_TOKENS,
            system=system_prompt,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": build_summary_prompt(keyword_matched)}],
        )
        record(resp, retries, _SUMMARY_MAX_TOKENS)

        raw_text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        log.debug("Summary response:\n%s", raw_text)
        parsed = _parse_llm_json(raw_text)
        if parsed is None:
            usage.warnings.append(
                f"Summary call returned invalid JSON — {len(keyword_matched)} "
                "keyword-matched event(s) are missing from Part A."
            )
        else:
            matched, unknown = _build_filtered_events(parsed, keyword_matched)
            filtered.extend(matched)
            if unknown:
                usage.warnings.append(_unknown_titles_warning("Summary", unknown))
            if len(matched) < len(keyword_matched):
                usage.warnings.append(
                    f"Summary call covered {len(matched)} of {len(keyword_matched)} "
                    "keyword-matched event(s); the rest are missing from Part A."
                )

    # Stage 2b: filter+summary call for unmatched events (with thinking).
    # DeepSeek ignores budget_tokens; max_tokens caps thinking + answer together.
    if unmatched:
        log.info(
            "Sending %d unmatched event(s) to LLM for filtering (profile=%s, model=%s)...",
            len(unmatched), profile, model,
        )
        resp, retries = _stream_with_retry(
            client,
            model=model,
            max_tokens=_MAX_TOKENS,
            system=system_prompt,
            thinking={"type": "enabled"},
            messages=[{"role": "user", "content": build_filter_prompt(unmatched, keywords)}],
        )
        record(resp, retries, _MAX_TOKENS)

        raw_text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        log.debug("Filter response:\n%s", raw_text)
        parsed = _parse_llm_json(raw_text)
        if parsed is None:
            usage.warnings.append(
                f"Filter call returned invalid JSON — none of the {len(unmatched)} "
                "non-keyword event(s) could be evaluated."
            )
        else:
            matched, unknown = _build_filtered_events(parsed, unmatched)
            filtered.extend(matched)
            if unknown:
                usage.warnings.append(_unknown_titles_warning("Filter", unknown))

    if usage.retries:
        usage.warnings.append(
            f"DeepSeek calls needed {usage.retries} retry(ies) — see the Actions log for errors."
        )
    usage.cost_usd = _estimate_cost_usd(
        model, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.off_peak
    )

    log.info(
        "LLM approved %d / %d event(s). Tokens: %d in, %d cached, %d out. Est. cost: $%.4f (%s).",
        len(filtered), len(events), usage.input_tokens, usage.cached_input_tokens,
        usage.output_tokens, usage.cost_usd, "off-peak" if usage.off_peak else "peak",
    )
    for warning in usage.warnings:
        log.warning(warning)
    return filtered, usage.model_id, usage


def fetch_deepseek_balance(api_key: str) -> DeepSeekBalance | None:
    """Fetch the account's remaining DeepSeek balance.

    This call is free. Any failure is logged and returns None so the digest
    still posts.

    Args:
        api_key: DeepSeek API key.

    Returns:
        The balance (USD entry preferred), or None if it couldn't be read.
    """
    try:
        resp = requests.get(
            DEEPSEEK_BALANCE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        infos = data.get("balance_infos") or []
        info = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
        if info is None:
            raise ValueError("response has no balance_infos")
        return DeepSeekBalance(
            currency=info["currency"],
            total=float(info["total_balance"]),
            is_available=bool(data.get("is_available", True)),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch DeepSeek balance: %s", exc)
        return None


def _unknown_titles_warning(call_name: str, titles: list[str]) -> str:
    """Describe titles the model returned that matched no fetched event.

    Args:
        call_name: 'Summary' or 'Filter'.
        titles:    The unmatched titles.

    Returns:
        A one-line warning for Part B diagnostics.
    """
    shown = "; ".join(f"“{t}”" for t in titles[:3])
    more = f" (+{len(titles) - 3} more)" if len(titles) > 3 else ""
    return (
        f"{call_name} call returned {len(titles)} title(s) that matched no fetched "
        f"event and were dropped: {shown}{more}"
    )


# ---------------------------------------------------------------------------
# Step 4: Format Slack message
# ---------------------------------------------------------------------------

def _minutes_of_day(time_str: str) -> int | None:
    """Parse a time like "7:30 PM" into minutes after midnight.

    Args:
        time_str: An event time, e.g. "7:30 PM" or "TBD".

    Returns:
        Minutes after midnight, or None if no time is found.
    """
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).upper() == "PM":
        hour += 12
    return hour * 60 + int(m.group(2))


def _showing_sort_key(event: Event) -> tuple[str, int]:
    """Order events by date, then time, with unknown times last in their day."""
    minutes = _minutes_of_day(event.time)
    return event.date, minutes if minutes is not None else 24 * 60


def _group_showings(filtered_events: list[FilteredEvent]) -> list[list[FilteredEvent]]:
    """Merge repeat showings of the same title into one entry, in date order.

    Each group's showings are sorted, and groups are ordered by their
    earliest showing.

    Args:
        filtered_events: LLM-approved events with summaries.

    Returns:
        Groups of showings; every group shares a title (per _title_key).
    """
    groups: dict[str, list[FilteredEvent]] = {}
    for fe in filtered_events:
        groups.setdefault(_title_key(fe.event.title), []).append(fe)
    for showings in groups.values():
        showings.sort(key=lambda fe: _showing_sort_key(fe.event))
    return sorted(groups.values(), key=lambda showings: _showing_sort_key(showings[0].event))


def format_slack_message(
    filtered_events: list[FilteredEvent],
    fetcher_results: list[FetcherResult],
    start_date: date,
    end_date: date,
    model: str,
    keywords: list[str],
    profile: str,
    llm_usage: LLMUsage | None = None,
    balance: DeepSeekBalance | None = None,
) -> dict:
    """Build a Slack Block Kit payload.

    Any issue (fetcher failure, LLM warning, cost alarm, low balance) gets a
    line in Part B and is counted in a banner under the header and in the
    notification preview text.

    Args:
        filtered_events: LLM-approved events with summaries.
        fetcher_results: Raw results from all fetchers (for diagnostics).
        start_date:      Start of the event window.
        end_date:        End of the event window.
        model:           Model ID used for filtering (shown in diagnostics).
        keywords:        Active keyword list (shown in Part A header).
        profile:         Active profile name (shown in digest header).
        llm_usage:       Token usage, cost estimate, and warnings from the LLM
                         calls (warnings in Part B, the rest in the footer).
        balance:         Remaining DeepSeek balance (footer; low balance is
                         flagged in Part B), or None if it couldn't be read.

    Returns:
        A dict representing the Slack Block Kit payload.
    """
    week_str = f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"
    profile_label = profile.capitalize()
    title = f"Vancouver Events ({profile_label}): {week_str}"

    issue_lines: list[str] = []
    zero_event_fetchers: list[str] = []
    for r in fetcher_results:
        if not r.success:
            issue_lines.append(f":x: *{r.name}* — FAILED: `{r.error}`")
        elif len(r.events) == 0:
            zero_event_fetchers.append(r.name)
            issue_lines.append(
                f":rotating_light: *{r.name}* — "
                f"0 events returned | raw={r.raw_count} | *CHECK IMMEDIATELY*"
            )
        else:
            missing = sum(1 for e in r.events if not e.description)
            if missing * 2 > len(r.events):
                issue_lines.append(
                    f":warning: *{r.name}* — {missing} of {len(r.events)} event(s) have no "
                    "description, so the LLM may reject them. Check the event page layout."
                )
    if llm_usage:
        issue_lines.extend(f":warning: *LLM* — {w}" for w in llm_usage.warnings)
        if llm_usage.cost_usd > _COST_ALARM_USD:
            issue_lines.append(
                f":rotating_light: *Cost* — this run cost an estimated ${llm_usage.cost_usd:.4f}, "
                f"above the ${_COST_ALARM_USD:.2f} alarm (set `cost_alarm_usd` in config.json). "
                "Check the token counts below and your DeepSeek usage page."
            )
    if balance and balance.is_low:
        state = "can no longer cover API calls" if not balance.is_available else (
            f"is below the ${_LOW_BALANCE_USD:.2f} alert (set `low_balance_usd` in config.json)"
        )
        issue_lines.append(
            f":warning: *Balance* — DeepSeek balance {_format_balance(balance)} {state}. "
            "Top up at <https://platform.deepseek.com/top_up|platform.deepseek.com>."
        )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title, "emoji": True},
        },
    ]
    if issue_lines:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":warning: *{len(issue_lines)} issue(s) this run — see Part B.*",
            }],
        })
    blocks.append({"type": "divider"})

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
        for showings in _group_showings(filtered_events):
            first = showings[0]
            date_lines = []
            for fe in showings:
                e = fe.event
                day_of_week = datetime.strptime(e.date, "%Y-%m-%d").strftime("%A")
                date_lines.append(
                    f":date: {e.date} ({day_of_week})  :clock1: {e.time}  "
                    f":round_pushpin: {e.location}"
                )
            event_text = (
                f"*<{first.event.url}|{first.event.title}>*\n"
                + "\n".join(date_lines)
                + f"\n_{first.summary}_"
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

    diag_lines.extend(
        f":white_check_mark: *{r.name}* — "
        f"{len(r.events)} in-area event(s)  _(raw: {r.raw_count})_"
        for r in fetcher_results
        if r.success and r.events
    )
    diag_lines.extend(issue_lines)

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

    if llm_usage:
        budget_label = "Hit" if llm_usage.budget_hit else "OK"
        rate_label = "off-peak" if llm_usage.off_peak else "peak"
        cost_label = f"${llm_usage.cost_usd:.4f}" if llm_usage.cost_usd >= 0.0001 else "<$0.0001"
        footer_text = (
            f":robot_face: `{llm_usage.model_id}` | "
            f"Tokens: {llm_usage.total_tokens:,} "
            f"(in {llm_usage.input_tokens:,} · cached {llm_usage.cached_input_tokens:,} · "
            f"out {llm_usage.output_tokens:,}) | "
            f"Est. cost: {cost_label} ({rate_label}) | "
            f"Budget: {budget_label}"
        )
    else:
        footer_text = f":robot_face: `{model}`"
    if balance:
        footer_text += f" | Balance: {_format_balance(balance)}"
    context_elements: list[dict] = [{"type": "mrkdwn", "text": footer_text}]
    if profile == "tech":
        context_elements.append({
            "type": "mrkdwn",
            "text": ":information_source: Events sourced from Luma's curated iCal feed — check <https://lu.ma/vancouver|lu.ma/vancouver> manually for completeness.",
        })
    blocks.append({"type": "context", "elements": context_elements})

    # Slack shows `text` in push notifications and previews when blocks are present.
    preview = f"{title} — ⚠️ {len(issue_lines)} issue(s), see Part B" if issue_lines else title
    return {"text": preview, "blocks": blocks}


def _format_balance(balance: DeepSeekBalance) -> str:
    """Format a balance as e.g. '$4.87' or '35.20 CNY'."""
    if balance.currency == "USD":
        return f"${balance.total:,.2f}"
    return f"{balance.total:,.2f} {balance.currency}"


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
        "text": "Vancouver Events Bot — FATAL ERROR, no digest posted",
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


def post_config_errors_to_slack(errors: list[str], profile: str, webhook_url: str) -> None:
    """Send an alert for config mistakes that stopped the run before it began.

    Args:
        errors:      Messages from config_errors().
        profile:     Active profile name.
        webhook_url: Slack incoming webhook URL.
    """
    bullet_list = "\n".join(f"• {e}" for e in errors)
    payload = {
        "text": f"Vancouver Events Bot ({profile}) — CONFIG ERROR, no digest posted",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":rotating_light: *Vancouver Events Bot — CONFIG ERROR* :rotating_light:\n"
                        f"The `{profile}` run stopped before fetching events, so no digest was posted.\n"
                        f"{bullet_list}\n"
                        "Fix `config.json` or the GitHub secrets, then re-run the workflow."
                    ),
                },
            },
        ],
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10,
                      headers={"Content-Type": "application/json"})
    except Exception as slack_exc:  # noqa: BLE001
        log.error("Could not send config-error alert to Slack: %s", slack_exc)


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
        minutes = _minutes_of_day(event.time)
        if minutes is None:
            return False  # time unknown — exclude
        return minutes >= 18 * 60
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

    # Load profile-specific config (exits on missing webhook — there's nowhere to alert).
    webhook_url, model, keywords, active_fetchers = load_profile_config(profile)

    errors = config_errors(profile, model, keywords)
    if errors:
        for error in errors:
            log.error(error)
        post_config_errors_to_slack(errors, profile, webhook_url)
        sys.exit(1)

    log.info("Starting run — profile=%s", profile)

    try:
        start_date, end_date = get_upcoming_week()
        log.info("Fetching events for %s to %s", start_date, end_date)

        # Step 1 — Collect events from all sources.
        fetcher_results = run_fetchers(active_fetchers, start_date, end_date)

        all_events: list[Event] = [
            event for result in fetcher_results for event in result.events
        ]

        # Deduplicate by (normalised title, date) — keeps the first occurrence.
        seen: set[tuple[str, str]] = set()
        deduped: list[Event] = []
        for event in all_events:
            key = (event.title.strip().lower(), event.date)
            if key not in seen:
                seen.add(key)
                deduped.append(event)
        if len(deduped) < len(all_events):
            log.info("Deduplicated %d duplicate(s).", len(all_events) - len(deduped))
        all_events = deduped

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
        filtered_events, model, llm_usage = filter_events_with_llm(
            all_events, keywords, profile, DEEPSEEK_API_KEY, model
        )

        # Deduplicate LLM output — the model occasionally emits the same event twice.
        seen_llm: set[tuple[str, str]] = set()
        deduped_filtered: list[FilteredEvent] = []
        for fe in filtered_events:
            key = (fe.event.title.strip().lower(), fe.event.date)
            if key not in seen_llm:
                seen_llm.add(key)
                deduped_filtered.append(fe)
        if len(deduped_filtered) < len(filtered_events):
            log.info(
                "Removed %d LLM-duplicated event(s).",
                len(filtered_events) - len(deduped_filtered),
            )
        filtered_events = deduped_filtered

        log.info(
            "Recall: %d raw → %d LLM-approved (%.0f%%)",
            raw_count,
            len(filtered_events),
            (len(filtered_events) / raw_count * 100) if raw_count else 0,
        )

        balance = fetch_deepseek_balance(DEEPSEEK_API_KEY)

        # Step 3 — Build and send Slack message.
        slack_payload = format_slack_message(
            filtered_events, fetcher_results, start_date, end_date, model, keywords, profile,
            llm_usage, balance,
        )
        post_to_slack(slack_payload, webhook_url)

        log.info("Done. %d relevant event(s) posted.", len(filtered_events))

    except Exception as exc:
        log.critical("Fatal error — bot aborted: %s", exc, exc_info=True)
        post_fatal_error_to_slack(exc, webhook_url)
        sys.exit(1)


if __name__ == "__main__":
    main()
