#!/usr/bin/env python3
"""Upcoming events management for MarginMise.

Provides a centralized catalog of event categories and a clean CRUD interface
for the upcoming-events calendar. Events are stored in the existing
``local_events`` table and are used by margin memory to learn how different
event types affect restaurant sales.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from phase3_features import Phase3Service, now_iso


# Standard event categories used across the app
EVENT_CATEGORIES = [
    ("Concert", "Live music / concert in the area"),
    ("Promotion", "In-store or neighborhood promotion"),
    ("Holiday", "Public holiday"),
    ("Bad Weather", "Severe weather advisory"),
    ("Construction", "Road / parking / access construction"),
    ("Local Event", "Fair, festival, parade, community event"),
    ("Sports", "Home game / tournament / race"),
    ("Other", "Anything not listed above"),
]

EVENT_CATEGORY_DEFAULT = "Local Event"

# Recommended sales-impact defaults by category (percent)
CATEGORY_IMPACT_HINTS: dict[str, float] = {
    "Concert": 25.0,
    "Promotion": 15.0,
    "Holiday": 20.0,
    "Bad Weather": -20.0,
    "Construction": -15.0,
    "Local Event": 10.0,
    "Sports": 18.0,
    "Other": 0.0,
}


@dataclass
class EventEntry:
    event_id: str
    event_name: str
    event_date: str
    end_date: str
    category: str
    expected_sales_impact_percent: float
    source: str
    notes: str
    external_uid: str | None = None


def get_categories() -> list[tuple[str, str]]:
    """Return the list of (category, description) tuples for UI dropdowns."""
    return list(EVENT_CATEGORIES)


def category_impact_hint(category: str) -> float:
    """Return the recommended default sales-impact percent for a category."""
    return CATEGORY_IMPACT_HINTS.get(category, 0.0)


def create_event(
    phase3: Phase3Service,
    event_name: str,
    event_date: str,
    *,
    end_date: str | None = None,
    category: str = EVENT_CATEGORY_DEFAULT,
    impact_percent: float | None = None,
    notes: str = "",
    source: str = "Manual",
    external_uid: str | None = None,
) -> EventEntry:
    """Create a new upcoming event via Phase3Service.

    If ``impact_percent`` is None, the category hint is used.
    """
    if impact_percent is None:
        impact_percent = category_impact_hint(category)

    event_id = phase3.add_event(
        event_name,
        event_date,
        end_date=end_date,
        category=category,
        impact_percent=impact_percent,
        notes=notes,
        source=source,
        external_uid=external_uid,
    )

    # Fetch back the created event by ID
    rows = phase3.list_events(limit=300)
    row = next((r for r in rows if r["event_id"] == event_id), None)
    return EventEntry(
        event_id=event_id,
        event_name=event_name,
        event_date=row["event_date"] if row else event_date,
        end_date=row["end_date"] if row else (end_date or event_date),
        category=row["category"] if row else category,
        expected_sales_impact_percent=float(row["expected_sales_impact_percent"]) if row else float(impact_percent),
        source=row["source"] if row else source,
        notes=row["notes"] if row else notes,
        external_uid=row["external_uid"] if row else None,
    )


def list_upcoming(phase3: Phase3Service, days: int = 90) -> list[EventEntry]:
    """Return events from today through the next ``days`` days."""
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    rows = phase3.list_events(start=start, end=end, limit=300)
    return [
        EventEntry(
            event_id=row["event_id"],
            event_name=row["event_name"],
            event_date=row["event_date"],
            end_date=row["end_date"],
            category=row["category"] if row["category"] else EVENT_CATEGORY_DEFAULT,
            expected_sales_impact_percent=float(row["expected_sales_impact_percent"] or 0),
            source=row["source"] if row["source"] else "Manual",
            notes=row["notes"] if row["notes"] else "",
            external_uid=row["external_uid"] if row["external_uid"] else None,
        )
        for row in rows
    ]


def update_event_impact(phase3: Phase3Service, event_id: str, impact_percent: float) -> None:
    """Update the sales-impact estimate for an existing event."""
    from phase3_features import now_iso
    with phase3.workspace.connect() as conn:
        conn.execute(
            "UPDATE local_events SET expected_sales_impact_percent=?, updated_at=? WHERE event_id=?",
            (f"{impact_percent:.2f}", now_iso(), event_id),
        )


def delete_event(phase3: Phase3Service, event_id: str) -> None:
    """Remove an event from the calendar."""
    with phase3.workspace.connect() as conn:
        conn.execute("DELETE FROM local_events WHERE event_id=?", (event_id,))
