"""
Meetup fetcher — placeholder implementation with mock data.

Replace the body of fetch_events() with real Meetup API calls
(or web scraping) once you have an API key / access method.
"""

from datetime import date, timedelta

from .base import BaseEventFetcher, Event


class MeetupFetcher(BaseEventFetcher):
    name = "Meetup"

    # Mock event templates; dates are filled in dynamically so they always
    # fall inside the requested week.
    _MOCK_EVENTS = [
        {
            "title": "Vancouver AI & Machine Learning Meetup",
            "time": "6:30 PM",
            "location": "Northeastern University Vancouver, 410 W Georgia St",
            "description": (
                "Monthly gathering of AI/ML practitioners in Vancouver. "
                "This month's topic: fine-tuning LLMs for production workloads. "
                "Talks, demos, and networking included."
            ),
            "url": "https://www.meetup.com/vancouver-ai-ml/",
            "tags": ["AI", "machine learning", "networking"],
            "day_offset": 1,  # Tuesday of the week
        },
        {
            "title": "Vancouver Startup Founders Circle",
            "time": "7:00 PM",
            "location": "Bootcamp Canada, 422 Richards St, Vancouver",
            "description": (
                "Peer support group for early-stage startup founders. "
                "Share wins, get feedback on blockers, and connect with "
                "other entrepreneurs building in Vancouver."
            ),
            "url": "https://www.meetup.com/vancouver-startup-founders/",
            "tags": ["startup", "entrepreneurship", "networking"],
            "day_offset": 2,  # Wednesday
        },
        {
            "title": "Data Science Vancouver — Monthly Workshop",
            "time": "6:00 PM",
            "location": "Science World, 1455 Quebec St, Vancouver",
            "description": (
                "Hands-on workshop covering exploratory data analysis with Python. "
                "Bring a laptop. Topics: pandas, Polars, and modern data stack tools."
            ),
            "url": "https://www.meetup.com/data-science-vancouver/",
            "tags": ["data science", "Python", "workshop"],
            "day_offset": 3,  # Thursday
        },
        {
            "title": "Mindful Tech: Meditation for Knowledge Workers",
            "time": "12:00 PM",
            "location": "Online (Zoom)",
            "description": (
                "A guided 30-minute meditation session designed for developers "
                "and knowledge workers. No experience required. Focus on breath "
                "awareness and reducing screen fatigue."
            ),
            "url": "https://www.meetup.com/mindful-tech-vancouver/",
            "tags": ["meditation", "wellness", "remote"],
            "day_offset": 0,  # Monday
        },
        {
            "title": "Vancouver React & Frontend Developers",
            "time": "6:30 PM",
            "location": "Hootsuite HQ, 5 E 8th Ave, Vancouver",
            "description": (
                "Monthly meetup for frontend engineers. Lightning talks on "
                "React 19 features, Signals, and performance optimisation."
            ),
            "url": "https://www.meetup.com/vancouver-react/",
            "tags": ["frontend", "React", "JavaScript"],
            "day_offset": 4,  # Friday
        },
    ]

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        events: list[Event] = []

        for template in self._MOCK_EVENTS:
            event_date = start_date + timedelta(days=template["day_offset"])
            # Skip if the calculated date falls outside the requested window.
            if not (start_date <= event_date <= end_date):
                continue

            events.append(
                Event(
                    title=template["title"],
                    date=event_date.isoformat(),
                    time=template["time"],
                    location=template["location"],
                    description=template["description"],
                    url=template["url"],
                    source=self.name,
                    tags=list(template["tags"]),
                )
            )

        return events
