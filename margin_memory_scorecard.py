#!/usr/bin/env python3
"""Margin Memory Scorecard — makes the system's learning visible.

Reads ``margin_memory_decisions`` and their linked outcomes to produce:
- overall learning metrics (decisions, overrides, accuracy)
- per-item pattern summaries
- weekly learning highlights
- encouraging feedback for manager overrides
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from margin_memory import MarginMemoryService
from phase3_features import now_iso


@dataclass
class ScorecardMetrics:
    learning_since: str
    decisions_reviewed: int
    manager_overrides: int
    manager_correct: int
    system_correct: int
    inconclusive: int
    patterns_learned: int
    override_rate: float
    manager_win_rate: float
    system_win_rate: float


@dataclass
class PatternSummary:
    subject_name: str
    subject_type: str
    override_count: int
    manager_correct_count: int
    system_correct_count: int
    avg_recommendation: float
    avg_actual: float
    common_reason: str
    last_updated: str


@dataclass
class WeeklyLearning:
    week_start: str
    week_end: str
    subject_name: str
    recommended: float
    actual: float
    manager_adjusted: float
    unit: str
    reason: str
    outcome: str
    value_saved_or_lost: float
    feedback: str


OVERRIDE_REASONS = [
    ("event_cancellation", "Event/catering canceled"),
    ("traffic_change", "Expected traffic change"),
    ("stock_not_recorded", "Existing stock not recorded"),
    ("vendor_issue", "Vendor issue"),
    ("weather", "Weather"),
    ("promotion_changed", "Promotion changed"),
    ("quality_concern", "Quality concern"),
    ("manager_judgment", "Manager judgment"),
    ("other", "Other"),
]

REASON_DB_VALUE = {
    "event_cancellation": "event_cancellation",
    "traffic_change": "traffic_change",
    "stock_not_recorded": "stock_not_recorded",
    "vendor_issue": "vendor_issue",
    "weather": "weather",
    "promotion_changed": "promotion_changed",
    "quality_concern": "quality_concern",
    "manager_judgment": "manager_judgment",
    "other": "other",
}


def _dec(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _safe(row: Any, key: str, default: Any = "") -> Any:
    """Safely extract a value from a sqlite3.Row or dict."""
    try:
        if row is None:
            return default
        # sqlite3.Row supports __getitem__ but not reliable __contains__
        if hasattr(row, "keys") and key in list(row.keys()):
            return row[key]
        if isinstance(row, dict) and key in row:
            return row[key]
        return default
    except Exception:
        return default


def scorecard(service: MarginMemoryService, location_id: str | None = None) -> ScorecardMetrics:
    """Compute overall scorecard metrics for a location (or all locations)."""
    conn = service.workspace.connect()
    try:
        total_query = "SELECT COUNT(*) as c FROM margin_memory_decisions"
        params: tuple[Any, ...] = ()
        if location_id:
            total_query += " WHERE location_id=?"
            params = (location_id,)
        total = conn.execute(total_query, params).fetchone()["c"]
        first_decision = conn.execute(
            f"{total_query} ORDER BY decision_time ASC LIMIT 1", params
        ).fetchone()
        learning_since = (
            _safe(first_decision, "decision_time", "")[:10]
            if first_decision
            else date.today().isoformat()
        )
        overrides_query = "SELECT COUNT(*) as c FROM margin_memory_decisions WHERE override_amount IS NOT NULL AND override_amount != ''"
        if location_id:
            overrides_query += " AND location_id=?"
        overrides = conn.execute(overrides_query, params).fetchone()["c"]

        # Count outcomes by grade
        outcome_query = f"""
            SELECT o.outcome_grade, COUNT(*) as cnt
              FROM margin_memory_decisions d
              JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
        """
        if location_id:
            outcome_query += " WHERE d.location_id=?"
        outcome_query += " GROUP BY o.outcome_grade"
        rows = conn.execute(outcome_query, params).fetchall()
        grade_counts: dict[str, int] = {str(r["outcome_grade"] or ""): r["cnt"] for r in rows}
        manager_correct = sum(
            cnt for grade, cnt in grade_counts.items() if "Beneficial Override" in grade
        )
        system_correct = sum(
            cnt for grade, cnt in grade_counts.items() if "System Correct" in grade
        )
        inconclusive = total - manager_correct - system_correct
        if inconclusive < 0:
            inconclusive = 0

        # Count patterns as distinct reason codes from beneficial overrides
        pattern_params: tuple[Any, ...] = ()
        pattern_query = """
            SELECT COUNT(DISTINCT d.reason_code) as cnt
              FROM margin_memory_decisions d
              JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
             WHERE o.outcome_grade='Beneficial Override'
        """
        if location_id:
            pattern_query += " AND d.location_id=?"
            pattern_params = (location_id,)
        patterns_learned = conn.execute(pattern_query, pattern_params).fetchone()["cnt"]
        if patterns_learned == 0:
            # Fallback: count total beneficial overrides as patterns
            patterns_learned = manager_correct
        override_rate = (overrides / total * 100) if total else 0.0
        manager_win_rate = (manager_correct / overrides * 100) if overrides else 0.0
        system_win_rate = (system_correct / (total - overrides) * 100) if (total - overrides) else 0.0

        return ScorecardMetrics(
            learning_since=learning_since,
            decisions_reviewed=total,
            manager_overrides=overrides,
            manager_correct=manager_correct,
            system_correct=system_correct,
            inconclusive=inconclusive,
            patterns_learned=patterns_learned,
            override_rate=round(override_rate, 1),
            manager_win_rate=round(manager_win_rate, 1),
            system_win_rate=round(system_win_rate, 1),
        )
    finally:
        conn.close()


def weekly_highlights(
    service: MarginMemoryService,
    location_id: str | None = None,
    weeks: int = 4,
) -> list[WeeklyLearning]:
    """Return recent weekly learning highlights."""
    conn = service.workspace.connect()
    try:
        end_date = date.today()
        start_date = end_date - timedelta(weeks=weeks)
        query = """
            SELECT d.decision_id, d.subject_name, d.subject_type, d.decision_time,
                   d.override_amount, d.override_percent, d.reason_code, d.manager_note,
                   d.decision_maker, d.location_id,
                   o.outcome_grade, o.estimated_margin_effect, o.explanation_json
              FROM margin_memory_decisions d
              JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
             WHERE d.decision_time >= ?
        """
        params: tuple[Any, ...] = (start_date.isoformat(),)
        if location_id:
            query += " AND d.location_id=?"
            params = (start_date.isoformat(), location_id)
        query += " ORDER BY d.decision_time DESC LIMIT 50"
        rows = conn.execute(query, params).fetchall()
        results: list[WeeklyLearning] = []
        for row in rows:
            explanation = _safe(row, "explanation_json", {})
            if isinstance(explanation, str):
                try:
                    explanation = json.loads(explanation)
                except Exception:
                    explanation = {}
            recommended = _dec(explanation.get("recommended_qty", _safe(row, "override_amount", 0)))
            actual = _dec(explanation.get("actual_usage", 0))
            adjusted = _dec(_safe(row, "override_amount", recommended))
            grade = str(_safe(row, "outcome_grade", ""))
            if "Beneficial Override" in grade:
                feedback = (
                    f"Good call. {_safe(row, 'decision_maker', 'Manager')}'s adjustment prevented "
                    f"approximately ${abs(_dec(_safe(row, 'estimated_margin_effect', 0))):,.2f} in excess inventory. "
                    "Margin Memory will consider this pattern in future recommendations."
                )
                outcome = "Manager Correct"
                value = abs(_dec(_safe(row, "estimated_margin_effect", 0)))
            elif "System Correct" in grade:
                feedback = (
                    "Margin Memory's recommendation was closer to actual usage. "
                    "This pattern has been logged for future reference."
                )
                outcome = "System Correct"
                value = 0.0
            else:
                feedback = "Outcome inconclusive. More data needed."
                outcome = "Inconclusive"
                value = 0.0
            results.append(WeeklyLearning(
                week_start=start_date.isoformat(),
                week_end=end_date.isoformat(),
                subject_name=str(_safe(row, "subject_name", "")),
                recommended=recommended,
                actual=actual,
                manager_adjusted=adjusted,
                unit="units",
                reason=str(_safe(row, "reason_code", "manager_judgment")),
                outcome=outcome,
                value_saved_or_lost=value,
                feedback=feedback,
            ))
        return results[:weeks * 5]
    finally:
        conn.close()


def pattern_summaries(
    service: MarginMemoryService,
    location_id: str | None = None,
    limit: int = 20,
) -> list[PatternSummary]:
    """Return per-subject pattern summaries."""
    conn = service.workspace.connect()
    try:
        query = """
            SELECT d.subject_name, d.subject_type, COUNT(*) as override_count,
                   SUM(CASE WHEN o.outcome_grade LIKE '%Beneficial Override%' THEN 1 ELSE 0 END) as manager_correct,
                   SUM(CASE WHEN o.outcome_grade LIKE '%System Correct%' THEN 1 ELSE 0 END) as system_correct,
                   AVG(CAST(d.override_amount AS REAL)) as avg_override,
                   AVG(CAST(json_extract(o.explanation_json, '$.actual_usage') AS REAL)) as avg_actual,
                   d.decision_time
              FROM margin_memory_decisions d
              JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
             WHERE d.override_amount IS NOT NULL AND d.override_amount != ''
        """
        params: tuple[Any, ...] = ()
        if location_id:
            query += " AND d.location_id=?"
            params = (location_id,)
        query += """
             GROUP BY d.subject_name, d.subject_type
             ORDER BY override_count DESC
             LIMIT ?
        """
        rows = conn.execute(query, params + (limit,)).fetchall()
        results: list[PatternSummary] = []
        for row in rows:
            results.append(PatternSummary(
                subject_name=str(_safe(row, "subject_name", "")),
                subject_type=str(_safe(row, "subject_type", "")),
                override_count=int(_safe(row, "override_count", 0)),
                manager_correct_count=int(_safe(row, "manager_correct", 0)),
                system_correct_count=int(_safe(row, "system_correct", 0)),
                avg_recommendation=float(_safe(row, "avg_override", 0)),
                avg_actual=float(_safe(row, "avg_actual", 0)),
                common_reason="manager_judgment",
                last_updated=str(_safe(row, "decision_time", ""))[:10],
            ))
        return results
    finally:
        conn.close()


def submit_override_feedback(
    service: MarginMemoryService,
    decision_id: str,
    reason_code: str,
    manager_note: str = "",
) -> None:
    """Record why a manager overrode the system recommendation."""
    conn = service.workspace.connect()
    try:
        conn.execute(
            """
            UPDATE margin_memory_decisions
               SET reason_code=?, manager_note=?, updated_at=?
             WHERE decision_id=?
            """,
            (reason_code, manager_note, now_iso(), decision_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_override_reasons() -> list[tuple[str, str]]:
    """Return the list of (reason_code, display_label) for quick-fill buttons."""
    return list(OVERRIDE_REASONS)
