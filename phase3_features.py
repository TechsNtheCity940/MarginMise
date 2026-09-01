#!/usr/bin/env python3
"""Phase 3 intelligence for Restaurant Cost Controller v3.0.

Adds portfolio reporting, inventory transfers, event/weather-aware forecasting,
forecast feedback, distributor exchange files, quantified value estimates,
advanced recipe variance, menu profitability, and sales-driven ordering.

The implementation is local-first. Weather uses Open-Meteo through urllib when
coordinates are configured. Events may be entered manually or imported from an
RFC 5545 iCalendar file. Distributor integrations are profile-driven exchange
files until a real distributor connector has been certified with a pilot.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import shutil
import sqlite3
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from inventory_planning import preferred_sales_rows

MONEY = Decimal("0.01")
QTY = Decimal("0.0001")


class _ManagedExternalConnection(sqlite3.Connection):
    """Close portfolio/transfer database handles when their context exits."""

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()

PHASE3_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS location_profile (
    location_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL DEFAULT 'My Restaurant Group',
    location_name TEXT NOT NULL,
    address TEXT,
    latitude REAL,
    longitude REAL,
    timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    adjustment_date TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    quantity_delta TEXT NOT NULL,
    adjustment_type TEXT NOT NULL,
    source_location_id TEXT,
    destination_location_id TEXT,
    reference_id TEXT,
    notes TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adjustments_item_date ON inventory_adjustments(item_id, adjustment_date);

CREATE TABLE IF NOT EXISTS inventory_transfers (
    transfer_id TEXT PRIMARY KEY,
    source_location_id TEXT NOT NULL,
    source_location_name TEXT NOT NULL,
    destination_location_id TEXT NOT NULL,
    destination_location_name TEXT NOT NULL,
    transfer_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',
    line_count INTEGER NOT NULL DEFAULT 0,
    estimated_value TEXT NOT NULL DEFAULT '0.00',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    shipped_at TEXT,
    received_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS inventory_transfer_lines (
    transfer_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL REFERENCES inventory_transfers(transfer_id) ON DELETE CASCADE,
    source_item_id TEXT NOT NULL,
    destination_item_id TEXT,
    vendor_name TEXT,
    vendor_sku TEXT,
    item_name TEXT NOT NULL,
    count_unit TEXT,
    quantity TEXT NOT NULL,
    unit_cost TEXT NOT NULL DEFAULT '0.00',
    line_value TEXT NOT NULL DEFAULT '0.00',
    receive_status TEXT NOT NULL DEFAULT 'Pending',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS local_events (
    event_id TEXT PRIMARY KEY,
    event_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    event_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Local Event',
    expected_sales_impact_percent TEXT NOT NULL DEFAULT '0.00',
    source TEXT NOT NULL DEFAULT 'Manual',
    external_uid TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(external_uid)
);
CREATE INDEX IF NOT EXISTS idx_local_events_dates ON local_events(event_date, end_date);

CREATE TABLE IF NOT EXISTS weather_daily (
    weather_date TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    weather_code INTEGER,
    temperature_max_f REAL,
    temperature_min_f REAL,
    precipitation_inches REAL,
    precipitation_probability REAL,
    wind_mph REAL,
    source TEXT NOT NULL DEFAULT 'Open-Meteo',
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS demand_forecasts (
    forecast_id TEXT PRIMARY KEY,
    forecast_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    baseline_sales TEXT NOT NULL,
    trend_multiplier TEXT NOT NULL DEFAULT '1.0000',
    weekday_multiplier TEXT NOT NULL DEFAULT '1.0000',
    weather_multiplier TEXT NOT NULL DEFAULT '1.0000',
    event_multiplier TEXT NOT NULL DEFAULT '1.0000',
    learned_multiplier TEXT NOT NULL DEFAULT '1.0000',
    predicted_net_sales TEXT NOT NULL,
    actual_net_sales TEXT,
    absolute_error TEXT,
    error_percent TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    explanation_json TEXT NOT NULL,
    UNIQUE(forecast_date, model_version)
);
CREATE INDEX IF NOT EXISTS idx_demand_forecasts_date ON demand_forecasts(forecast_date);

CREATE TABLE IF NOT EXISTS forecast_learning (
    factor_key TEXT PRIMARY KEY,
    sample_count INTEGER NOT NULL DEFAULT 0,
    learned_multiplier TEXT NOT NULL DEFAULT '1.0000',
    mean_absolute_percent_error TEXT NOT NULL DEFAULT '0.00',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS distributor_profiles (
    distributor_id TEXT PRIMARY KEY,
    distributor_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    vendor_name_match TEXT,
    connector_type TEXT NOT NULL DEFAULT 'Folder Exchange',
    account_number TEXT,
    outbound_folder TEXT,
    inbound_folder TEXT,
    order_format TEXT NOT NULL DEFAULT 'CSV',
    confirmation_format TEXT NOT NULL DEFAULT 'CSV',
    active INTEGER NOT NULL DEFAULT 1,
    settings_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS distributor_catalog (
    catalog_id INTEGER PRIMARY KEY AUTOINCREMENT,
    distributor_id TEXT NOT NULL REFERENCES distributor_profiles(distributor_id) ON DELETE CASCADE,
    distributor_sku TEXT NOT NULL,
    description TEXT NOT NULL,
    brand TEXT,
    pack TEXT,
    unit_price TEXT NOT NULL DEFAULT '0.00',
    effective_date TEXT,
    item_id TEXT REFERENCES items(item_id),
    raw_json TEXT,
    UNIQUE(distributor_id, distributor_sku)
);

CREATE TABLE IF NOT EXISTS distributor_exchanges (
    exchange_id TEXT PRIMARY KEY,
    distributor_id TEXT NOT NULL REFERENCES distributor_profiles(distributor_id),
    exchange_type TEXT NOT NULL,
    reference_id TEXT,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    total_amount TEXT NOT NULL DEFAULT '0.00',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS savings_events (
    savings_id TEXT PRIMARY KEY,
    event_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    estimated_value TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'Estimated',
    source_type TEXT,
    source_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_report_history (
    report_id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    report_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    location_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal(default)


def money(value: Any) -> Decimal:
    return dec(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def qty(value: Any) -> Decimal:
    return dec(value).quantize(QTY, rounding=ROUND_HALF_UP)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("._") or "export"


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip())


def parse_date(value: Any) -> str:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y%m%d", "%Y%m%dT%H%M%S", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(text[:15] if "T" in text else text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date: {value!r}")


def location_id_for(path: Path) -> str:
    return "LOC-" + hashlib.sha256(str(path.expanduser().resolve()).lower().encode()).hexdigest()[:14].upper()


class Phase3Error(RuntimeError):
    pass


@dataclass
class ForecastResult:
    forecast_id: str
    forecast_date: str
    predicted_sales: Decimal
    baseline_sales: Decimal
    explanation: dict[str, Any]


class Phase3Service:
    MODEL_VERSION = "phase3-v1"

    def __init__(self, workspace: Any, planning: Any, controls: Any, phase2: Any):
        self.workspace = workspace
        self.planning = planning
        self.controls = controls
        self.phase2 = phase2
        self._location_provider: Callable[[], list[dict[str, str]]] | None = None
        self.ensure_schema()

    def set_location_provider(self, provider: Callable[[], list[dict[str, str]]]) -> None:
        self._location_provider = provider

    @staticmethod
    def _to_month_start(value: str) -> date:
        """Parse a date or month string into the first day of that month.

        Accepts both full ISO dates ('2026-01-15') and compact month strings
        ('2026-01').
        """
        cleaned = str(value).strip()
        # Month string like '2026-01'
        if re.fullmatch(r"\d{4}-\d{2}", cleaned):
            return date.fromisoformat(cleaned + "-01")
        return date.fromisoformat(cleaned[:10]).replace(day=1)

    @staticmethod
    def _month_keys(start: str, end: str) -> list[str]:
        cursor = Phase3Service._to_month_start(str(start))
        finish = Phase3Service._to_month_start(str(end))
        months: list[str] = []
        while cursor <= finish:
            months.append(cursor.strftime("%Y-%m"))
            cursor = date(
                cursor.year + (1 if cursor.month == 12 else 0),
                1 if cursor.month == 12 else cursor.month + 1,
                1,
            )
        return months

    def _complete_month_usage(self, month: str) -> list[Any]:
        """Return persisted or preview usage only when its endpoint counts exist."""
        summary = self.planning.month_summary(month)
        status = str(summary.get("count_status") or summary.get("status") or "")
        if status == "Future period":
            return []
        if int(summary.get("missing_opening_counts") or 0):
            return []
        if int(summary.get("missing_ending_counts") or 0):
            return []
        return self.planning.list_month_usage(month)

    def ensure_schema(self) -> None:
        folders = {
            "phase3": self.workspace.root / "Phase 3 Intelligence",
            "transfers": self.workspace.root / "Inventory Transfers",
            "forecasting": self.workspace.root / "Forecasting",
            "distributors": self.workspace.root / "Distributor Exchange",
            "owner_reports": self.workspace.root / "Owner Reports",
        }
        for key, path in folders.items():
            self.workspace.folders.setdefault(key, path)
            Path(path).mkdir(parents=True, exist_ok=True)
        settings = self.workspace.load_settings()
        loc_id = location_id_for(self.workspace.root)
        with self.workspace.connect() as conn:
            conn.executescript(PHASE3_SCHEMA_SQL)
            conn.execute(
                """INSERT INTO location_profile(location_id,group_name,location_name,address,latitude,longitude,timezone,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(location_id) DO UPDATE SET location_name=excluded.location_name,
                   timezone=excluded.timezone,updated_at=excluded.updated_at""",
                (loc_id, settings.get("restaurant_group", "My Restaurant Group"), settings.get("restaurant_name", "Restaurant"),
                 settings.get("address", ""), settings.get("latitude"), settings.get("longitude"),
                 settings.get("timezone", "America/Chicago"), now_iso()),
            )

    @property
    def current_location_id(self) -> str:
        return location_id_for(self.workspace.root)

    def registered_locations(self) -> list[dict[str, Any]]:
        rows = self._location_provider() if self._location_provider else [
            {"name": self.workspace.load_settings().get("restaurant_name", "Restaurant"), "path": str(self.workspace.root)}
        ]
        output: list[dict[str, Any]] = []
        for row in rows:
            path = Path(str(row.get("path") or "")).expanduser()
            db = path / "restaurant_costs.sqlite3"
            if not db.exists():
                continue
            output.append({"location_id": location_id_for(path), "name": row.get("name") or path.name, "path": str(path.resolve()), "db_path": str(db.resolve())})
        if not any(row["location_id"] == self.current_location_id for row in output):
            output.append({"location_id": self.current_location_id, "name": self.workspace.load_settings().get("restaurant_name", "Restaurant"), "path": str(self.workspace.root), "db_path": str(self.workspace.db_path)})
        return output

    @staticmethod
    def _connect_external(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, factory=_ManagedExternalConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def portfolio_summary(self, year: int | None = None) -> dict[str, Any]:
        year = int(year or date.today().year)
        start, end = f"{year}-01-01", f"{year}-12-31"
        locations: list[dict[str, Any]] = []
        totals = defaultdict(Decimal)
        for location in self.registered_locations():
            try:
                with self._connect_external(Path(location["db_path"])) as conn:
                    sales = money(sum(
                        (dec(row["net_sales"]) for row in preferred_sales_rows(conn, start, end)),
                        Decimal("0"),
                    ))
                    purchases = money(conn.execute("SELECT COALESCE(SUM(CAST(total AS REAL)),0) FROM invoices WHERE status='Approved' AND invoice_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
                    inventory = money(conn.execute("SELECT COALESCE(SUM(CAST(estimated_on_hand AS REAL) * (CAST(current_price AS REAL)/CASE WHEN CAST(units_per_purchase_unit AS REAL)>0 THEN CAST(units_per_purchase_unit AS REAL) ELSE 1 END)),0) FROM items WHERE active=1").fetchone()[0])
                    waste = money(conn.execute("SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) FROM waste_events WHERE event_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
                    menu_sales = money(conn.execute("SELECT COALESCE(SUM(CAST(net_sales AS REAL)),0) FROM pos_sales_lines WHERE business_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
                    open_exceptions = int(conn.execute("SELECT COUNT(*) FROM operational_exceptions WHERE status IN ('Open','Acknowledged')").fetchone()[0])
                    pending_reviews = int(conn.execute("SELECT COUNT(*) FROM reviews WHERE status='Open'").fetchone()[0])
                location_row = {**location, "sales": sales, "purchases": purchases, "inventory_value": inventory, "waste_cost": waste,
                                "pos_sales": menu_sales, "open_exceptions": open_exceptions, "pending_reviews": pending_reviews,
                                "purchase_percent": (purchases / sales * 100).quantize(Decimal('0.01')) if sales else Decimal('0')}
                locations.append(location_row)
                for key in ("sales", "purchases", "inventory_value", "waste_cost", "pos_sales"):
                    totals[key] += location_row[key]
            except sqlite3.Error as exc:
                locations.append({**location, "error": str(exc), "sales": Decimal("0"), "purchases": Decimal("0"), "inventory_value": Decimal("0"), "waste_cost": Decimal("0"), "pos_sales": Decimal("0"), "open_exceptions": 0, "pending_reviews": 0, "purchase_percent": Decimal("0")})
        return {"year": year, "location_count": len(locations), "locations": locations,
                "total_sales": money(totals["sales"]), "total_purchases": money(totals["purchases"]),
                "total_inventory_value": money(totals["inventory_value"]), "total_waste_cost": money(totals["waste_cost"]),
                "total_pos_sales": money(totals["pos_sales"]),
                "portfolio_purchase_percent": (totals["purchases"] / totals["sales"] * 100).quantize(Decimal('0.01')) if totals["sales"] else Decimal("0")}

    # ------------------------------------------------------------------
    # Inventory transfers
    # ------------------------------------------------------------------
    def _match_destination_item(self, conn: sqlite3.Connection, source: sqlite3.Row) -> sqlite3.Row | None:
        if source["vendor_sku"]:
            found = conn.execute("SELECT * FROM items WHERE vendor_sku=? COLLATE NOCASE LIMIT 1", (source["vendor_sku"],)).fetchone()
            if found:
                return found
        return conn.execute("SELECT * FROM items WHERE normalized_description=? LIMIT 1", (source["normalized_description"],)).fetchone()

    def create_transfer(self, destination_path: Path, lines: Iterable[dict[str, Any]], *, transfer_date: str | None = None,
                        notes: str = "", created_by: str = "system") -> str:
        destination_path = Path(destination_path).expanduser().resolve()
        if destination_path == self.workspace.root:
            raise Phase3Error("Source and destination locations must be different.")
        destination_db = destination_path / "restaurant_costs.sqlite3"
        if not destination_db.exists():
            raise Phase3Error("Destination restaurant workspace does not contain a database.")
        transfer_date = parse_date(transfer_date or date.today().isoformat())
        destination_name = destination_path.name
        try:
            cfg = json.loads((destination_path / "restaurant_config.json").read_text(encoding="utf-8"))
            destination_name = cfg.get("restaurant_name") or destination_name
        except Exception:
            pass
        transfer_id = f"XFER-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        prepared: list[dict[str, Any]] = []
        total = Decimal("0")
        with self.workspace.connect() as source_conn, self._connect_external(destination_db) as dest_conn:
            dest_conn.executescript(PHASE3_SCHEMA_SQL)
            for raw in lines:
                item_id = str(raw.get("item_id") or "")
                amount = qty(raw.get("quantity"))
                if not item_id or amount <= 0:
                    continue
                source_item = source_conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
                if not source_item:
                    raise Phase3Error(f"Source item not found: {item_id}")
                dest_item = self._match_destination_item(dest_conn, source_item)
                if not dest_item:
                    raise Phase3Error(f"Destination has no matching item for {source_item['item_name']}. Add or map it before transferring.")
                units_per = dec(source_item["units_per_purchase_unit"], "1") or Decimal("1")
                unit_cost = money(dec(source_item["current_price"]) / units_per)
                line_value = money(amount * unit_cost)
                total += line_value
                prepared.append({"source": source_item, "destination": dest_item, "quantity": amount, "unit_cost": unit_cost, "line_value": line_value, "notes": str(raw.get("notes") or "")})
            if not prepared:
                raise Phase3Error("At least one positive transfer quantity is required.")
            source_name = self.workspace.load_settings().get("restaurant_name", self.workspace.root.name)
            source_conn.execute("""INSERT INTO inventory_transfers(transfer_id,source_location_id,source_location_name,destination_location_id,
                destination_location_name,transfer_date,status,line_count,estimated_value,created_by,created_at,shipped_at,notes)
                VALUES(?,?,?,?,?,?,'Shipped',?,?,?,?,?,?)""",
                (transfer_id, self.current_location_id, source_name, location_id_for(destination_path), destination_name, transfer_date,
                 len(prepared), f"{money(total):.2f}", created_by, now_iso(), now_iso(), notes))
            dest_conn.execute("""INSERT INTO inventory_transfers(transfer_id,source_location_id,source_location_name,destination_location_id,
                destination_location_name,transfer_date,status,line_count,estimated_value,created_by,created_at,shipped_at,notes)
                VALUES(?,?,?,?,?,?,'In Transit',?,?,?,?,?,?)""",
                (transfer_id, self.current_location_id, source_name, location_id_for(destination_path), destination_name, transfer_date,
                 len(prepared), f"{money(total):.2f}", created_by, now_iso(), now_iso(), notes))
            for row in prepared:
                s, d = row["source"], row["destination"]
                values = (transfer_id, s["item_id"], d["item_id"], s["vendor_name"], s["vendor_sku"] or "", s["item_name"],
                          s["count_unit"] or s["unit"] or "each", f"{row['quantity']:.4f}", f"{row['unit_cost']:.2f}",
                          f"{row['line_value']:.2f}", "Pending", row["notes"])
                source_conn.execute("""INSERT INTO inventory_transfer_lines(transfer_id,source_item_id,destination_item_id,vendor_name,vendor_sku,
                    item_name,count_unit,quantity,unit_cost,line_value,receive_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                dest_conn.execute("""INSERT INTO inventory_transfer_lines(transfer_id,source_item_id,destination_item_id,vendor_name,vendor_sku,
                    item_name,count_unit,quantity,unit_cost,line_value,receive_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                source_conn.execute("""INSERT INTO inventory_adjustments(adjustment_id,adjustment_date,item_id,quantity_delta,adjustment_type,
                    source_location_id,destination_location_id,reference_id,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"ADJ-{uuid.uuid4().hex[:14].upper()}", transfer_date, s["item_id"], f"{-row['quantity']:.4f}", "Transfer Out",
                     self.current_location_id, location_id_for(destination_path), transfer_id, notes, created_by, now_iso()))
        self.controls.audit("transfer.ship", "inventory_transfer", transfer_id, f"Shipped {len(prepared)} inventory transfer lines to {destination_name}")
        return transfer_id

    def receive_transfer(self, transfer_id: str, *, received_by: str = "system", notes: str = "") -> None:
        with self.workspace.connect() as conn:
            transfer = conn.execute("SELECT * FROM inventory_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
            if not transfer:
                raise Phase3Error("Transfer not found in this location.")
            if transfer["destination_location_id"] != self.current_location_id:
                raise Phase3Error("Only the destination location can receive this transfer.")
            if transfer["status"] == "Received":
                return
            rows = conn.execute("SELECT * FROM inventory_transfer_lines WHERE transfer_id=?", (transfer_id,)).fetchall()
            for row in rows:
                if not row["destination_item_id"]:
                    raise Phase3Error(f"Transfer line {row['item_name']} is not mapped to a destination item.")
                conn.execute("""INSERT INTO inventory_adjustments(adjustment_id,adjustment_date,item_id,quantity_delta,adjustment_type,
                    source_location_id,destination_location_id,reference_id,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"ADJ-{uuid.uuid4().hex[:14].upper()}", date.today().isoformat(), row["destination_item_id"], row["quantity"], "Transfer In",
                     transfer["source_location_id"], transfer["destination_location_id"], transfer_id, notes, received_by, now_iso()))
                conn.execute("UPDATE inventory_transfer_lines SET receive_status='Received' WHERE transfer_line_id=?", (row["transfer_line_id"],))
            conn.execute("UPDATE inventory_transfers SET status='Received',received_at=?,notes=TRIM(COALESCE(notes,'') || ' ' || ?) WHERE transfer_id=?", (now_iso(), notes, transfer_id))
        self.controls.audit("transfer.receive", "inventory_transfer", transfer_id, "Received inventory transfer")

    def list_transfers(self, limit: int = 300) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute("SELECT * FROM inventory_transfers ORDER BY transfer_date DESC,created_at DESC LIMIT ?", (limit,)).fetchall()

    def adjustment_total(self, item_id: str, start: str | None = None, end: str | None = None) -> Decimal:
        query = "SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) FROM inventory_adjustments WHERE item_id=?"
        params: list[Any] = [item_id]
        if start:
            query += " AND adjustment_date>=?"; params.append(start)
        if end:
            query += " AND adjustment_date<=?"; params.append(end)
        with self.workspace.connect() as conn:
            return qty(conn.execute(query, tuple(params)).fetchone()[0])

    # ------------------------------------------------------------------
    # Events and weather
    # ------------------------------------------------------------------
    def add_event(self, event_name: str, event_date: str, *, end_date: str | None = None, category: str = "Local Event",
                  impact_percent: Any = 0, notes: str = "", source: str = "Manual", external_uid: str | None = None) -> str:
        start = parse_date(event_date); end = parse_date(end_date or start)
        if end < start:
            raise Phase3Error("Event end date cannot be before its start date.")
        event_name = str(event_name or "").strip()
        if not event_name:
            raise Phase3Error("Event name is required.")
        event_id = f"EVT-{uuid.uuid4().hex[:12].upper()}"
        stamp = now_iso()
        with self.workspace.connect() as conn:
            if external_uid:
                existing = conn.execute("SELECT event_id FROM local_events WHERE external_uid=?", (external_uid,)).fetchone()
                if existing:
                    event_id = existing["event_id"]
            conn.execute("""INSERT INTO local_events(event_id,event_date,end_date,event_name,category,expected_sales_impact_percent,source,
                external_uid,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                event_date=excluded.event_date,end_date=excluded.end_date,event_name=excluded.event_name,category=excluded.category,
                expected_sales_impact_percent=excluded.expected_sales_impact_percent,source=excluded.source,notes=excluded.notes,updated_at=excluded.updated_at""",
                (event_id, start, end, event_name, category, f"{dec(impact_percent):.2f}", source, external_uid, notes, stamp, stamp))
        return event_id

    def import_ics(self, path: Path, default_impact_percent: Any = 10) -> dict[str, Any]:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        # RFC 5545 folded lines continue with a leading space or tab.
        unfolded = re.sub(r"\r?\n[ \t]", "", text)
        imported, skipped, errors = 0, 0, []
        for index, block in enumerate(re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, flags=re.S | re.I), 1):
            fields: dict[str, str] = {}
            for raw in block.splitlines():
                if ":" not in raw:
                    continue
                key, value = raw.split(":", 1)
                fields[key.split(";", 1)[0].upper()] = value.replace("\\,", ",").replace("\\n", " ").strip()
            try:
                start = parse_date(fields.get("DTSTART"))
                raw_end = fields.get("DTEND")
                end = parse_date(raw_end) if raw_end else start
                # All-day DTEND is exclusive in iCalendar.
                if raw_end and re.fullmatch(r"\d{8}", raw_end) and end > start:
                    end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
                self.add_event(fields.get("SUMMARY") or "Imported Event", start, end_date=end,
                               impact_percent=default_impact_percent, notes=fields.get("DESCRIPTION", ""),
                               source="iCalendar", external_uid=fields.get("UID") or f"{Path(path).name}-{index}")
                imported += 1
            except Exception as exc:
                skipped += 1; errors.append(f"Event {index}: {exc}")
        return {"imported": imported, "skipped": skipped, "errors": errors}

    def list_events(self, start: str | None = None, end: str | None = None, limit: int = 300) -> list[sqlite3.Row]:
        start = start or date.today().isoformat(); end = end or (date.today() + timedelta(days=365)).isoformat()
        with self.workspace.connect() as conn:
            return conn.execute("SELECT * FROM local_events WHERE end_date>=? AND event_date<=? ORDER BY event_date LIMIT ?", (start, end, limit)).fetchall()

    def _coordinates(self) -> tuple[float, float]:
        settings = self.workspace.load_settings()
        try:
            lat, lon = float(settings.get("latitude")), float(settings.get("longitude"))
        except (TypeError, ValueError):
            raise Phase3Error("Set the restaurant latitude and longitude in Settings before fetching weather.")
        return lat, lon

    def refresh_weather(self, forecast_days: int = 16, *, opener: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
        lat, lon = self._coordinates()
        params = {
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
            "timezone": "auto", "forecast_days": max(1, min(16, int(forecast_days))),
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
        request = Request(url, headers={"User-Agent": "RestaurantCostController/3.0"})
        opener = opener or urlopen
        with opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        output = []
        with self.workspace.connect() as conn:
            for i, day in enumerate(dates):
                row = {
                    "weather_date": day,
                    "weather_code": (daily.get("weather_code") or [None] * len(dates))[i],
                    "temperature_max_f": (daily.get("temperature_2m_max") or [None] * len(dates))[i],
                    "temperature_min_f": (daily.get("temperature_2m_min") or [None] * len(dates))[i],
                    "precipitation_inches": (daily.get("precipitation_sum") or [None] * len(dates))[i],
                    "precipitation_probability": (daily.get("precipitation_probability_max") or [None] * len(dates))[i],
                    "wind_mph": (daily.get("wind_speed_10m_max") or [None] * len(dates))[i],
                }
                conn.execute("""INSERT INTO weather_daily(weather_date,fetched_at,latitude,longitude,weather_code,temperature_max_f,
                    temperature_min_f,precipitation_inches,precipitation_probability,wind_mph,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(weather_date) DO UPDATE SET fetched_at=excluded.fetched_at,weather_code=excluded.weather_code,
                    temperature_max_f=excluded.temperature_max_f,temperature_min_f=excluded.temperature_min_f,
                    precipitation_inches=excluded.precipitation_inches,precipitation_probability=excluded.precipitation_probability,
                    wind_mph=excluded.wind_mph,raw_json=excluded.raw_json""",
                    (day, now_iso(), lat, lon, row["weather_code"], row["temperature_max_f"], row["temperature_min_f"],
                     row["precipitation_inches"], row["precipitation_probability"], row["wind_mph"], json.dumps(row)))
                output.append(row)
        return output

    def refresh_weather_history(self, days: int = 180, *, opener: Callable[..., Any] | None = None) -> int:
        """Backfill observed weather so MarginMemory can measure weather effects on sales."""
        lat, lon = self._coordinates()
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=max(30, min(730, int(days))) - 1)
        params = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
                  "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
                  "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch", "timezone": "auto"}
        url = "https://archive-api.open-meteo.com/v1/archive?" + urlencode(params)
        request = Request(url, headers={"User-Agent": "RestaurantCostController/3.0"})
        opener = opener or urlopen
        with opener(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        daily = payload.get("daily") or {}; dates = daily.get("time") or []
        with self.workspace.connect() as conn:
            for i, day in enumerate(dates):
                values = [daily.get(key) or [None] * len(dates) for key in ("weather_code","temperature_2m_max","temperature_2m_min","precipitation_sum","precipitation_probability_max","wind_speed_10m_max")]
                conn.execute("""INSERT INTO weather_daily(weather_date,fetched_at,latitude,longitude,weather_code,temperature_max_f,
                    temperature_min_f,precipitation_inches,precipitation_probability,wind_mph,source,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(weather_date) DO UPDATE SET weather_code=excluded.weather_code,temperature_max_f=excluded.temperature_max_f,
                    temperature_min_f=excluded.temperature_min_f,precipitation_inches=excluded.precipitation_inches,precipitation_probability=excluded.precipitation_probability,wind_mph=excluded.wind_mph,source=excluded.source,raw_json=excluded.raw_json""",
                    (day, now_iso(), lat, lon, values[0][i], values[1][i], values[2][i], values[3][i], values[4][i], values[5][i], "Open-Meteo Archive", json.dumps({"date": day, "historical": True})))
        return len(dates)

    def list_weather(self, start: str | None = None, end: str | None = None) -> list[sqlite3.Row]:
        start = start or date.today().isoformat(); end = end or (date.today() + timedelta(days=16)).isoformat()
        with self.workspace.connect() as conn:
            return conn.execute("SELECT * FROM weather_daily WHERE weather_date BETWEEN ? AND ? ORDER BY weather_date", (start, end)).fetchall()

    # ------------------------------------------------------------------
    # Forecasting and learning
    # ------------------------------------------------------------------
    def _daily_sales(self, start: date, end: date) -> dict[date, Decimal]:
        output: dict[date, Decimal] = defaultdict(Decimal)
        with self.workspace.connect() as conn:
            pos = conn.execute("SELECT business_date,SUM(CAST(net_sales AS REAL)) total FROM pos_sales_lines WHERE business_date BETWEEN ? AND ? GROUP BY business_date", (start.isoformat(), end.isoformat())).fetchall()
            for row in pos:
                output[date.fromisoformat(row["business_date"])] = money(row["total"])
            rows = preferred_sales_rows(conn, start, end)
            for row in rows:
                a, b = date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])
                days = max(1, (b - a).days + 1); daily = money(dec(row["net_sales"]) / Decimal(days))
                cursor = max(a, start)
                while cursor <= min(b, end):
                    # Item-level POS is the best source for an observed day.
                    # Summary rows fill only dates not covered by POS.
                    if cursor not in output:
                        output[cursor] += daily
                    cursor += timedelta(days=1)
        return output

    def _learned_multiplier(self, key: str) -> Decimal:
        with self.workspace.connect() as conn:
            row = conn.execute("SELECT learned_multiplier FROM forecast_learning WHERE factor_key=?", (key,)).fetchone()
        return dec(row["learned_multiplier"], "1") if row else Decimal("1")

    def forecast_sales(self, forecast_date: str | date) -> ForecastResult:
        target = date.fromisoformat(forecast_date) if isinstance(forecast_date, str) else forecast_date
        history_start = target - timedelta(days=84)
        daily = self._daily_sales(history_start, target - timedelta(days=1))
        values = list(daily.values())
        overall = Decimal(str(statistics.mean(float(v) for v in values))) if values else Decimal("0")
        same_weekday = [value for day, value in daily.items() if day.weekday() == target.weekday()]
        weekday_base = Decimal(str(statistics.mean(float(v) for v in same_weekday))) if same_weekday else overall
        baseline = money(weekday_base or overall)
        recent = [v for d, v in daily.items() if d >= target - timedelta(days=14)]
        previous = [v for d, v in daily.items() if target - timedelta(days=28) <= d < target - timedelta(days=14)]
        recent_avg = Decimal(str(statistics.mean(float(v) for v in recent))) if recent else baseline
        previous_avg = Decimal(str(statistics.mean(float(v) for v in previous))) if previous else recent_avg
        trend = max(Decimal("0.75"), min(Decimal("1.30"), recent_avg / previous_avg if previous_avg else Decimal("1")))
        weekday_mult = max(Decimal("0.70"), min(Decimal("1.40"), weekday_base / overall if overall else Decimal("1")))
        with self.workspace.connect() as conn:
            weather = conn.execute("SELECT * FROM weather_daily WHERE weather_date=?", (target.isoformat(),)).fetchone()
            events = conn.execute("SELECT * FROM local_events WHERE event_date<=? AND end_date>=?", (target.isoformat(), target.isoformat())).fetchall()
        weather_mult = Decimal("1")
        weather_notes: list[str] = []
        if weather:
            high, rain, wind = dec(weather["temperature_max_f"]), dec(weather["precipitation_inches"]), dec(weather["wind_mph"])
            if rain >= Decimal("0.50") or wind >= Decimal("35"):
                weather_mult *= Decimal("0.90"); weather_notes.append("severe rain/wind reduction")
            elif rain >= Decimal("0.15"):
                weather_mult *= Decimal("0.96"); weather_notes.append("rain reduction")
            if high >= Decimal("100"):
                weather_mult *= Decimal("0.96"); weather_notes.append("extreme heat reduction")
            elif high <= Decimal("35"):
                weather_mult *= Decimal("0.95"); weather_notes.append("cold-weather reduction")
        learned_key = f"weekday:{target.weekday()}"
        learned = self._learned_multiplier(learned_key)
        learned_weather = Decimal("1")
        if weather:
            high, rain, wind = dec(weather["temperature_max_f"]), dec(weather["precipitation_inches"]), dec(weather["wind_mph"])
            if rain >= Decimal("0.50") or wind >= Decimal("35"):
                learned_weather *= self._learned_multiplier("weather:severe")
            elif rain >= Decimal("0.15"):
                learned_weather *= self._learned_multiplier("weather:rain")
            elif high >= Decimal("95"):
                learned_weather *= self._learned_multiplier("weather:hot")
            elif high <= Decimal("40"):
                learned_weather *= self._learned_multiplier("weather:cold")
        event_mult = Decimal("1")
        event_notes = []
        learned_event = Decimal("1")
        for event in events:
            event_mult *= Decimal("1") + dec(event["expected_sales_impact_percent"]) / Decimal("100")
            category_key = "event:" + re.sub(r"[^a-z0-9]+", "_", str(event["category"] or "local").lower()).strip("_")
            name = str(event["event_name"]).lower()
            factor_key = "holiday" if any(x in name for x in ("holiday", "christmas", "thanksgiving", "new year", "independence", "memorial", "labor day", "easter", "valentine")) else category_key
            learned_event *= self._learned_multiplier(factor_key)
            event_notes.append(f"{event['event_name']} ({event['expected_sales_impact_percent']}%)")
        predicted = money(baseline * trend * weekday_mult * weather_mult * learned_weather * event_mult * learned_event * learned)
        explanation = {"history_days": len(daily), "baseline": str(baseline), "trend_multiplier": str(trend),
                       "weekday_multiplier": str(weekday_mult), "weather_multiplier": str(weather_mult),
                       "event_multiplier": str(event_mult), "learned_multiplier": str(learned),
                       "weather_notes": weather_notes, "events": event_notes}
        forecast_id = f"FCST-{target.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        with self.workspace.connect() as conn:
            existing = conn.execute("SELECT forecast_id FROM demand_forecasts WHERE forecast_date=? AND model_version=?", (target.isoformat(), self.MODEL_VERSION)).fetchone()
            if existing:
                forecast_id = existing["forecast_id"]
            conn.execute("""INSERT INTO demand_forecasts(forecast_id,forecast_date,created_at,model_version,baseline_sales,
                trend_multiplier,weekday_multiplier,weather_multiplier,event_multiplier,learned_multiplier,predicted_net_sales,
                status,explanation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,'Open',?) ON CONFLICT(forecast_date,model_version)
                DO UPDATE SET created_at=excluded.created_at,baseline_sales=excluded.baseline_sales,trend_multiplier=excluded.trend_multiplier,
                weekday_multiplier=excluded.weekday_multiplier,weather_multiplier=excluded.weather_multiplier,event_multiplier=excluded.event_multiplier,
                learned_multiplier=excluded.learned_multiplier,predicted_net_sales=excluded.predicted_net_sales,status='Open',explanation_json=excluded.explanation_json""",
                (forecast_id, target.isoformat(), now_iso(), self.MODEL_VERSION, f"{baseline:.2f}", f"{trend:.4f}", f"{weekday_mult:.4f}",
                 f"{weather_mult:.4f}", f"{event_mult:.4f}", f"{learned:.4f}", f"{predicted:.2f}", json.dumps(explanation)))
        return ForecastResult(forecast_id, target.isoformat(), predicted, baseline, explanation)

    def generate_forecast_range(self, start: str | date | None = None, days: int = 14) -> list[ForecastResult]:
        cursor = date.fromisoformat(start) if isinstance(start, str) else start or date.today()
        return [self.forecast_sales(cursor + timedelta(days=i)) for i in range(max(1, min(31, int(days))))]

    def learn_from_actuals(self) -> dict[str, Any]:
        today = date.today()
        daily = self._daily_sales(today - timedelta(days=180), today)
        updated = 0; errors: list[Decimal] = []
        with self.workspace.connect() as conn:
            rows = conn.execute("SELECT * FROM demand_forecasts WHERE forecast_date<? ORDER BY forecast_date", (today.isoformat(),)).fetchall()
            groups: dict[str, list[Decimal]] = defaultdict(list)
            for row in rows:
                target = date.fromisoformat(row["forecast_date"]); actual = daily.get(target)
                predicted = money(row["predicted_net_sales"])
                if actual is None or predicted <= 0:
                    continue
                ratio = max(Decimal("0.60"), min(Decimal("1.50"), actual / predicted))
                error = abs(actual - predicted); error_pct = (error / actual * 100) if actual else Decimal("0")
                groups[f"weekday:{target.weekday()}"].append(ratio); errors.append(error_pct)
                conn.execute("UPDATE demand_forecasts SET actual_net_sales=?,absolute_error=?,error_percent=?,status='Scored' WHERE forecast_id=?",
                             (f"{actual:.2f}", f"{money(error):.2f}", f"{error_pct.quantize(Decimal('0.01')):.2f}", row["forecast_id"]))
                updated += 1
            for key, ratios in groups.items():
                learned = Decimal(str(statistics.mean(float(v) for v in ratios[-12:])))
                mape = Decimal(str(statistics.mean(float(v) for v in errors[-50:]))) if errors else Decimal("0")
                conn.execute("""INSERT INTO forecast_learning(factor_key,sample_count,learned_multiplier,mean_absolute_percent_error,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(factor_key) DO UPDATE SET sample_count=excluded.sample_count,
                    learned_multiplier=excluded.learned_multiplier,mean_absolute_percent_error=excluded.mean_absolute_percent_error,updated_at=excluded.updated_at""",
                    (key, len(ratios), f"{learned.quantize(Decimal('0.0001')):.4f}", f"{mape.quantize(Decimal('0.01')):.2f}", now_iso()))
        operational = self.learn_operational_patterns(daily)
        return {"forecasts_scored": updated, "mean_absolute_percent_error": money(sum(errors, Decimal("0")) / Decimal(len(errors))) if errors else Decimal("0"), **operational}

    def learn_operational_patterns(self, daily_sales: dict[date, Decimal] | None = None) -> dict[str, Any]:
        """Learn weather/event sales effects and inventory-par recommendations.

        Effects are treated as correlations, not causal facts. A factor is only
        learned when enough comparable observations exist, preventing one rainy
        Tuesday from rewriting the restaurant's ordering model.
        """
        today = date.today()
        daily = daily_sales or self._daily_sales(today - timedelta(days=180), today)
        if not daily:
            return {"factors_learned": 0, "par_recommendations": []}
        with self.workspace.connect() as conn:
            weather = {date.fromisoformat(r["weather_date"]): dict(r) for r in conn.execute(
                "SELECT * FROM weather_daily WHERE weather_date BETWEEN ? AND ?", ((today-timedelta(days=180)).isoformat(), today.isoformat()))}
            events = conn.execute("SELECT * FROM local_events WHERE end_date>=? AND event_date<=?", ((today-timedelta(days=180)).isoformat(), today.isoformat())).fetchall()
            item_rows = conn.execute("SELECT item_id,item_name,current_price,estimated_on_hand,par_override_count_units,units_per_purchase_unit,lead_time_days,order_cycle_days,safety_stock_days FROM items WHERE active=1").fetchall()
            usage_rows = conn.execute("SELECT item_id,AVG(CAST(average_daily_usage AS REAL)) avg_daily,COUNT(*) samples FROM monthly_item_usage WHERE month>=? GROUP BY item_id", ((today-timedelta(days=180)).strftime("%Y-%m"),)).fetchall()
            waste_rows = conn.execute("SELECT item_id,SUM(CAST(quantity_count_units AS REAL)) waste_qty,SUM(CAST(estimated_cost AS REAL)) waste_cost FROM waste_events WHERE event_date>=? AND event_date<=? GROUP BY item_id", ((today-timedelta(days=180)).isoformat(), today.isoformat())).fetchall()
        factors: dict[str, list[Decimal]] = defaultdict(list)
        weekdays: dict[int, list[Decimal]] = defaultdict(list)
        for day, sales in daily.items():
            if sales <= 0:
                continue
            weekdays[day.weekday()].append(sales)
        for day, sales in daily.items():
            baseline_values = [v for d, v in daily.items() if d.weekday() == day.weekday() and d != day]
            if len(baseline_values) < 3 or sales <= 0:
                continue
            baseline = Decimal(str(statistics.median([float(v) for v in baseline_values])))
            if baseline <= 0:
                continue
            ratio = max(Decimal("0.50"), min(Decimal("1.50"), sales / baseline))
            w = weather.get(day)
            if w:
                rain = dec(w.get("precipitation_inches")); high = dec(w.get("temperature_max_f")); wind = dec(w.get("wind_mph"))
                if rain >= Decimal("0.50") or wind >= Decimal("35"):
                    factors["weather:severe"] .append(ratio)
                elif rain >= Decimal("0.15"):
                    factors["weather:rain"] .append(ratio)
                elif high >= Decimal("95"):
                    factors["weather:hot"] .append(ratio)
                elif high <= Decimal("40"):
                    factors["weather:cold"] .append(ratio)
            for event in events:
                try:
                    start = date.fromisoformat(event["event_date"]); end = date.fromisoformat(event["end_date"])
                except (TypeError, ValueError):
                    continue
                if start <= day <= end:
                    name = str(event["event_name"]).lower()
                    key = "holiday" if any(x in name for x in ("holiday", "christmas", "thanksgiving", "new year", "independence", "memorial", "labor day", "easter", "valentine")) else "event:" + re.sub(r"[^a-z0-9]+", "_", str(event["category"] or "local").lower()).strip("_")
                    factors[key].append(ratio)
        learned = 0
        with self.workspace.connect() as conn:
            for key, ratios in factors.items():
                if len(ratios) < 3:
                    continue
                multiplier = Decimal(str(statistics.median([float(x) for x in ratios])))
                multiplier = max(Decimal("0.70"), min(Decimal("1.30"), multiplier))
                conn.execute("""INSERT INTO forecast_learning(factor_key,sample_count,learned_multiplier,mean_absolute_percent_error,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(factor_key) DO UPDATE SET sample_count=excluded.sample_count,learned_multiplier=excluded.learned_multiplier,updated_at=excluded.updated_at""",
                    (key, len(ratios), f"{multiplier:.4f}", "0.00", now_iso()))
                learned += 1
        par_recommendations: list[dict[str, Any]] = []
        for row in item_rows:
            usage = next((dec(r["avg_daily"]) for r in usage_rows if r["item_id"] == row["item_id"]), Decimal("0"))
            current_par = dec(row["par_override_count_units"])
            if usage <= 0 or current_par <= 0:
                continue
            coverage_days = dec(row["lead_time_days"]) + dec(row["order_cycle_days"]) + dec(row["safety_stock_days"])
            target = (usage * coverage_days).quantize(Decimal("0.01"))
            waste = next((r for r in waste_rows if r["item_id"] == row["item_id"]), None)
            waste_qty = dec(waste["waste_qty"]) if waste else Decimal("0")
            waste_adjustment = Decimal("0")
            if usage > 0 and waste_qty > 0:
                waste_adjustment = max(Decimal("0"), min(Decimal("0.15"), waste_qty / usage / Decimal("180")))
            target = (target * (Decimal("1") - waste_adjustment)).quantize(Decimal("0.01"))
            delta_percent = ((target / current_par) - 1) * 100 if current_par else Decimal("0")
            if abs(delta_percent) >= Decimal("10"):
                direction = "increase" if delta_percent > 0 else "decrease"
                reason = ("Observed usage indicates the current par is too low for lead time + order cycle + safety stock." if direction == "increase" else "Observed usage plus persistent waste indicates the current par may be too high; reducing it could lower spoilage.")
                if waste_adjustment > 0:
                    reason += f" Waste history reduced the target by {waste_adjustment*100:.1f}%."
                par_recommendations.append({"item_id": row["item_id"], "item_name": row["item_name"], "current_par": float(current_par), "recommended_par": float(target), "increase_percent": float(delta_percent), "sample_count": next((int(r["samples"]) for r in usage_rows if r["item_id"] == row["item_id"]), 0), "waste_qty_180d": float(waste_qty), "reason": reason + " Manager approval required."})
        return {"factors_learned": learned, "par_recommendations": par_recommendations[:12]}

    def list_forecasts(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute("SELECT * FROM demand_forecasts ORDER BY forecast_date DESC LIMIT ?", (limit,)).fetchall()

    def forecast_accuracy(self) -> dict[str, Any]:
        with self.workspace.connect() as conn:
            row = conn.execute("SELECT COUNT(*) n,AVG(CAST(error_percent AS REAL)) mape,AVG(CAST(absolute_error AS REAL)) mae FROM demand_forecasts WHERE status='Scored'").fetchone()
        mape = dec(row["mape"]); return {"sample_count": int(row["n"]), "mape": mape.quantize(Decimal('0.01')), "accuracy_percent": max(Decimal("0"), Decimal("100") - mape).quantize(Decimal('0.01')), "mean_absolute_error": money(row["mae"])}

    # ------------------------------------------------------------------
    # Advanced usage, profitability, sales-driven ordering
    # ------------------------------------------------------------------
    def usage_variance(self, month: str) -> list[dict[str, Any]]:
        base = {row["item_id"]: row for row in self.phase2.recipe_variance(month)}
        start = date.fromisoformat(month + "-01")
        end = (date(start.year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1))
        with self.workspace.connect() as conn:
            adjustments = {row["item_id"]: dec(row["total"]) for row in conn.execute("SELECT item_id,SUM(CAST(quantity_delta AS REAL)) total FROM inventory_adjustments WHERE adjustment_date BETWEEN ? AND ? GROUP BY item_id", (start.isoformat(), end.isoformat()))}
            items = {row["item_id"]: row for row in conn.execute("SELECT item_id,current_price,units_per_purchase_unit FROM items")}
        output = []
        for item_id, row in base.items():
            transfer_adjustment = adjustments.get(item_id, Decimal("0"))
            actual = dec(row["actual_depletion"]) + transfer_adjustment
            expected = dec(row["expected_depletion"])
            shrink = actual - expected
            item = items.get(item_id); unit_cost = money(dec(item["current_price"]) / (dec(item["units_per_purchase_unit"], "1") or Decimal("1"))) if item else Decimal("0")
            shrink_cost = money(max(Decimal("0"), shrink) * unit_cost)
            pct = (shrink / expected * 100).quantize(Decimal('0.01')) if expected else Decimal("0")
            status = (
                "High Shrinkage" if pct > 10
                else "Review" if pct > 5
                else "High Variance" if pct < -10
                else "Review" if pct < -5
                else "Normal"
            )
            output.append({**row, "transfer_adjustment": f"{transfer_adjustment:.4f}", "transfer_adjusted_actual": f"{actual:.4f}",
                           "unexplained_variance": f"{shrink:.4f}", "shrinkage_percent": f"{pct:.2f}", "estimated_shrinkage_cost": f"{shrink_cost:.2f}",
                           "status": status})
        return output

    def menu_profitability(self, start: str | None = None, end: str | None = None, target_food_cost_percent: Any = 30) -> list[dict[str, Any]]:
        start = start or f"{date.today().year}-01-01"; end = end or date.today().isoformat()
        rows = self.phase2.list_menu_costs(start, end)
        theoretical_total = sum((money(row["theoretical_food_cost"]) for row in rows), Decimal("0"))
        usage_rows = [
            row
            for month in self._month_keys(start, end)
            for row in self._complete_month_usage(month)
        ]
        actual_cost = money(sum((dec(row["estimated_usage_cost"]) for row in usage_rows), Decimal("0")))
        with self.workspace.connect() as conn:
            waste = money(conn.execute("SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) FROM waste_events WHERE event_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
        variance_factor = (actual_cost / theoretical_total) if theoretical_total > 0 and actual_cost > 0 else Decimal("1")
        variance_factor = max(Decimal("0.75"), min(Decimal("1.75"), variance_factor))
        target = dec(target_food_cost_percent, "30") / Decimal("100")
        output = []
        for row in rows:
            recipe_cost = money(row["recipe_cost"]); true_cost = money(recipe_cost * variance_factor)
            price = money(row["menu_price"]); qty_sold = dec(row["quantity_sold"]); sales = money(row["net_sales"])
            true_pct = (true_cost / price * 100).quantize(Decimal('0.01')) if price else Decimal("0")
            contribution = money(price - true_cost); total_contribution = money(sales - true_cost * qty_sold)
            recommended = money(true_cost / target) if target > 0 else price
            output.append({**row, "true_menu_cost": f"{true_cost:.2f}", "true_food_cost_percent": f"{true_pct:.2f}",
                           "true_contribution_margin": f"{contribution:.2f}", "estimated_total_contribution": f"{total_contribution:.2f}",
                           "recommended_price": f"{recommended:.2f}", "price_change_needed": f"{money(recommended-price):.2f}",
                           "actual_to_theoretical_factor": f"{variance_factor.quantize(Decimal('0.0001')):.4f}",
                           "profitability_status": "Strong" if true_pct <= dec(target_food_cost_percent) else "Review Price or Recipe",
                           "waste_pool": f"{waste:.2f}"})
        return output

    def generate_sales_driven_order_batch(self, as_of: str | date | None = None, forecast_days: int = 9) -> dict[str, Any]:
        target = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or date.today()
        forecasts = self.generate_forecast_range(target, forecast_days)
        forecast_sales = sum((f.predicted_sales for f in forecasts), Decimal("0"))
        history_start = target - timedelta(days=28)
        daily_sales = self._daily_sales(history_start, target - timedelta(days=1))
        historical_total = sum(daily_sales.values(), Decimal("0"))
        scale = max(Decimal("0.60"), min(Decimal("1.60"), forecast_sales / historical_total * Decimal("28") / Decimal(forecast_days) if historical_total else Decimal("1")))
        batch = self.planning.generate_order_predictions(target)
        updated = 0
        with self.workspace.connect() as conn:
            for row in conn.execute("SELECT * FROM order_predictions WHERE batch_id=?", (batch["batch_id"],)).fetchall():
                original = dec(row["suggested_order_quantity"])
                adjusted = (original * scale).quantize(QTY, rounding=ROUND_HALF_UP)
                multiple = dec(row["order_multiple"], "1") or Decimal("1")
                adjusted = (adjusted / multiple).to_integral_value(rounding="ROUND_CEILING") * multiple if adjusted > 0 else Decimal("0")
                cost = money(adjusted * dec(row["current_price"]))
                conn.execute("UPDATE order_predictions SET suggested_order_quantity=?,manager_order_quantity=?,estimated_order_cost=?,notes=? WHERE prediction_id=?",
                             (f"{adjusted:.4f}", f"{adjusted:.4f}", f"{cost:.2f}", f"Sales/event/weather forecast scale {scale:.3f}; manager review required", row["prediction_id"]))
                updated += 1
            conn.execute("UPDATE order_batches SET notes=? WHERE batch_id=?", (f"Phase 3 sales-driven forecast. Projected sales ${forecast_sales:.2f}; scale {scale:.3f}. Manager review required.", batch["batch_id"]))
        return {**batch, "forecast_sales": money(forecast_sales), "sales_scale": scale.quantize(Decimal('0.0001')), "updated_items": updated}

    # ------------------------------------------------------------------
    # Distributor exchanges
    # ------------------------------------------------------------------
    def save_distributor_profile(self, distributor_name: str, *, vendor_match: str = "", connector_type: str = "Folder Exchange",
                                 account_number: str = "", outbound_folder: str = "", inbound_folder: str = "",
                                 order_format: str = "CSV", confirmation_format: str = "CSV") -> str:
        name = str(distributor_name or "").strip()
        if not name:
            raise Phase3Error("Distributor name is required.")
        distributor_id = "DIST-" + hashlib.sha256(name.lower().encode()).hexdigest()[:12].upper()
        outbound = Path(outbound_folder).expanduser() if outbound_folder else Path(self.workspace.folders["distributors"]) / safe_name(name) / "Outbound"
        inbound = Path(inbound_folder).expanduser() if inbound_folder else Path(self.workspace.folders["distributors"]) / safe_name(name) / "Inbound"
        outbound.mkdir(parents=True, exist_ok=True); inbound.mkdir(parents=True, exist_ok=True)
        with self.workspace.connect() as conn:
            conn.execute("""INSERT INTO distributor_profiles(distributor_id,distributor_name,vendor_name_match,connector_type,account_number,
                outbound_folder,inbound_folder,order_format,confirmation_format,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(distributor_id) DO UPDATE SET vendor_name_match=excluded.vendor_name_match,connector_type=excluded.connector_type,
                account_number=excluded.account_number,outbound_folder=excluded.outbound_folder,inbound_folder=excluded.inbound_folder,
                order_format=excluded.order_format,confirmation_format=excluded.confirmation_format,updated_at=excluded.updated_at""",
                (distributor_id, name, vendor_match or name, connector_type, account_number, str(outbound), str(inbound), order_format, confirmation_format, now_iso()))
        return distributor_id

    def list_distributors(self) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute("SELECT d.*,(SELECT COUNT(*) FROM distributor_catalog c WHERE c.distributor_id=d.distributor_id) catalog_count FROM distributor_profiles d WHERE active=1 ORDER BY distributor_name").fetchall()

    def import_distributor_catalog(self, distributor_id: str, path: Path) -> dict[str, Any]:
        path = Path(path)
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        imported, skipped, errors = 0, 0, []
        with self.workspace.connect() as conn:
            if not conn.execute("SELECT 1 FROM distributor_profiles WHERE distributor_id=?", (distributor_id,)).fetchone():
                raise Phase3Error("Distributor profile not found.")
            for index, row in enumerate(rows, 2):
                try:
                    sku = str(row.get("SKU") or row.get("Distributor SKU") or row.get("Item Number") or "").strip()
                    description = str(row.get("Description") or row.get("Item") or row.get("Product") or "").strip()
                    if not sku or not description:
                        raise ValueError("SKU and Description are required")
                    item = conn.execute("SELECT item_id FROM items WHERE vendor_sku=? COLLATE NOCASE OR normalized_description=? LIMIT 1", (sku, normalize(description).upper())).fetchone()
                    conn.execute("""INSERT INTO distributor_catalog(distributor_id,distributor_sku,description,brand,pack,unit_price,effective_date,item_id,raw_json)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(distributor_id,distributor_sku) DO UPDATE SET description=excluded.description,
                        brand=excluded.brand,pack=excluded.pack,unit_price=excluded.unit_price,effective_date=excluded.effective_date,item_id=excluded.item_id,raw_json=excluded.raw_json""",
                        (distributor_id, sku, description, row.get("Brand", ""), row.get("Pack", ""), f"{money(row.get('Price') or row.get('Unit Price')):.2f}",
                         str(row.get("Effective Date") or date.today().isoformat()), item["item_id"] if item else None, json.dumps(row)))
                    imported += 1
                except Exception as exc:
                    skipped += 1; errors.append(f"Row {index}: {exc}")
        return {"imported": imported, "skipped": skipped, "errors": errors}

    def export_distributor_orders(self, distributor_id: str, po_ids: Iterable[str] | None = None) -> list[Path]:
        with self.workspace.connect() as conn:
            profile = conn.execute("SELECT * FROM distributor_profiles WHERE distributor_id=?", (distributor_id,)).fetchone()
            if not profile:
                raise Phase3Error("Distributor profile not found.")
            ids = list(po_ids or [])
            params: list[Any] = []
            query = "SELECT * FROM purchase_orders WHERE vendor_name LIKE ?"
            params.append(f"%{profile['vendor_name_match'] or profile['distributor_name']}%")
            if ids:
                query += " AND po_id IN (" + ",".join("?" for _ in ids) + ")"; params.extend(ids)
            query += " ORDER BY po_date,po_id"
            pos = conn.execute(query, tuple(params)).fetchall()
            output: list[Path] = []
            for po in pos:
                lines = conn.execute("SELECT * FROM purchase_order_lines WHERE po_id=? ORDER BY item_name", (po["po_id"],)).fetchall()
                fmt = str(profile["order_format"] or "CSV").upper()
                suffix = ".json" if fmt == "JSON" else ".csv"
                path = Path(profile["outbound_folder"]) / safe_name(f"{po['po_id']}_{profile['distributor_name']}{suffix}")
                if fmt == "JSON":
                    payload = {"purchase_order": dict(po), "account_number": profile["account_number"], "lines": [dict(row) for row in lines]}
                    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                else:
                    with path.open("w", encoding="utf-8", newline="") as fh:
                        writer = csv.writer(fh); writer.writerow(["PO Number","PO Date","Expected Delivery","Account Number","Distributor SKU","Description","Quantity","Unit","Unit Price","Line Total"])
                        for line in lines:
                            writer.writerow([po["po_id"],po["po_date"],po["expected_delivery_date"] or "",profile["account_number"] or "",line["vendor_sku"] or "",line["item_name"],line["quantity"],line["purchase_unit"] or "",line["unit_price"],line["line_total"]])
                exchange_id = f"DISTX-{uuid.uuid4().hex[:12].upper()}"
                conn.execute("INSERT INTO distributor_exchanges(exchange_id,distributor_id,exchange_type,reference_id,file_path,status,row_count,total_amount,created_by,created_at) VALUES(?,?,?,?,?,'Exported',?,?,?,?)",
                             (exchange_id, distributor_id, "Purchase Order", po["po_id"], str(path), len(lines), po["subtotal"], self.controls.current_user.username if self.controls.current_user else "system", now_iso()))
                output.append(path)
            return output

    def import_distributor_confirmation(self, distributor_id: str, path: Path) -> dict[str, Any]:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        updated = 0; errors = []
        with self.workspace.connect() as conn:
            for index, row in enumerate(rows, 2):
                po_id = str(row.get("PO Number") or row.get("PO") or row.get("po_id") or "").strip()
                status = str(row.get("Status") or "Confirmed").strip()
                if not po_id:
                    errors.append(f"Row {index}: PO Number missing"); continue
                result = conn.execute("UPDATE purchase_orders SET status=?,expected_delivery_date=COALESCE(NULLIF(?,''),expected_delivery_date),notes=TRIM(COALESCE(notes,'') || ' ' || ?) WHERE po_id=?",
                                      (status, str(row.get("Expected Delivery") or ""), str(row.get("Notes") or ""), po_id))
                updated += result.rowcount
            exchange_id = f"DISTX-{uuid.uuid4().hex[:12].upper()}"
            conn.execute("INSERT INTO distributor_exchanges(exchange_id,distributor_id,exchange_type,reference_id,file_path,status,row_count,total_amount,created_by,created_at,details_json) VALUES(?,?,?,?,?,'Imported',?,0,?,?,?)",
                         (exchange_id, distributor_id, "Order Confirmation", "", str(Path(path).resolve()), len(rows), self.controls.current_user.username if self.controls.current_user else "system", now_iso(), json.dumps({"updated": updated, "errors": errors})))
        return {"updated": updated, "errors": errors}

    def list_distributor_exchanges(self, limit: int = 300) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute("SELECT e.*,d.distributor_name FROM distributor_exchanges e JOIN distributor_profiles d ON d.distributor_id=e.distributor_id ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------------------
    # Quantified value and owner reporting
    # ------------------------------------------------------------------
    def log_savings(self, category: str, description: str, value: Any, *, confidence: str = "Estimated",
                    source_type: str = "", source_id: str = "", created_by: str = "system") -> str:
        savings_id = f"SAVE-{uuid.uuid4().hex[:12].upper()}"
        with self.workspace.connect() as conn:
            conn.execute("INSERT INTO savings_events(savings_id,event_date,category,description,estimated_value,confidence,source_type,source_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (savings_id, date.today().isoformat(), category, description, f"{money(value):.2f}", confidence, source_type, source_id, created_by, now_iso()))
        return savings_id

    def savings_dashboard(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        start = start or f"{date.today().year}-01-01"; end = end or date.today().isoformat()
        settings = self.workspace.load_settings()
        minutes_per_invoice = dec(settings.get("estimated_manual_invoice_minutes", 8), "8")
        manager_hourly = dec(settings.get("estimated_manager_hourly_cost", 25), "25")
        with self.workspace.connect() as conn:
            invoice_count = int(conn.execute("SELECT COUNT(*) FROM invoices WHERE status='Approved' AND invoice_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
            expected_credits = money(conn.execute("SELECT COALESCE(SUM(CAST(credit_expected AS REAL)),0) FROM receiving_lines r JOIN receiving_sessions s ON s.session_id=r.session_id WHERE s.received_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
            price_alerts = int(conn.execute("SELECT COUNT(*) FROM price_history WHERE price_alert=1 AND invoice_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
            waste_cost = money(conn.execute("SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) FROM waste_events WHERE event_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
            manual = money(conn.execute("SELECT COALESCE(SUM(CAST(estimated_value AS REAL)),0) FROM savings_events WHERE event_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
        shrink = money(sum(
            (
                money(row["estimated_shrinkage_cost"])
                for month in self._month_keys(start, end)
                if self._complete_month_usage(month)
                for row in self.usage_variance(month)
            ),
            Decimal("0"),
        ))
        hours_saved = (Decimal(invoice_count) * minutes_per_invoice / Decimal("60")).quantize(Decimal('0.01'))
        labor_value = money(hours_saved * manager_hourly)
        detected_value = money(expected_credits + manual + labor_value)
        return {"period_start": start, "period_end": end, "invoice_count": invoice_count, "invoice_hours_saved": hours_saved,
                "estimated_labor_value": labor_value, "expected_vendor_credits": expected_credits, "price_alerts_detected": price_alerts,
                "documented_waste_cost": waste_cost, "estimated_shrinkage_exposure": shrink, "manual_savings": manual,
                "estimated_value_delivered": detected_value,
                "method_note": "Estimated value combines configured invoice-entry labor savings, expected receiving credits, and manually confirmed savings. Waste and shrinkage are exposures, not claimed savings."}

    def export_owner_report(self, start: str, end: str, destination: Path | None = None) -> Path:
        start, end = parse_date(start), parse_date(end)
        portfolio = self.portfolio_summary(int(start[:4])); savings = self.savings_dashboard(start, end)
        profitability = self.menu_profitability(start, end)
        accuracy = self.forecast_accuracy()
        report_id = f"OWNER-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        destination = destination or Path(self.workspace.folders["owner_reports"]) / f"{report_id}.html"
        rows = "".join(f"<tr><td>{html.escape(str(r['name']))}</td><td>${float(r['sales']):,.2f}</td><td>${float(r['purchases']):,.2f}</td><td>{float(r['purchase_percent']):,.2f}%</td><td>${float(r['inventory_value']):,.2f}</td><td>${float(r['waste_cost']):,.2f}</td><td>{r['open_exceptions']}</td></tr>" for r in portfolio["locations"])
        menu_rows = "".join(f"<tr><td>{html.escape(str(r['menu_item_name']))}</td><td>${float(r['menu_price']):,.2f}</td><td>${float(r['true_menu_cost']):,.2f}</td><td>{float(r['true_food_cost_percent']):,.2f}%</td><td>${float(r['true_contribution_margin']):,.2f}</td><td>${float(r['recommended_price']):,.2f}</td><td>{html.escape(str(r['profitability_status']))}</td></tr>" for r in profitability[:100])
        body = f"""<!doctype html><html><head><meta charset='utf-8'><title>Owner Report</title><style>
        body{{font-family:Segoe UI,Arial,sans-serif;margin:28px;color:#1f2937}}h1,h2{{color:#17324d}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{border:1px solid #d1d5db;padding:14px;border-radius:8px}}table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border:1px solid #d1d5db;padding:7px;text-align:left}}th{{background:#17324d;color:white}}.note{{color:#6b7280}}</style></head><body>
        <h1>MarginMise Owner Report</h1><p>{start} through {end}</p><div class='kpis'>
        <div class='card'><b>Portfolio Sales</b><br>${float(portfolio['total_sales']):,.2f}</div>
        <div class='card'><b>Purchases</b><br>${float(portfolio['total_purchases']):,.2f}</div>
        <div class='card'><b>Estimated Value Delivered</b><br>${float(savings['estimated_value_delivered']):,.2f}</div>
        <div class='card'><b>Forecast Accuracy</b><br>{float(accuracy['accuracy_percent']):,.2f}% ({accuracy['sample_count']} scored)</div></div>
        <h2>Location Comparison</h2><table><tr><th>Location</th><th>Sales</th><th>Purchases</th><th>Purchase %</th><th>Inventory</th><th>Waste</th><th>Open Exceptions</th></tr>{rows}</table>
        <h2>Menu Profitability</h2><table><tr><th>Menu Item</th><th>Price</th><th>True Cost</th><th>Food Cost %</th><th>Contribution</th><th>Suggested Price</th><th>Status</th></tr>{menu_rows}</table>
        <h2>Value and Risk</h2><p>Invoice time saved: {savings['invoice_hours_saved']} hours (${float(savings['estimated_labor_value']):,.2f}). Expected vendor credits: ${float(savings['expected_vendor_credits']):,.2f}. Documented waste exposure: ${float(savings['documented_waste_cost']):,.2f}. Estimated shrinkage exposure: ${float(savings['estimated_shrinkage_exposure']):,.2f}.</p>
        <p class='note'>{html.escape(savings['method_note'])}</p></body></html>"""
        destination.write_text(body, encoding="utf-8")
        with self.workspace.connect() as conn:
            conn.execute("INSERT INTO owner_report_history(report_id,period_start,period_end,report_type,file_path,location_count,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (report_id, start, end, "HTML Owner Report", str(destination), portfolio["location_count"], self.controls.current_user.username if self.controls.current_user else "system", now_iso()))
        return destination

    def dashboard_summary(self) -> dict[str, Any]:
        portfolio = self.portfolio_summary(date.today().year)
        accuracy = self.forecast_accuracy(); savings = self.savings_dashboard()
        with self.workspace.connect() as conn:
            open_transfers = int(conn.execute("SELECT COUNT(*) FROM inventory_transfers WHERE status IN ('Draft','Shipped','In Transit')").fetchone()[0])
            future_events = int(conn.execute("SELECT COUNT(*) FROM local_events WHERE end_date>=?", (date.today().isoformat(),)).fetchone()[0])
            distributors = int(conn.execute("SELECT COUNT(*) FROM distributor_profiles WHERE active=1").fetchone()[0])
        return {"portfolio_location_count": portfolio["location_count"], "portfolio_sales": portfolio["total_sales"],
                "forecast_accuracy": accuracy["accuracy_percent"], "forecast_samples": accuracy["sample_count"],
                "estimated_value_delivered": savings["estimated_value_delivered"], "open_transfers": open_transfers,
                "upcoming_events": future_events, "distributor_profiles": distributors}

    def export_csvs(self) -> list[Path]:
        queries = {
            "inventory_transfers.csv": "SELECT * FROM inventory_transfers ORDER BY transfer_date DESC",
            "inventory_transfer_lines.csv": "SELECT * FROM inventory_transfer_lines ORDER BY transfer_id,item_name",
            "inventory_adjustments.csv": "SELECT * FROM inventory_adjustments ORDER BY adjustment_date DESC",
            "local_events.csv": "SELECT * FROM local_events ORDER BY event_date",
            "weather_daily.csv": "SELECT * FROM weather_daily ORDER BY weather_date",
            "demand_forecasts.csv": "SELECT * FROM demand_forecasts ORDER BY forecast_date",
            "forecast_learning.csv": "SELECT * FROM forecast_learning ORDER BY factor_key",
            "distributor_profiles.csv": "SELECT * FROM distributor_profiles ORDER BY distributor_name",
            "distributor_catalog.csv": "SELECT * FROM distributor_catalog ORDER BY distributor_id,description",
            "distributor_exchanges.csv": "SELECT * FROM distributor_exchanges ORDER BY created_at DESC",
            "savings_events.csv": "SELECT * FROM savings_events ORDER BY event_date DESC",
            "owner_report_history.csv": "SELECT * FROM owner_report_history ORDER BY created_at DESC",
        }
        paths = []
        with self.workspace.connect() as conn:
            for filename, query in queries.items():
                path = Path(self.workspace.folders["exports"]) / filename
                rows = conn.execute(query).fetchall()
                with path.open("w", encoding="utf-8", newline="") as fh:
                    if rows:
                        writer = csv.DictWriter(fh, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(dict(row) for row in rows)
                paths.append(path)
        return paths

