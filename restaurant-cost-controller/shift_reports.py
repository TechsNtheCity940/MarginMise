#!/usr/bin/env python3
"""Shift report detection, extraction, and lightweight logging.

Shift reports are identified by filename hints or by spreadsheet columns
such as `Shift`, `Labor Cost`, `Guests`, `Sales`, `Surcharge`.
The program extracts only the high-level summary and logs which source file
it came from, so CostPilot can reference it. Raw shift data is not stored.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from bulk_ingestion import read_document, normalize_header
from invoice_pipeline import now_iso


SHIFT_KEYWORDS = re.compile(
    r"shift[\s_\-]*report|shift[\s_\-]*log|shift[\s_\-]*summary|daily[\s_\-]*shift|shift[\s_\-]*review|"
    r"manager[\s_\-]*shift|server[\s_\-]*report|cashier[\s_\-]*report",
    re.IGNORECASE,
)

SHIFT_HEADER_PATTERNS = [
    {"shift", "labor cost", "guests", "net sales", "surcharge"},
    {"shift", "labor cost", "guests"},
    {"shift", "manager", "sales", "guests"},
    {"shift", "labor cost", "sales"},
]


@dataclass
class ShiftReportSummary:
    source_path: str
    source_name: str
    report_date: str | None
    shift: str | None
    labor_cost: float | None = None
    guests: int | None = None
    net_sales: float | None = None
    surcharge: float | None = None
    notes: str | None = None
    extracted_at: str = field(default_factory=now_iso)


def _looks_like_shift_report(path: Path, rows: Sequence[dict] | None = None) -> bool:
    """Return True when the file path or headers suggest a shift report."""
    filename = path.stem.lower()
    if SHIFT_KEYWORDS.search(filename):
        return True
    if rows:
        headers = {normalize_header(k) for k in rows[0].keys()}
        for pattern in SHIFT_HEADER_PATTERNS:
            if pattern <= headers:
                return True
    return False


def extract_shift_report(path: Path) -> ShiftReportSummary | None:
    """Read a shift-report file and return a lightweight summary.

    Returns None if the file does not look like a shift report.
    """
    if not _looks_like_shift_report(path):
        # Try reading the file to check headers if filename didn't match
        try:
            rows = read_document(path)
            if rows and _looks_like_shift_report(path, rows):
                pass  # headers match, continue below
            else:
                return None
        except Exception:
            return None

    source_name = path.name
    summary = ShiftReportSummary(
        source_path=str(path),
        source_name=source_name,
        report_date=None,
        shift=None,
    )

    try:
        rows = read_document(path)
    except Exception:
        return summary

    if not rows:
        return summary

    headers = list(rows[0].keys())

    def first_col(*candidates: str) -> Any:
        for c in candidates:
            for h in headers:
                if normalize_header(h) == normalize_header(c):
                    return rows[0].get(h) or rows[-1].get(h)
        return None

    # Date / shift detection
    summary.report_date = str(first_col("Date", "Business Date", "Shift Date") or "")
    summary.shift = str(first_col("Shift", "Shift Name", "Period") or "")

    # Numeric fields
    labor = first_col("Labor Cost", "Total Labor", "Labor")
    guests = first_col("Guests", "Covers", "Guest Count")
    sales = first_col("Net Sales", "Sales", "Total Sales", "Gross Sales")
    surcharge = first_col("Surcharge", "Service Charge", "Auto Gratuity")

    try:
        summary.labor_cost = float(str(labor).replace("$", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        summary.labor_cost = None

    try:
        summary.guests = int(float(str(guests).strip() or 0))
    except (TypeError, ValueError):
        summary.guests = None

    try:
        summary.net_sales = float(str(sales).replace("$", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        summary.net_sales = None

    try:
        summary.surcharge = float(str(surcharge).replace("$", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        summary.surcharge = None

    # Build a concise note
    parts = []
    if summary.shift:
        parts.append(f"Shift={summary.shift}")
    if summary.labor_cost is not None:
        parts.append(f"Labor=${summary.labor_cost:,.2f}")
    if summary.guests is not None:
        parts.append(f"Guests={summary.guests}")
    if summary.net_sales is not None:
        parts.append(f"Sales=${summary.net_sales:,.2f}")
    if summary.surcharge is not None:
        parts.append(f"Surcharge=${summary.surcharge:,.2f}")
    summary.notes = "; ".join(parts) if parts else "Shift report with no extractable summary fields"

    return summary


def log_shift_report(conn: sqlite3.Connection, summary: ShiftReportSummary) -> None:
    """Log a shift-report summary to the lightweight reference table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shift_report_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            source_name TEXT NOT NULL,
            report_date TEXT,
            shift TEXT,
            labor_cost REAL,
            guests INTEGER,
            net_sales REAL,
            surcharge REAL,
            notes TEXT,
            extracted_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO shift_report_logs
            (source_path, source_name, report_date, shift, labor_cost, guests, net_sales, surcharge, notes, extracted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            summary.source_path,
            summary.source_name,
            summary.report_date,
            summary.shift,
            summary.labor_cost,
            summary.guests,
            summary.net_sales,
            summary.surcharge,
            summary.notes,
            summary.extracted_at,
        ),
    )


def get_shift_report_logs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Return recent shift-report log entries for CostPilot reference."""
    rows = conn.execute(
        """
        SELECT log_id, source_path, source_name, report_date, shift,
               labor_cost, guests, net_sales, surcharge, notes, extracted_at
          FROM shift_report_logs
         ORDER BY extracted_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
