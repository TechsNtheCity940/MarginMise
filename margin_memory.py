#!/usr/bin/env python3
"""MarginMemory decision capture for MarginMise v3.5.

Phase 1 records material manager decisions and the operational context that
existed when they were made.  It does not generate autonomous actions.  The
ledger is deliberately local-first, explainable, and backward compatible with
MarginMise v3.3-compatible workspaces.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

MONEY = Decimal("0.01")
QTY = Decimal("0.0001")

REASON_CODES: tuple[tuple[str, str], ...] = (
    ("WEATHER", "Weather"),
    ("LOCAL_EVENT", "Local event"),
    ("VENDOR_RELIABILITY", "Vendor reliability"),
    ("STORAGE_LIMITATION", "Storage limitation"),
    ("PROMOTION", "Promotion"),
    ("PRODUCT_QUALITY", "Product quality concern"),
    ("MANAGER_EXPERIENCE", "Manager experience"),
    ("INVENTORY_BALANCE", "Inventory balance between locations"),
    ("RECEIVING_EXCEPTION", "Receiving exception"),
    ("DOCUMENT_CORRECTION", "Document correction"),
    ("OTHER", "Other"),
    ("UNDOCUMENTED", "Undocumented"),
)
REASON_LABELS = dict(REASON_CODES)

MARGIN_MEMORY_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS margin_memory_decisions (
    decision_id TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL UNIQUE,
    decision_type TEXT NOT NULL,
    location_id TEXT NOT NULL,
    location_name TEXT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_name TEXT,
    recommended_action_json TEXT NOT NULL DEFAULT '{}',
    actual_action_json TEXT NOT NULL DEFAULT '{}',
    override_amount TEXT,
    override_percent TEXT,
    reason_code TEXT NOT NULL DEFAULT 'UNDOCUMENTED',
    manager_note TEXT,
    decision_maker_id TEXT,
    decision_maker TEXT NOT NULL,
    decision_maker_role TEXT,
    decision_time TEXT NOT NULL,
    evaluation_start_date TEXT,
    evaluation_end_date TEXT,
    status TEXT NOT NULL DEFAULT 'Pending Outcome',
    confidence_at_decision TEXT,
    source_entity_type TEXT,
    source_entity_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_margin_memory_decisions_status
    ON margin_memory_decisions(status, decision_time DESC);
CREATE INDEX IF NOT EXISTS idx_margin_memory_decisions_type
    ON margin_memory_decisions(decision_type, subject_id, decision_time DESC);
CREATE INDEX IF NOT EXISTS idx_margin_memory_decisions_manager
    ON margin_memory_decisions(decision_maker, decision_time DESC);
CREATE INDEX IF NOT EXISTS idx_margin_memory_decisions_location
    ON margin_memory_decisions(location_id, decision_time DESC);

CREATE TABLE IF NOT EXISTS margin_memory_context (
    context_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE REFERENCES margin_memory_decisions(decision_id) ON DELETE CASCADE,
    business_date TEXT,
    weekday INTEGER,
    location_id TEXT,
    vendor_name TEXT,
    product_id TEXT,
    category TEXT,
    current_inventory TEXT,
    inventory_days_remaining TEXT,
    forecast_sales TEXT,
    average_daily_sales TEXT,
    weather_code TEXT,
    temperature TEXT,
    precipitation_probability TEXT,
    event_type TEXT,
    event_impact TEXT,
    lead_time_days TEXT,
    order_cycle_days TEXT,
    safety_stock_days TEXT,
    open_purchase_orders INTEGER NOT NULL DEFAULT 0,
    open_transfers INTEGER NOT NULL DEFAULT 0,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS margin_memory_outcomes (
    outcome_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE REFERENCES margin_memory_decisions(decision_id) ON DELETE CASCADE,
    evaluation_date TEXT,
    actual_sales TEXT,
    actual_usage TEXT,
    ending_inventory TEXT,
    waste_quantity TEXT,
    waste_cost TEXT,
    stockout_quantity TEXT,
    estimated_lost_sales TEXT,
    emergency_purchase_cost TEXT,
    vendor_credit_recovered TEXT,
    transfer_cost TEXT,
    estimated_margin_effect TEXT,
    system_action_estimate TEXT,
    manager_action_result TEXT,
    outcome_grade TEXT,
    evaluation_confidence TEXT,
    explanation_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at TEXT
);

CREATE TABLE IF NOT EXISTS margin_memory_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    location_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    recommended_action_json TEXT NOT NULL,
    supporting_decision_ids_json TEXT NOT NULL DEFAULT '[]',
    similarity_score TEXT,
    confidence TEXT,
    estimated_value TEXT,
    explanation TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    accepted_at TEXT,
    dismissed_at TEXT,
    dismissal_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_margin_memory_recommendations_status
    ON margin_memory_recommendations(status, generated_at DESC);
CREATE TABLE IF NOT EXISTS recommendation_cache (
    item_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    recommendation_json TEXT NOT NULL DEFAULT '{}',
    generated_at TEXT NOT NULL,
    PRIMARY KEY (item_id, location_id, as_of_date)
);

-- Missing inventory_estimates table (required by evaluate_pending_outcomes and recommended_adjustments_for_item)
CREATE TABLE IF NOT EXISTS inventory_estimates (
    item_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    estimated_on_hand TEXT,
    average_daily_usage TEXT,
    par_quantity_count_units TEXT,
    lead_time_days TEXT,
    order_cycle_days TEXT,
    safety_stock_days TEXT,
    inventory_confidence TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_inventory_estimates_item
    ON inventory_estimates(item_id, as_of_date DESC);

CREATE TABLE IF NOT EXISTS margin_memory_sales_factors (
    factor_id TEXT PRIMARY KEY,
    location_id TEXT NOT NULL,
    factor_type TEXT NOT NULL,
    factor_key TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    baseline_sales TEXT NOT NULL,
    observed_sales TEXT NOT NULL,
    multiplier TEXT NOT NULL,
    confidence TEXT NOT NULL,
    last_observed_date TEXT,
    explanation TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(location_id, factor_type, factor_key)
);
CREATE INDEX IF NOT EXISTS idx_margin_memory_sales_factors
    ON margin_memory_sales_factors(location_id, factor_type, confidence DESC);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def dec(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal(default)


def qty(value: Any) -> Decimal:
    return dec(value).quantize(QTY, rounding=ROUND_HALF_UP)


def money(value: Any) -> Decimal:
    return dec(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def json_safe(value: Any) -> Any:
    if isinstance(value, sqlite3.Row):
        return {key: json_safe(value[key]) for key in value.keys()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def location_id_for(path: Path) -> str:
    return "LOC-" + hashlib.sha256(
        str(path.expanduser().resolve()).lower().encode("utf-8")
    ).hexdigest()[:14].upper()


class MarginMemoryError(RuntimeError):
    pass


class MarginMemoryService:
    """Captures decisions and immutable decision-time context snapshots."""

    def __init__(self, workspace: Any, planning: Any, controls: Any):
        self.workspace = workspace
        self.planning = planning
        self.controls = controls
        self.ensure_schema()

    def ensure_schema(self) -> None:
        folder = self.workspace.root / "MarginMemory"
        folder.mkdir(parents=True, exist_ok=True)
        self.workspace.folders.setdefault("margin_memory", folder)
        with self.workspace.connect() as conn:
            conn.executescript(MARGIN_MEMORY_SCHEMA_SQL)

    @property
    def location_id(self) -> str:
        return location_id_for(self.workspace.root)

    @property
    def location_name(self) -> str:
        return str(
            self.workspace.load_settings().get("restaurant_name")
            or self.workspace.root.name
        )

    def settings(self) -> dict[str, Any]:
        data = self.workspace.load_settings()
        defaults = {
            "margin_memory_enabled": True,
            "margin_memory_materiality_threshold_percent": 10.0,
            "margin_memory_capture_order_overrides": True,
            "margin_memory_capture_transfers": True,
            "margin_memory_capture_receiving": True,
            "margin_memory_capture_invoice_corrections": True,
        }
        defaults.update(data)
        return defaults

    def _actor_fields(self, actor: Any | None = None) -> tuple[str | None, str, str]:
        user = actor or getattr(self.controls, "current_user", None)
        if user is None:
            return None, "system", "System"
        return (
            str(getattr(user, "user_id", "") or "") or None,
            str(getattr(user, "username", "") or getattr(user, "display_name", "") or "system"),
            str(getattr(user, "role", "") or "System"),
        )

    def _normalize_reason(self, reason_code: str | None) -> str:
        code = str(reason_code or "").strip().upper().replace(" ", "_")
        return code if code in REASON_LABELS else ("OTHER" if code else "UNDOCUMENTED")

    def materiality_threshold(self) -> Decimal:
        return max(
            Decimal("0"),
            dec(self.settings().get("margin_memory_materiality_threshold_percent"), "10"),
        )

    def order_override_percent(self, suggested: Any, actual: Any) -> Decimal:
        suggested_q = qty(suggested)
        actual_q = qty(actual)
        difference = abs(actual_q - suggested_q)
        if difference == 0:
            return Decimal("0.00")
        if suggested_q == 0:
            return Decimal("100.00")
        return (difference / abs(suggested_q) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def is_material_order_override(self, suggested: Any, actual: Any) -> bool:
        return self.order_override_percent(suggested, actual) >= self.materiality_threshold()

    @staticmethod
    def _confidence_value(label: Any) -> Decimal:
        text = str(label or "").lower()
        if "high" in text:
            return Decimal("0.90")
        if "medium" in text:
            return Decimal("0.70")
        if "low" in text:
            return Decimal("0.45")
        return Decimal("0.60")

    def _environment_context(self, conn: sqlite3.Connection, business_date: str) -> dict[str, Any]:
        forecast = conn.execute(
            """SELECT predicted_net_sales FROM demand_forecasts
               WHERE forecast_date=? ORDER BY created_at DESC LIMIT 1""",
            (business_date,),
        ).fetchone()
        weather = conn.execute(
            "SELECT * FROM weather_daily WHERE weather_date=?",
            (business_date,),
        ).fetchone()
        events = conn.execute(
            """SELECT event_name,category,expected_sales_impact_percent
               FROM local_events WHERE event_date<=? AND end_date>=?
               ORDER BY ABS(CAST(expected_sales_impact_percent AS REAL)) DESC""",
            (business_date, business_date),
        ).fetchall()
        open_purchase_orders = int(
            conn.execute(
                "SELECT COUNT(*) FROM purchase_orders WHERE status IN ('Draft','Approved','Confirmed','Partially Confirmed','Backordered')"
            ).fetchone()[0]
        )
        open_transfers = int(
            conn.execute(
                "SELECT COUNT(*) FROM inventory_transfers WHERE status IN ('Draft','Shipped','In Transit')"
            ).fetchone()[0]
        )
        return {
            "forecast_sales": str(forecast["predicted_net_sales"]) if forecast else "",
            "weather_code": str(weather["weather_code"]) if weather and weather["weather_code"] is not None else "",
            "temperature": str(weather["temperature_max_f"]) if weather and weather["temperature_max_f"] is not None else "",
            "precipitation_probability": str(weather["precipitation_probability"]) if weather and weather["precipitation_probability"] is not None else "",
            "event_type": ", ".join(str(row["category"] or row["event_name"]) for row in events),
            "event_impact": ", ".join(str(row["expected_sales_impact_percent"]) for row in events),
            "events": [dict(row) for row in events],
            "open_purchase_orders": open_purchase_orders,
            "open_transfers": open_transfers,
        }

    def _upsert_decision(
        self,
        *,
        source_event_key: str,
        decision_type: str,
        subject_type: str,
        subject_id: str,
        subject_name: str,
        recommended_action: dict[str, Any],
        actual_action: dict[str, Any],
        override_amount: Any = "",
        override_percent: Any = "",
        reason_code: str | None = None,
        manager_note: str | None = None,
        decision_time: str | None = None,
        evaluation_start_date: str | None = None,
        evaluation_end_date: str | None = None,
        status: str = "Pending Outcome",
        confidence_at_decision: Any = "",
        source_entity_type: str = "",
        source_entity_id: str = "",
        context: dict[str, Any] | None = None,
        actor: Any | None = None,
    ) -> str:
        actor_id, actor_name, actor_role = self._actor_fields(actor)
        stamp = now_iso()
        decision_time = decision_time or stamp
        reason = self._normalize_reason(reason_code)
        with self.workspace.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM margin_memory_decisions WHERE source_event_key=?",
                (source_event_key,),
            ).fetchone()
            if existing:
                decision_id = existing["decision_id"]
                if reason_code is None:
                    reason = str(existing["reason_code"] or "UNDOCUMENTED")
                if manager_note is None:
                    manager_note = str(existing["manager_note"] or "")
                created_at = existing["created_at"]
            else:
                decision_id = f"MM-{uuid.uuid4().hex[:18].upper()}"
                created_at = stamp
            conn.execute(
                """INSERT INTO margin_memory_decisions(
                       decision_id,source_event_key,decision_type,location_id,location_name,
                       subject_type,subject_id,subject_name,recommended_action_json,
                       actual_action_json,override_amount,override_percent,reason_code,
                       manager_note,decision_maker_id,decision_maker,decision_maker_role,
                       decision_time,evaluation_start_date,evaluation_end_date,status,
                       confidence_at_decision,source_entity_type,source_entity_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_event_key) DO UPDATE SET
                       decision_type=excluded.decision_type,
                       location_id=excluded.location_id,
                       location_name=excluded.location_name,
                       subject_type=excluded.subject_type,
                       subject_id=excluded.subject_id,
                       subject_name=excluded.subject_name,
                       recommended_action_json=excluded.recommended_action_json,
                       actual_action_json=excluded.actual_action_json,
                       override_amount=excluded.override_amount,
                       override_percent=excluded.override_percent,
                       reason_code=excluded.reason_code,
                       manager_note=excluded.manager_note,
                       decision_maker_id=excluded.decision_maker_id,
                       decision_maker=excluded.decision_maker,
                       decision_maker_role=excluded.decision_maker_role,
                       decision_time=excluded.decision_time,
                       evaluation_start_date=excluded.evaluation_start_date,
                       evaluation_end_date=excluded.evaluation_end_date,
                       status=excluded.status,
                       confidence_at_decision=excluded.confidence_at_decision,
                       source_entity_type=excluded.source_entity_type,
                       source_entity_id=excluded.source_entity_id,
                       updated_at=excluded.updated_at""",
                (
                    decision_id, source_event_key, decision_type, self.location_id,
                    self.location_name, subject_type, str(subject_id), subject_name,
                    json.dumps(json_safe(recommended_action), separators=(",", ":")),
                    json.dumps(json_safe(actual_action), separators=(",", ":")),
                    str(override_amount or ""), str(override_percent or ""), reason,
                    str(manager_note or ""), actor_id, actor_name, actor_role,
                    decision_time, evaluation_start_date, evaluation_end_date, status,
                    str(confidence_at_decision or ""), source_entity_type,
                    str(source_entity_id or ""), created_at, stamp,
                ),
            )
            context = dict(context or {})
            business_date = str(context.get("business_date") or "")
            weekday = None
            if business_date:
                try:
                    weekday = date.fromisoformat(business_date).weekday()
                except ValueError:
                    weekday = None
            context_id = f"CTX-{decision_id}"
            conn.execute(
                """INSERT INTO margin_memory_context(
                       context_id,decision_id,business_date,weekday,location_id,vendor_name,
                       product_id,category,current_inventory,inventory_days_remaining,
                       forecast_sales,average_daily_sales,weather_code,temperature,
                       precipitation_probability,event_type,event_impact,lead_time_days,
                       order_cycle_days,safety_stock_days,open_purchase_orders,open_transfers,
                       context_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(decision_id) DO UPDATE SET
                       business_date=excluded.business_date,weekday=excluded.weekday,
                       location_id=excluded.location_id,vendor_name=excluded.vendor_name,
                       product_id=excluded.product_id,category=excluded.category,
                       current_inventory=excluded.current_inventory,
                       inventory_days_remaining=excluded.inventory_days_remaining,
                       forecast_sales=excluded.forecast_sales,
                       average_daily_sales=excluded.average_daily_sales,
                       weather_code=excluded.weather_code,temperature=excluded.temperature,
                       precipitation_probability=excluded.precipitation_probability,
                       event_type=excluded.event_type,event_impact=excluded.event_impact,
                       lead_time_days=excluded.lead_time_days,
                       order_cycle_days=excluded.order_cycle_days,
                       safety_stock_days=excluded.safety_stock_days,
                       open_purchase_orders=excluded.open_purchase_orders,
                       open_transfers=excluded.open_transfers,
                       context_json=excluded.context_json""",
                (
                    context_id, decision_id, business_date, weekday, self.location_id,
                    str(context.get("vendor_name") or ""),
                    str(context.get("product_id") or ""),
                    str(context.get("category") or ""),
                    str(context.get("current_inventory") or ""),
                    str(context.get("inventory_days_remaining") or ""),
                    str(context.get("forecast_sales") or ""),
                    str(context.get("average_daily_sales") or ""),
                    str(context.get("weather_code") or ""),
                    str(context.get("temperature") or ""),
                    str(context.get("precipitation_probability") or ""),
                    str(context.get("event_type") or ""),
                    str(context.get("event_impact") or ""),
                    str(context.get("lead_time_days") or ""),
                    str(context.get("order_cycle_days") or ""),
                    str(context.get("safety_stock_days") or ""),
                    int(context.get("open_purchase_orders") or 0),
                    int(context.get("open_transfers") or 0),
                    json.dumps(json_safe(context), separators=(",", ":")),
                    stamp,
                ),
            )
        self.controls.audit(
            "margin_memory.capture" if not existing else "margin_memory.update",
            "margin_memory_decision",
            decision_id,
            f"Recorded {decision_type.lower()} for {subject_name or subject_id}",
            after={
                "decision_type": decision_type,
                "subject_id": subject_id,
                "reason_code": reason,
                "status": status,
            },
            actor=actor,
        )
        return decision_id

    def capture_order_override(
        self,
        prediction_id: int,
        manager_quantity: Any | None = None,
        *,
        reason_code: str | None = None,
        manager_note: str | None = None,
        status: str = "Pending Approval",
        actor: Any | None = None,
    ) -> str | None:
        settings = self.settings()
        if not settings.get("margin_memory_enabled", True) or not settings.get(
            "margin_memory_capture_order_overrides", True
        ):
            return None
        with self.workspace.connect() as conn:
            row = conn.execute(
                """SELECT p.*,b.as_of_date,b.status AS batch_status,i.category
                   FROM order_predictions p
                   JOIN order_batches b ON b.batch_id=p.batch_id
                   LEFT JOIN items i ON i.item_id=p.item_id
                   WHERE p.prediction_id=?""",
                (int(prediction_id),),
            ).fetchone()
            if not row:
                raise MarginMemoryError("Order prediction was not found.")
            actual = qty(
                manager_quantity
                if manager_quantity is not None
                else row["manager_order_quantity"] or row["suggested_order_quantity"]
            )
            suggested = qty(row["suggested_order_quantity"])
            source_event_key = f"order_prediction:{prediction_id}"
            if not self.is_material_order_override(suggested, actual):
                conn.execute(
                    """DELETE FROM margin_memory_decisions
                       WHERE source_event_key=? AND status='Pending Approval'""",
                    (source_event_key,),
                )
                return None
            environment = self._environment_context(conn, row["as_of_date"])
            on_hand = dec(row["estimated_on_hand"])
            avg_daily = dec(row["average_daily_usage"])
            days_remaining = (
                (on_hand / avg_daily).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if avg_daily > 0
                else Decimal("0")
            )
            context = {
                "business_date": row["as_of_date"],
                "vendor_name": row["vendor_name"],
                "product_id": row["item_id"],
                "category": row["category"],
                "current_inventory": str(on_hand),
                "inventory_days_remaining": str(days_remaining),
                "average_daily_sales": str(row["average_daily_usage"] or ""),
                "lead_time_days": str(row["lead_time_days"] or ""),
                "order_cycle_days": str(row["order_cycle_days"] or ""),
                "safety_stock_days": str(row["safety_stock_days"] or ""),
                "inventory_confidence": str(row["inventory_confidence"] or ""),
                "average_weekly_usage": str(row["average_weekly_usage"] or ""),
                "par_quantity_count_units": str(row["par_quantity_count_units"] or ""),
                "order_multiple": str(row["order_multiple"] or ""),
                **environment,
            }
        evaluation_start = str(row["as_of_date"])
        try:
            evaluation_end = (
                date.fromisoformat(evaluation_start)
                + timedelta(
                    days=max(
                        1,
                        int(
                            dec(row["lead_time_days"])
                            + dec(row["order_cycle_days"])
                        ),
                    )
                )
            ).isoformat()
        except ValueError:
            evaluation_end = ""
        difference = (actual - suggested).quantize(QTY)
        percent = self.order_override_percent(suggested, actual)
        return self._upsert_decision(
            source_event_key=source_event_key,
            decision_type="Order Override",
            subject_type="Inventory Item",
            subject_id=row["item_id"],
            subject_name=row["item_name"],
            recommended_action={
                "order_quantity": str(suggested),
                "purchase_unit": row["purchase_unit"],
                "estimated_order_cost": str(
                    money(suggested * dec(row["current_price"]))
                ),
            },
            actual_action={
                "order_quantity": str(actual),
                "purchase_unit": row["purchase_unit"],
                "estimated_order_cost": str(
                    money(actual * dec(row["current_price"]))
                ),
            },
            override_amount=str(difference),
            override_percent=str(percent),
            reason_code=reason_code,
            manager_note=manager_note,
            decision_time=now_iso(),
            evaluation_start_date=evaluation_start,
            evaluation_end_date=evaluation_end,
            status=status,
            confidence_at_decision=str(
                self._confidence_value(row["inventory_confidence"])
            ),
            source_entity_type="order_prediction",
            source_entity_id=str(prediction_id),
            context=context,
            actor=actor,
        )

    def finalize_order_batch(self, batch_id: str, *, actor: Any | None = None) -> dict[str, Any]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                "SELECT prediction_id,manager_order_quantity,suggested_order_quantity FROM order_predictions WHERE batch_id=?",
                (batch_id,),
            ).fetchall()
        captured = 0
        skipped = 0
        for row in rows:
            decision_id = self._trusted_order_override(
                int(row["prediction_id"]),
                status="Pending Outcome",
                actor=actor,
            )
            if decision_id:
                captured += 1
            else:
                skipped += 1
        self.controls.audit(
            "margin_memory.batch_finalize",
            "order_batch",
            batch_id,
            f"MarginMemory recaptured {captured} trusted order override(s) from batch finalize",
            details={"captured": captured, "below_threshold": skipped},
            actor=actor,
        )
        return {"batch_id": batch_id, "captured": captured, "below_threshold": skipped}

    def _trusted_order_override(
        self,
        prediction_id: int,
        *,
        status: str = "Ready to Evaluate",
        actor: Any | None = None,
    ) -> str | None:
        with self.workspace.connect() as conn:
            row = conn.execute(
                """SELECT p.*,b.as_of_date,b.status AS batch_status,i.category
                   FROM order_predictions p
                   JOIN order_batches b ON b.batch_id=p.batch_id
                   LEFT JOIN items i ON i.item_id=p.item_id
                   WHERE p.prediction_id=?""",
                (int(prediction_id),),
            ).fetchone()
            if not row:
                return None
            suggested = qty(row["suggested_order_quantity"])
            actual = qty(row["manager_order_quantity"] or row["suggested_order_quantity"])
            if not self.is_material_order_override(suggested, actual):
                return None
            source_event_key = f"order_prediction:{prediction_id}"
            environment = self._environment_context(conn, row["as_of_date"])
            on_hand = dec(row["estimated_on_hand"])
            avg_daily = dec(row["average_daily_usage"])
            days_remaining = (
                (on_hand / avg_daily).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if avg_daily > 0
                else Decimal("0")
            )
            context = {
                "business_date": row["as_of_date"],
                "vendor_name": row["vendor_name"],
                "product_id": row["item_id"],
                "category": row["category"],
                "current_inventory": str(on_hand),
                "inventory_days_remaining": str(days_remaining),
                "average_daily_sales": str(row["average_daily_usage"] or ""),
                "lead_time_days": str(row["lead_time_days"] or ""),
                "order_cycle_days": str(row["order_cycle_days"] or ""),
                "safety_stock_days": str(row["safety_stock_days"] or ""),
                "inventory_confidence": str(row["inventory_confidence"] or ""),
                "average_weekly_usage": str(row["average_weekly_usage"] or ""),
                "par_quantity_count_units": str(row["par_quantity_count_units"] or ""),
                "order_multiple": str(row["order_multiple"] or ""),
                **environment,
            }
            evaluation_start = str(row["as_of_date"])
            try:
                evaluation_end = (
                    date.fromisoformat(evaluation_start)
                    + timedelta(
                        days=max(
                            1,
                            int(
                                dec(row["lead_time_days"])
                                + dec(row["order_cycle_days"])
                            ),
                        )
                    )
                ).isoformat()
            except ValueError:
                evaluation_end = ""
            return self._upsert_decision(
                source_event_key=source_event_key,
                decision_type="Order Override",
                subject_type="Inventory Item",
                subject_id=row["item_id"],
                subject_name=row["item_name"],
                recommended_action={
                    "order_quantity": str(suggested),
                    "purchase_unit": row["purchase_unit"],
                    "estimated_order_cost": str(money(suggested * dec(row["current_price"]))),
                },
                actual_action={
                    "order_quantity": str(actual),
                    "purchase_unit": row["purchase_unit"],
                    "estimated_order_cost": str(money(actual * dec(row["current_price"]))),
                },
                override_amount=str((actual - suggested).quantize(QTY)),
                override_percent=str(self.order_override_percent(suggested, actual)),
                reason_code=None,
                manager_note=None,
                decision_time=now_iso(),
                evaluation_start_date=evaluation_start,
                evaluation_end_date=evaluation_end,
                status=status,
                confidence_at_decision=str(self._confidence_value(row["inventory_confidence"])),
                source_entity_type="order_prediction",
                source_entity_id=str(prediction_id),
                context=context,
                actor=actor,
            )

    def record_order_override(
        self,
        prediction_id: int,
        manager_quantity: Any | None = None,
        *,
        reason_code: str | None = None,
        manager_note: str | None = None,
        actor: Any | None = None,
    ) -> str | None:
        with self.workspace.connect() as conn:
            row = conn.execute(
                """SELECT p.*,b.as_of_date,i.category
                   FROM order_predictions p
                   JOIN order_batches b ON b.batch_id=p.batch_id
                   LEFT JOIN items i ON i.item_id=p.item_id
                   WHERE p.prediction_id=?""",
                (int(prediction_id),),
            ).fetchone()
            if not row:
                raise MarginMemoryError("Order prediction was not found.")
            actual = qty(manager_quantity if manager_quantity is not None else row["manager_order_quantity"] or row["suggested_order_quantity"])
            suggested = qty(row["suggested_order_quantity"])
            source_event_key = f"order_prediction:{prediction_id}"
            environment = self._environment_context(conn, row["as_of_date"])
            on_hand = dec(row["estimated_on_hand"])
            avg_daily = dec(row["average_daily_usage"])
            days_remaining = (
                (on_hand / avg_daily).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if avg_daily > 0
                else Decimal("0")
            )
            context = {
                "business_date": row["as_of_date"],
                "vendor_name": row["vendor_name"],
                "product_id": row["item_id"],
                "category": row["category"],
                "current_inventory": str(on_hand),
                "inventory_days_remaining": str(days_remaining),
                "average_daily_sales": str(row["average_daily_usage"] or ""),
                "lead_time_days": str(row["lead_time_days"] or ""),
                "order_cycle_days": str(row["order_cycle_days"] or ""),
                "safety_stock_days": str(row["safety_stock_days"] or ""),
                "inventory_confidence": str(row["inventory_confidence"] or ""),
                "average_weekly_usage": str(row["average_weekly_usage"] or ""),
                "par_quantity_count_units": str(row["par_quantity_count_units"] or ""),
                "order_multiple": str(row["order_multiple"] or ""),
                **environment,
            }
            evaluation_start = str(row["as_of_date"])
            try:
                evaluation_end = (
                    date.fromisoformat(evaluation_start)
                    + timedelta(
                        days=max(
                            1,
                            int(
                                dec(row["lead_time_days"])
                                + dec(row["order_cycle_days"])
                            ),
                        )
                    )
                ).isoformat()
            except ValueError:
                evaluation_end = ""
            return self._upsert_decision(
                source_event_key=source_event_key,
                decision_type="Order Override",
                subject_type="Inventory Item",
                subject_id=row["item_id"],
                subject_name=row["item_name"],
                recommended_action={
                    "order_quantity": str(suggested),
                    "purchase_unit": row["purchase_unit"],
                    "estimated_order_cost": str(money(suggested * dec(row["current_price"]))),
                },
                actual_action={
                    "order_quantity": str(actual),
                    "purchase_unit": row["purchase_unit"],
                    "estimated_order_cost": str(money(actual * dec(row["current_price"]))),
                },
                override_amount=str((actual - suggested).quantize(QTY)),
                override_percent=str(self.order_override_percent(suggested, actual)),
                reason_code=reason_code,
                manager_note=manager_note,
                decision_time=now_iso(),
                evaluation_start_date=evaluation_start,
                evaluation_end_date=evaluation_end,
                status="Pending Approval",
                confidence_at_decision=str(self._confidence_value(row["inventory_confidence"])),
                source_entity_type="order_prediction",
                source_entity_id=str(prediction_id),
                context=context,
                actor=actor,
            )

    def approve_decision(self, decision_id: str, actor: Any | None = None) -> dict[str, Any]:
        self.controls.require_permission("margin_memory.approve")
        stamp = now_iso()
        with self.workspace.connect() as conn:
            decision = conn.execute(
                "SELECT * FROM margin_memory_decisions WHERE decision_id=? AND location_id=?",
                (decision_id, self.location_id),
            ).fetchone()
            if not decision:
                raise MarginMemoryError("Decision was not found.")
            if str(decision["status"]) not in {"Pending Approval", "Pending Outcome"}:
                raise MarginMemoryError("Decision is not in a state that can be approved.")
            conn.execute(
                """UPDATE margin_memory_decisions SET status='Pending Outcome',updated_at=?
                   WHERE decision_id=?""",
                (stamp, decision_id),
            )
        self.controls.audit(
            "margin_memory.approve",
            "margin_memory_decision",
            decision_id,
            "Approved MarginMemory decision",
            after={"approved_at": stamp, "previous_status": decision["status"]},
            actor=actor,
        )
        return self.get_decision(decision_id)

    def reject_decision(self, decision_id: str, *, reason: str = "Rejected by manager", actor: Any | None = None) -> dict[str, Any]:
        self.controls.require_permission("margin_memory.approve")
        stamp = now_iso()
        with self.workspace.connect() as conn:
            decision = conn.execute(
                "SELECT * FROM margin_memory_decisions WHERE decision_id=? AND location_id=?",
                (decision_id, self.location_id),
            ).fetchone()
            if not decision:
                raise MarginMemoryError("Decision was not found.")
            if str(decision["status"]) not in {"Pending Approval", "Pending Outcome"}:
                raise MarginMemoryError("Decision is not in a state that can be rejected.")
            conn.execute(
                """UPDATE margin_memory_decisions SET status='Rejected',manager_note=COALESCE(NULLIF(manager_note,''),?),updated_at=?
                   WHERE decision_id=?""",
                (str(reason), stamp, decision_id),
            )
        self.controls.audit(
            "margin_memory.reject",
            "margin_memory_decision",
            decision_id,
            "Rejected MarginMemory decision",
            after={"rejected_at": stamp},
            actor=actor,
        )
        return self.get_decision(decision_id)

    def evaluate_pending_outcomes(self, evaluation_date: str | None = None) -> dict[str, Any]:
        evaluation_date = evaluation_date or date.today().isoformat()
        evaluated = {"evaluated": 0, "insufficient_evidence": 0, "skipped": 0, "error": ""}
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT d.* FROM margin_memory_decisions d
                   WHERE d.location_id=? AND d.status='Pending Outcome'
                     AND d.evaluation_end_date <= ?
                     AND d.source_entity_type='order_prediction'""",
                (self.location_id, evaluation_date),
            ).fetchall()
            for decision in rows:
                try:
                    prediction_id = int(decision["source_entity_id"])
                except (TypeError, ValueError):
                    evaluated["skipped"] += 1
                    continue
                actual_usage = Decimal("0")
                ending_inventory = Decimal("0")
                waste_qty = Decimal("0")
                waste_cost = Decimal("0")
                stockout_qty = Decimal("0")
                usage = conn.execute(
                    """SELECT COALESCE(SUM(ABS(quantity_delta)),0) AS usage
                       FROM inventory_adjustments
                       WHERE item_id=? AND adjustment_date BETWEEN ? AND ?
                         AND ABS(CAST(quantity_delta AS REAL)) > 0""",
                    (
                        decision["subject_id"],
                        decision["evaluation_start_date"],
                        decision["evaluation_end_date"],
                    ),
                ).fetchone()
                if usage:
                    actual_usage = dec(usage["usage"])
                try:
                    inv_ending = conn.execute(
                        """SELECT estimated_on_hand FROM inventory_estimates
                           WHERE item_id=? AND as_of_date <= ? ORDER BY as_of_date DESC LIMIT 1""",
                        (decision["subject_id"], decision["evaluation_end_date"]),
                    ).fetchone()
                    if inv_ending:
                        ending_inventory = dec(inv_ending["estimated_on_hand"])
                    else:
                        fallback = conn.execute(
                            """SELECT estimated_on_hand FROM inventory_estimates
                               WHERE item_id=? ORDER BY as_of_date DESC LIMIT 1""",
                            (decision["subject_id"],),
                        ).fetchone()
                        if fallback:
                            ending_inventory = dec(fallback["estimated_on_hand"])
                except Exception:
                    context_map = json.loads(dict(decision).get("context_json") or "{}")
                    ctx_inventory = context_map.get("current_inventory")
                    ending_inventory = dec(ctx_inventory) if ctx_inventory is not None else Decimal("0")
                try:
                    ev_start = date.fromisoformat(decision["evaluation_start_date"])
                    ev_end = date.fromisoformat(decision["evaluation_end_date"])
                    current = ending_inventory
                    movement = actual_usage
                    if movement > 0:
                        avg_daily = (movement / max((ev_end - ev_start).days, 1)).quantize(Decimal("0.0001"))
                        if avg_daily > 0:
                            current = ending_inventory + avg_daily * max((date.today() - ev_end).days, 0)
                    ending_inventory = current.quantize(Decimal("0.0001"))
                except ValueError:
                    pass
                decision_map = json.loads(decision["actual_action_json"] or "{}")
                actual_qty = dec(decision_map.get("order_quantity", 0))
                recommended_action = json.loads(decision["recommended_action_json"] or "{}")
                recommended_qty = dec(recommended_action.get("order_quantity", 0))
                purchase_unit_cost = dec(recommended_action.get("estimated_order_cost", 0)) / recommended_qty if recommended_qty else Decimal("0")
                was_manager_correct = bool(
                    actual_usage <= (recommended_qty * Decimal("1.2"))
                    and actual_usage >= (recommended_qty * Decimal("0.8"))
                )
                if was_manager_correct:
                    estimated_margin = (money((actual_qty - actual_usage) * purchase_unit_cost))
                    grade = "Beneficial Override"
                else:
                    estimated_margin = money((actual_qty - actual_usage) * purchase_unit_cost)
                    if actual_usage > recommended_qty:
                        grade = "Below Recommendation"
                    else:
                        grade = "Above Recommendation"
                if actual_usage == 0 and recommended_qty > 0:
                    grade = "Insufficient Evidence"
                explanation = {
                    "recommended_qty": str(recommended_qty),
                    "actual_qty": str(actual_qty),
                    "actual_usage": str(actual_usage),
                    "ending_inventory": str(ending_inventory),
                    "was_manager_correct": was_manager_correct,
                }
                now = now_iso()
                outcome_id = f"OUT-{uuid.uuid4().hex[:18].upper()}"
                conn.execute(
                    """INSERT INTO margin_memory_outcomes(
                           outcome_id,decision_id,evaluation_date,actual_sales,actual_usage,
                           ending_inventory,waste_quantity,waste_cost,stockout_quantity,
                           estimated_lost_sales,emergency_purchase_cost,vendor_credit_recovered,
                           transfer_cost,estimated_margin_effect,system_action_estimate,
                           manager_action_result,outcome_grade,evaluation_confidence,explanation_json,evaluated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        outcome_id, decision["decision_id"], evaluation_date, None, str(actual_usage), str(ending_inventory),
                        str(waste_qty), str(waste_cost), str(stockout_qty), "0.00", "0.00", "0.00", "0.00",
                        str(estimated_margin), str(recommended_qty), str(actual_qty), grade,
                        decision["confidence_at_decision"], json.dumps(explanation, separators=(",", ":")), now,
                    ),
                )
                status = "Evaluated" if grade != "Insufficient Evidence" else "Insufficient Evidence"
                conn.execute(
                    "UPDATE margin_memory_decisions SET status=?,updated_at=? WHERE decision_id=?",
                    (status, now, decision["decision_id"]),
                )
                evaluated["evaluated" if status == "Evaluated" else "insufficient_evidence"] += 1
        return evaluated

    def recommended_adjustments_for_item(
        self,
        item_id: str,
        as_of_date: str | None = None,
        *,
        lookback_decisions: int = 50,
    ) -> list[dict[str, Any]]:
        recommendations: list[dict[str, Any]] = []
        as_of = as_of_date or date.today().isoformat()
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT d.*,c.business_date,c.vendor_name,c.category,c.current_inventory,
                        c.inventory_days_remaining,c.forecast_sales,c.weather_code,c.temperature,
                        c.precipitation_probability,c.event_type,c.event_impact,c.open_purchase_orders,
                        o.outcome_grade,o.estimated_margin_effect,o.explanation_json
                   FROM margin_memory_decisions d
                   JOIN margin_memory_context c ON c.decision_id=d.decision_id
                   LEFT JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
                   WHERE d.location_id=? AND d.decision_type='Order Override'
                     AND d.subject_id=? AND d.status='Evaluated'
                   ORDER BY d.decision_time DESC LIMIT ?""",
                (self.location_id, item_id, max(1, min(200, lookback_decisions))),
            ).fetchall()
            if not rows:
                return []
            try:
                current = conn.execute(
                    "SELECT estimated_on_hand,average_daily_usage FROM inventory_estimates WHERE item_id=? AND as_of_date <= ? ORDER BY as_of_date DESC LIMIT 1",
                    (item_id, as_of),
                ).fetchone()
            except Exception:
                current = None
            if not current:
                fallback_inventory = None
                for row in rows:
                    try:
                        ctx = json.loads((row["context_json"] or "{}"))
                        fallback_inventory = ctx.get("current_inventory")
                        if fallback_inventory is not None:
                            break
                    except Exception:
                        continue
                current = {"estimated_on_hand": fallback_inventory or "0", "average_daily_usage": "0"} if fallback_inventory else None
            if not current:
                return []
            current_inventory = dec(current["estimated_on_hand"])
            avg_daily = dec(current["average_daily_usage"])
            for decision in rows:
                actual_json = json.loads(decision["actual_action_json"] or "{}")
                outcome = None
                if decision["outcome_grade"]:
                    outcome = {
                        "outcome_grade": decision["outcome_grade"],
                        "estimated_margin_effect": decision["estimated_margin_effect"],
                        "explanation_json": decision["explanation_json"],
                    }
                priority = Decimal("0")
                if outcome:
                    grade = str(outcome["outcome_grade"] or "")
                    if grade == "Beneficial Override":
                        priority = Decimal("1.0")
                    elif grade == "Below Recommendation":
                        priority = Decimal("0.35")
                    else:
                        priority = Decimal("0.0")
                if not decision["manager_note"]:
                    priority = priority * Decimal("0.5")
                context = json.loads(decision["context_json"] or "{}")
                if str(decision["reason_code"]) in {"WEATHER", "LOCAL_EVENT"}:
                    priority = priority * Decimal("1.15")
                recommendation_id = f"REC-{uuid.uuid4().hex[:18].upper()}"
                conn.execute(
                    """INSERT INTO margin_memory_recommendations(
                           recommendation_id,location_id,decision_type,subject_id,generated_at,
                           recommended_action_json,supporting_decision_ids_json,similarity_score,confidence,
                           estimated_value,explanation,status,accepted_at,dismissed_at,dismissal_reason)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        recommendation_id,
                        self.location_id,
                        "Order Override",
                        item_id,
                        now_iso(),
                        json.dumps(actual_json, separators=(",", ":")),
                        json.dumps([decision["decision_id"]], separators=(",", ":")),
                        f"{min(Decimal('0.99'), priority):.4f}",
                        f"{min(Decimal('0.99'), priority * Decimal('0.85')):.4f}",
                        outcome["estimated_margin_effect"] if outcome else "",
                        "Similar manager adjustment found for this item."
                        if priority > 0
                        else "Low-signal past override for this item.",
                        "Open",
                        None,
                        None,
                        None,
                    ),
                )
                recommendations.append(
                    {
                        "recommendation_id": recommendation_id,
                        "decision_id": decision["decision_id"],
                        "decision_time": decision["decision_time"],
                        "reason_code": decision["reason_code"],
                        "manager_note": decision["manager_note"],
                        "recommended_action": actual_json,
                        "confidence": f"{float(priority):.2f}",
                        "outcome_grade": outcome["outcome_grade"] if outcome else "",
                        "explanation": "Similar override found for this item.",
                    }
                )
            final = {"suggested_order_quantity": f"{current_inventory:.4f}"}
            top = recommendations[0] if recommendations else None
            conn.execute(
                """INSERT OR REPLACE INTO recommendation_cache (item_id,location_id,as_of_date,recommendation_json,generated_at)
                   VALUES(?,?,?,?,?)""",
                (item_id, self.location_id, as_of, json.dumps(json_safe(final), separators=(",", ":")), now_iso()),
            )
        return recommendations

    def capture_transfer(self, transfer_id: str, *, actor: Any | None = None) -> list[str]:
        settings = self.settings()
        if not settings.get("margin_memory_enabled", True) or not settings.get(
            "margin_memory_capture_transfers", True
        ):
            return []
        with self.workspace.connect() as conn:
            transfer = conn.execute(
                "SELECT * FROM inventory_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if not transfer:
                raise MarginMemoryError("Inventory transfer was not found.")
            lines = conn.execute(
                "SELECT * FROM inventory_transfer_lines WHERE transfer_id=? ORDER BY transfer_line_id",
                (transfer_id,),
            ).fetchall()
        decision_ids: list[str] = []
        for line in lines:
            source_key = f"inventory_transfer:{transfer_id}:{line['transfer_line_id']}"
            quantity_value = qty(line["quantity"])
            evaluation_start = str(transfer["transfer_date"])
            evaluation_end = (
                date.fromisoformat(evaluation_start) + timedelta(days=14)
            ).isoformat()
            context = {
                "business_date": evaluation_start,
                "vendor_name": line["vendor_name"],
                "product_id": line["source_item_id"],
                "category": "Inventory Transfer",
                "current_inventory": "",
                "source_location_id": transfer["source_location_id"],
                "source_location_name": transfer["source_location_name"],
                "destination_location_id": transfer["destination_location_id"],
                "destination_location_name": transfer["destination_location_name"],
                "estimated_value": transfer["estimated_value"],
                "notes": transfer["notes"] or "",
            }
            decision_ids.append(
                self._upsert_decision(
                    source_event_key=source_key,
                    decision_type="Inventory Transfer",
                    subject_type="Inventory Item",
                    subject_id=line["source_item_id"],
                    subject_name=line["item_name"],
                    recommended_action={
                        "action": "Replenish through normal purchasing",
                        "vendor_name": line["vendor_name"],
                    },
                    actual_action={
                        "action": "Transfer inventory",
                        "quantity": str(quantity_value),
                        "count_unit": line["count_unit"],
                        "source_location": transfer["source_location_name"],
                        "destination_location": transfer["destination_location_name"],
                    },
                    override_amount=str(quantity_value),
                    override_percent="",
                    reason_code="INVENTORY_BALANCE",
                    manager_note=transfer["notes"] or "",
                    decision_time=transfer["created_at"],
                    evaluation_start_date=evaluation_start,
                    evaluation_end_date=evaluation_end,
                    status="Pending Outcome",
                    confidence_at_decision="0.75",
                    source_entity_type="inventory_transfer",
                    source_entity_id=transfer_id,
                    context=context,
                    actor=actor,
                )
            )
        return decision_ids

    def capture_receiving_discrepancies(
        self,
        session_id: str,
        *,
        actor: Any | None = None,
    ) -> list[str]:
        settings = self.settings()
        if not settings.get("margin_memory_enabled", True) or not settings.get(
            "margin_memory_capture_receiving", True
        ):
            return []
        with self.workspace.connect() as conn:
            session = conn.execute(
                "SELECT * FROM receiving_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not session:
                raise MarginMemoryError("Receiving session was not found.")
            lines = conn.execute(
                """SELECT r.*,i.category FROM receiving_lines r
                   LEFT JOIN items i ON i.item_id=r.item_id
                   WHERE r.session_id=? ORDER BY r.receiving_line_id""",
                (session_id,),
            ).fetchall()
        ids: list[str] = []
        for line in lines:
            expected = qty(line["expected_quantity"])
            received = qty(line["received_quantity"])
            status = str(line["line_status"] or "Pending")
            credit = money(line["credit_expected"])
            if status == "Received" and received == expected and credit == 0:
                continue
            source_key = f"receiving:{session_id}:{line['receiving_line_id']}"
            start = str(session["received_date"] or session["invoice_date"] or date.today().isoformat())
            try:
                end = (date.fromisoformat(start) + timedelta(days=45)).isoformat()
            except ValueError:
                end = ""
            context = {
                "business_date": start,
                "vendor_name": session["vendor"],
                "product_id": line["item_id"],
                "category": line["category"] or "Receiving",
                "invoice_id": session["invoice_id"],
                "invoice_number": session["invoice_number"],
                "expected_value": session["expected_value"],
                "received_value": session["received_value"],
                "substitution_description": line["substitution_description"] or "",
            }
            ids.append(
                self._upsert_decision(
                    source_event_key=source_key,
                    decision_type="Receiving Discrepancy",
                    subject_type="Invoice Line",
                    subject_id=str(line["receiving_line_id"]),
                    subject_name=line["description"],
                    recommended_action={
                        "line_status": "Received",
                        "received_quantity": str(expected),
                        "credit_expected": "0.00",
                    },
                    actual_action={
                        "line_status": status,
                        "received_quantity": str(received),
                        "credit_expected": str(credit),
                        "substitution_description": line["substitution_description"] or "",
                    },
                    override_amount=str((received - expected).quantize(QTY)),
                    reason_code="RECEIVING_EXCEPTION",
                    manager_note=line["notes"] or session["notes"] or "",
                    decision_time=session["updated_at"] or now_iso(),
                    evaluation_start_date=start,
                    evaluation_end_date=end,
                    status="Pending Outcome",
                    confidence_at_decision="0.90",
                    source_entity_type="receiving_session",
                    source_entity_id=session_id,
                    context=context,
                    actor=actor,
                )
            )
        return ids

    def capture_invoice_correction(
        self,
        invoice_id: str,
        before: dict[str, Any] | sqlite3.Row,
        after: dict[str, Any],
        *,
        correction_source: str = "Manual Review",
        actor: Any | None = None,
    ) -> str | None:
        settings = self.settings()
        if not settings.get("margin_memory_enabled", True) or not settings.get(
            "margin_memory_capture_invoice_corrections", True
        ):
            return None
        before_map = dict(before)
        fields = ("vendor", "invoice_number", "invoice_date", "subtotal", "tax", "credits", "total")
        changes = {
            field: {"before": str(before_map.get(field) or ""), "after": str(after.get(field) or "")}
            for field in fields
            if str(before_map.get(field) or "").strip() != str(after.get(field) or "").strip()
        }
        if not changes:
            return None
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
        invoice_date = str(after.get("invoice_date") or (row["invoice_date"] if row else "") or "")
        try:
            evaluation_end = (date.fromisoformat(invoice_date) + timedelta(days=30)).isoformat()
        except ValueError:
            evaluation_end = ""
        subject_name = (
            str(after.get("invoice_number") or "")
            or str(before_map.get("invoice_number") or "")
            or invoice_id
        )
        context = {
            "business_date": invoice_date,
            "vendor_name": str(after.get("vendor") or before_map.get("vendor") or ""),
            "category": "Invoice Review",
            "changed_fields": changes,
            "correction_source": correction_source,
            "source_file": str(row["source_name"] if row else before_map.get("source_file") or ""),
        }
        return self._upsert_decision(
            source_event_key=f"invoice_correction:{invoice_id}",
            decision_type="Invoice Correction",
            subject_type="Invoice",
            subject_id=invoice_id,
            subject_name=subject_name,
            recommended_action={
                "action": "Retain original extraction for review",
                "fields": {field: value["before"] for field, value in changes.items()},
            },
            actual_action={
                "action": "Correct invoice header or totals",
                "fields": {field: value["after"] for field, value in changes.items()},
                "correction_source": correction_source,
            },
            reason_code="DOCUMENT_CORRECTION",
            manager_note=f"Corrected fields: {', '.join(changes)}",
            decision_time=now_iso(),
            evaluation_start_date=invoice_date,
            evaluation_end_date=evaluation_end,
            status="Pending Outcome",
            confidence_at_decision="1.00" if correction_source == "Manual Review" else "0.85",
            source_entity_type="invoice",
            source_entity_id=invoice_id,
            context=context,
            actor=actor,
        )

    def list_decisions(
        self,
        *,
        status: str | None = None,
        decision_type: str | None = None,
        manager: str | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        self.controls.require_permission("margin_memory.view")
        where = ["d.location_id=?"]
        params: list[Any] = [self.location_id]
        if status and status != "All":
            where.append("d.status=?")
            params.append(status)
        if decision_type and decision_type != "All":
            where.append("d.decision_type=?")
            params.append(decision_type)
        if manager and manager != "All":
            where.append("d.decision_maker=?")
            params.append(manager)
        params.append(max(1, min(5000, int(limit))))
        with self.workspace.connect() as conn:
            return conn.execute(
                f"""SELECT d.*,o.outcome_grade,o.estimated_margin_effect,
                           c.business_date,c.vendor_name,c.category,c.current_inventory,
                           c.inventory_days_remaining,c.forecast_sales,c.event_type
                    FROM margin_memory_decisions d
                    LEFT JOIN margin_memory_context c ON c.decision_id=d.decision_id
                    LEFT JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
                    WHERE {' AND '.join(where)}
                    ORDER BY d.decision_time DESC LIMIT ?""",
                tuple(params),
            ).fetchall()

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        self.controls.require_permission("margin_memory.view")
        with self.workspace.connect() as conn:
            decision = conn.execute(
                "SELECT * FROM margin_memory_decisions WHERE decision_id=? AND location_id=?",
                (decision_id, self.location_id),
            ).fetchone()
            if not decision:
                raise MarginMemoryError("MarginMemory decision was not found.")
            context = conn.execute(
                "SELECT * FROM margin_memory_context WHERE decision_id=?", (decision_id,)
            ).fetchone()
            outcome = conn.execute(
                "SELECT * FROM margin_memory_outcomes WHERE decision_id=?", (decision_id,)
            ).fetchone()
            recommendations = conn.execute(
                """SELECT * FROM margin_memory_recommendations
                   WHERE supporting_decision_ids_json LIKE ? ORDER BY generated_at DESC""",
                (f"%{decision_id}%",),
            ).fetchall()
        return {
            "decision": dict(decision),
            "context": dict(context) if context else None,
            "outcome": dict(outcome) if outcome else None,
            "recommendations": [dict(row) for row in recommendations],
        }

    def filter_options(self) -> dict[str, list[str]]:
        self.controls.require_permission("margin_memory.view")
        with self.workspace.connect() as conn:
            types = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT decision_type FROM margin_memory_decisions WHERE location_id=? ORDER BY decision_type",
                    (self.location_id,),
                ).fetchall()
            ]
            managers = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT decision_maker FROM margin_memory_decisions WHERE location_id=? ORDER BY decision_maker",
                    (self.location_id,),
                ).fetchall()
            ]
            statuses = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT status FROM margin_memory_decisions WHERE location_id=? ORDER BY status",
                    (self.location_id,),
                ).fetchall()
            ]
        return {"decision_types": types, "managers": managers, "statuses": statuses}

    def summary(self) -> dict[str, Any]:
        self.controls.require_permission("margin_memory.view")
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT status,COUNT(*) count FROM margin_memory_decisions
                   WHERE location_id=? GROUP BY status""",
                (self.location_id,),
            ).fetchall()
            types = conn.execute(
                """SELECT decision_type,COUNT(*) count FROM margin_memory_decisions
                   WHERE location_id=? GROUP BY decision_type""",
                (self.location_id,),
            ).fetchall()
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM margin_memory_decisions WHERE location_id=?",
                    (self.location_id,),
                ).fetchone()[0]
            )
        return {
            "total": total,
            "by_status": {row["status"]: int(row["count"]) for row in rows},
            "by_type": {row["decision_type"]: int(row["count"]) for row in types},
            "evaluated": sum(
                int(row["count"])
                for row in rows
                if row["status"] in {"Evaluated", "Insufficient Evidence"}
            ),
        }

    def export_decisions_csv(self, destination: Path | None = None) -> Path:
        self.controls.require_permission("margin_memory.view")
        destination = destination or (
            Path(self.workspace.folders["margin_memory"])
            / f"MarginMemory_Decisions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        rows = self.list_decisions(limit=5000)
        import csv

        headers = [
            "Decision ID", "Decision Time", "Decision Type", "Subject", "Manager",
            "Reason", "Override Amount", "Override Percent", "Status",
            "Evaluation Start", "Evaluation End", "Outcome Grade", "Estimated Margin Effect",
        ]
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([
                    row["decision_id"], row["decision_time"], row["decision_type"],
                    row["subject_name"] or row["subject_id"], row["decision_maker"],
                    REASON_LABELS.get(row["reason_code"], row["reason_code"]),
                    row["override_amount"], row["override_percent"], row["status"],
                    row["evaluation_start_date"], row["evaluation_end_date"],
                    row["outcome_grade"] or "", row["estimated_margin_effect"] or "",
                ])
        self.controls.audit(
            "margin_memory.export",
            "export",
            destination.name,
            "Exported MarginMemory decision ledger",
            details={"path": str(destination), "rows": len(rows)},
        )
        return destination


    # ------------------------------------------------------------------
    # Learning layer: pattern matching, demand prediction, smart orders
    # ------------------------------------------------------------------

    def similar_decisions(
        self,
        item_id: str,
        context: dict[str, Any],
        *,
        lookback_decisions: int = 50,
    ) -> list[dict[str, Any]]:
        """Find past decisions for *item_id* in similar operating contexts.

        Contexts are compared on weekday, weather (fuzzy), and event type
        so that a manager override from a rainy Tuesday in summer can be
        recommended for a similar upcoming day.
        """
        weekday = context.get("weekday")
        weather = str(context.get("weather_code", "")).strip()
        event_type = str(context.get("event_type", "")).strip()
        with self.workspace.connect() as conn:
            sql = """SELECT d.*,c.business_date,c.weekday,c.weather_code,c.temperature,
                            c.precipitation_probability,c.event_type,c.event_impact,
                            c.forecast_sales,c.average_daily_sales,c.category,
                            o.outcome_grade,o.estimated_margin_effect
                       FROM margin_memory_decisions d
                       JOIN margin_memory_context c ON c.decision_id=d.decision_id
                       LEFT JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
                      WHERE d.location_id=? AND d.decision_type='Order Override'
                        AND d.subject_id=? AND d.status='Evaluated'
                        AND o.outcome_grade='Beneficial Override'"""
            params: list[Any] = [self.location_id, str(item_id)]
            conditions: list[str] = []
            if weekday is not None:
                conditions.append("c.weekday=?")
                params.append(int(weekday))
            if weather:
                conditions.append("c.weather_code=?")
                params.append(weather)
            if event_type:
                conditions.append("c.event_type LIKE ?")
                params.append(f"%{event_type}%")
            if conditions:
                sql += " AND (" + " AND ".join(conditions) + ")"
            sql += " ORDER BY d.decision_time DESC"
            rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            actual = json.loads(r["actual_action_json"] or "{}")
            results.append({
                "decision_id": r["decision_id"],
                "decision_time": r["decision_time"],
                "actual_order_quantity": actual.get("order_quantity", ""),
                "recommended_quantity": json.loads(r["recommended_action_json"])["order_quantity"],
                "override_percent": r["override_percent"],
                "reason_code": r["reason_code"],
                "manager_note": r["manager_note"] or "",
                "weekday": r["weekday"],
                "weather_code": r["weather_code"] or "",
                "event_type": r["event_type"] or "",
                "business_date": r["business_date"],
                "outcome_grade": r["outcome_grade"],
                "margin_effect": r["estimated_margin_effect"],
            })
        return results

    def predict_adjusted_order_quantity(
        self,
        item_id: str,
        base_quantity: Decimal,
        context: dict[str, Any],
        *,
        lookback_decisions: int = 50,
    ) -> tuple[Decimal, Decimal, str]:
        """Predict a better order quantity using learned manager overrides.

        Looks for similar past decisions for this item in matching contexts.
        If a consistent pattern of manager adjustments exists (e.g. always
        ordering 20% less than suggested on rainy Fridays), the system applies
        that learned adjustment to the base prediction.

        Returns ``(adjusted_quantity, confidence_0_1, explanation)``.
        """
        similar = self.similar_decisions(item_id, context, lookback_decisions=lookback_decisions)
        if not similar:
            return base_quantity, Decimal("0.00"), "No similar historical decisions found"

        # Compute the average override factor from beneficial decisions
        factors: list[Decimal] = []
        for dec_row in similar:
            rec_qty = dec(dec_row["recommended_quantity"])
            act_qty = dec(dec_row["actual_order_quantity"])
            if rec_qty > 0 and act_qty > 0:
                factors.append((act_qty / rec_qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        if not factors:
            return base_quantity, Decimal("0.00"), "Similar decisions found but no usable ratio data"

        avg_factor = (sum(factors) / len(factors)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        adjusted = (base_quantity * avg_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Confidence based on number of similar decisions
        if len(factors) >= 5:
            confidence = Decimal("0.85")
        elif len(factors) >= 3:
            confidence = Decimal("0.65")
        elif len(factors) >= 1:
            confidence = Decimal("0.40")
        else:
            confidence = Decimal("0.00")

        explanation = (
            f"Adjusted from {base_quantity} based on {len(factors)} similar historical "
            f"decision(s). Average manager override factor: {avg_factor:.4f}. "
            f"Weather: {similar[0]['weather_code'] or 'N/A'}, "
            f"Weekday: {similar[0]['weekday']}, "
            f"Event: {similar[0]['event_type'] or 'none'}"
        )
        return adjusted, confidence, explanation

    def predict_demand(
        self,
        item_id: str,
        date_str: str,
        *,
        lookback_days: int = 60,
    ) -> dict[str, Any]:
        """Predict demand for an item on a given date using historical patterns.

        Uses past sales + decision context (weekday, weather, events) to
        produce a demand forecast with confidence.
        """
        prediction_date = date.fromisoformat(date_str)
        weekday = prediction_date.weekday()
        with self.workspace.connect() as conn:
            # Historical sales for this weekday
            sales_rows = conn.execute(
                """SELECT business_date, weekday, forecast_sales, average_daily_sales
                   FROM margin_memory_context
                   WHERE product_id=? AND weekday=?
                   AND business_date IS NOT NULL
                   ORDER BY business_date DESC LIMIT ?""",
                (str(item_id), weekday, lookback_days),
            ).fetchall()

            # Sales by weekday
            weekday_sales = conn.execute(
                """SELECT AVG(CAST(average_daily_sales AS REAL)) as avg_sales
                   FROM margin_memory_context
                   WHERE product_id=? AND weekday=? AND average_daily_sales IS NOT NULL
                     AND CAST(average_daily_sales AS REAL) > 0""",
                (str(item_id), weekday),
            ).fetchone()

            # Overall average
            overall_avg = conn.execute(
                """SELECT AVG(CAST(average_daily_sales AS REAL)) as avg_sales
                   FROM margin_memory_context
                   WHERE product_id=? AND average_daily_sales IS NOT NULL
                     AND CAST(average_daily_sales AS REAL) > 0""",
                (str(item_id),),
            ).fetchone()

        weekday_avg = dec(weekday_sales["avg_sales"]) if weekday_sales and weekday_sales["avg_sales"] else Decimal("0")
        overall = dec(overall_avg["avg_sales"]) if overall_avg and overall_avg["avg_sales"] else Decimal("0")
        sample_count = len(sales_rows)

        if weekday_avg > 0:
            prediction = weekday_avg
            confidence = Decimal("0.75") if sample_count >= 5 else Decimal("0.50") if sample_count >= 3 else Decimal("0.30")
            method = f"Weekday average ({prediction_date.strftime('%A')}) from {sample_count} historical sample(s)"
        elif overall > 0:
            prediction = overall
            confidence = Decimal("0.40")
            method = f"Overall average from {sample_count} historical records"
        else:
            prediction = Decimal("0")
            confidence = Decimal("0.00")
            method = "No historical data for this item"

        return {
            "item_id": str(item_id),
            "predicted_date": date_str,
            "predicted_demand": f"{prediction:.4f}",
            "confidence": f"{confidence:.2f}",
            "method": method,
            "weekday_avg": f"{weekday_avg:.4f}",
            "overall_avg": f"{overall:.4f}",
            "sample_count": sample_count,
        }

    def learn_operational_factors(self, *, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        """Learn sales effects from weekday, weather and event/holiday history.

        Factors are descriptive correlations, not causal claims. They require at
        least three observations and expose sample count/confidence to managers.
        """
        end = date.fromisoformat(end_date) if end_date else date.today()
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=730)
        with self.workspace.connect() as conn:
            sales = conn.execute("SELECT period_start, net_sales FROM sales WHERE period_start BETWEEN ? AND ? ORDER BY period_start", (start.isoformat(), end.isoformat())).fetchall()
            weather = {r["weather_date"]: r for r in conn.execute("SELECT * FROM weather_daily WHERE weather_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchall()}
            events = conn.execute("SELECT event_date,end_date,event_name,category,expected_sales_impact_percent FROM local_events WHERE end_date>=? AND event_date<=?", (start.isoformat(), end.isoformat())).fetchall()
        by_date = {str(r["period_start"]): dec(r["net_sales"]) for r in sales if dec(r["net_sales"]) > 0}
        weekdays = {i: [v for k,v in by_date.items() if date.fromisoformat(k).weekday()==i] for i in range(7)}
        facts=[]
        for i,vals in weekdays.items():
            if len(vals)>=3: facts.append(("weekday",str(i),vals,[],sum(vals)/Decimal(len(vals))))
        # Weather: compare each rainy day with the dry-day average for the same weekday.
        rain_ratios=[]
        for k,v in by_date.items():
            w=weather.get(k)
            if w and dec(w["precipitation_probability"])>=50:
                wd=date.fromisoformat(k).weekday()
                baseline=[x for dk,x in by_date.items() if date.fromisoformat(dk).weekday()==wd and (dk not in weather or dec(weather[dk]["precipitation_probability"])<50)]
                if baseline: rain_ratios.append((v/(sum(baseline)/Decimal(len(baseline))),v))
        if len(rain_ratios)>=3:
            facts.append(("weather","rain",[x[1] for x in rain_ratios],[],sum(x[0] for x in rain_ratios)/Decimal(len(rain_ratios))))
        # Events/holidays: group by category and compare event dates to non-event dates on the same weekday.
        by_category={}
        for e in events:
            cat=str(e["category"] or e["event_name"] or "Event")
            by_category.setdefault(cat, set()).update(
                k for k in by_date if str(e["event_date"])<=k<=str(e["end_date"])
            )
        for cat,days in by_category.items():
            ratios=[]; observed=[]
            for k in days:
                if k not in by_date: continue
                wd=date.fromisoformat(k).weekday()
                baseline=[v for dk,v in by_date.items() if dk not in days and date.fromisoformat(dk).weekday()==wd]
                if baseline:
                    observed.append(by_date[k]); ratios.append(by_date[k]/(sum(baseline)/Decimal(len(baseline))))
            if len(ratios)>=3:
                facts.append(("event",cat,observed,[],sum(ratios)/Decimal(len(ratios))))
        learned=0
        with self.workspace.connect() as conn:
            for typ,key,observed,comparison,observed_avg in facts:
                baseline_vals=[]
                if typ=='weekday': baseline_vals=[v for i,vals in weekdays.items() if str(i)!=key for v in vals]
                elif typ=='weather': baseline_vals=dry
                else:
                    baseline_vals=[v for v in by_date.values() if v>0]
                if not baseline_vals: continue
                baseline=sum(baseline_vals)/Decimal(len(baseline_vals)); mult=(observed_avg/baseline) if baseline else Decimal('1')
                conf=min(Decimal('1'),Decimal(len(observed))/Decimal('12'))
                fid=f"FAC-{hashlib.sha256(f'{self.location_id}:{typ}:{key}'.encode()).hexdigest()[:18].upper()}"
                explanation=f"Observed {len(observed)} sample(s); observed average ${observed_avg:,.2f} vs baseline ${baseline:,.2f}."
                conn.execute("""INSERT INTO margin_memory_sales_factors(factor_id,location_id,factor_type,factor_key,sample_count,baseline_sales,observed_sales,multiplier,confidence,last_observed_date,explanation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(location_id,factor_type,factor_key) DO UPDATE SET sample_count=excluded.sample_count,baseline_sales=excluded.baseline_sales,observed_sales=excluded.observed_sales,multiplier=excluded.multiplier,confidence=excluded.confidence,last_observed_date=excluded.last_observed_date,explanation=excluded.explanation,updated_at=excluded.updated_at""",(fid,self.location_id,typ,key,len(observed),f'{baseline:.2f}',f'{observed_avg:.2f}',f'{mult:.4f}',f'{conf:.4f}',max(by_date.keys()) if by_date else None,explanation,now_iso()))
                learned+=1
        return {"learned":learned,"factors":[{"type":t,"key":k,"samples":len(o)} for t,k,o,_,_ in facts]}

    def learn_from_outcomes(self, *, evaluation_date: str | None = None) -> dict[str, Any]:
        """Run the full learning cycle:

        1. Evaluate pending outcomes (from ``evaluate_pending_outcomes``)
        2. Extract patterns from beneficial overrides
        3. Store learned patterns for future recommendation generation
        4. Generate updated recommendations

        Returns a summary of what was learned.
        """
        evaluation_date = evaluation_date or date.today().isoformat()

        # Step 1: Evaluate pending outcomes
        eval_result = self.evaluate_pending_outcomes(evaluation_date)
        factor_result = self.learn_operational_factors(end_date=evaluation_date)

        # Step 2: Find the most impactful learned patterns
        with self.workspace.connect() as conn:
            # Beneficial overrides by reason code
            reason_patterns = conn.execute(
                """SELECT reason_code, COUNT(*) as count, AVG(CAST(override_percent AS REAL)) as avg_override
                   FROM margin_memory_decisions d
                   JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
                   WHERE d.location_id=? AND o.outcome_grade='Beneficial Override'
                   GROUP BY reason_code ORDER BY count DESC LIMIT 10""",
                (self.location_id,),
            ).fetchall()

            # Beneficial overrides by weekday
            weekday_patterns = conn.execute(
                """SELECT c.weekday, COUNT(*) as count, AVG(CAST(o.estimated_margin_effect AS REAL)) as avg_margin
                   FROM margin_memory_decisions d
                   JOIN margin_memory_context c ON c.decision_id=d.decision_id
                   JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
                   WHERE d.location_id=? AND o.outcome_grade='Beneficial Override'
                   GROUP BY c.weekday ORDER BY count DESC LIMIT 10""",
                (self.location_id,),
            ).fetchall()

            # Items with most beneficial overrides
            item_patterns = conn.execute(
                """SELECT d.subject_id, d.subject_name, COUNT(*) as count, AVG(CAST(d.override_percent AS REAL)) as avg_override_pct
                   FROM margin_memory_decisions d
                   JOIN margin_memory_outcomes o ON o.decision_id=d.decision_id
                   WHERE d.location_id=? AND o.outcome_grade='Beneficial Override'
                   GROUP BY d.subject_id ORDER BY count DESC LIMIT 10""",
                (self.location_id,),
            ).fetchall()

        patterns = {
            "by_reason_code": [
                {"reason": r["reason_code"], "count": int(r["count"]), "avg_override_pct": dec(r["avg_override"])}
                for r in reason_patterns
            ],
            "by_weekday": [
                {"weekday": r["weekday"], "count": int(r["count"]), "avg_margin": dec(r["avg_margin"])}
                for r in weekday_patterns
            ],
            "by_item": [
                {"item_id": r["subject_id"], "item_name": r["subject_name"], "count": int(r["count"]),
                 "avg_override_pct": dec(r["avg_override_pct"])}
                for r in item_patterns
            ],
        }

        self.controls.audit(
            "margin_memory.learning",
            "learning_cycle",
            evaluation_date,
            "Completed MarginMemory learning cycle",
            details={
                "evaluated": eval_result.get("evaluated", 0),
                "patterns_found": len(patterns["by_reason_code"]),
                "items_with_patterns": len(patterns["by_item"]),
            },
        )

        return {
            "evaluation_result": eval_result,
            "operational_factors": factor_result,
            "learned_patterns": patterns,
            "summary": (
                f"Evaluated {eval_result.get('evaluated', 0)} decision(s). "
                f"Found {len(patterns['by_reason_code'])} reason-code patterns, "
                f"{len(patterns['by_weekday'])} weekday patterns, "
                f"{len(patterns['by_item'])} item patterns."
            ),
        }

    def generate_smart_order_predictions(
        self,
        as_of_date: str,
        *,
        lead_time_days: int = 7,
        order_cycle_days: int = 7,
        lookback_decisions: int = 50,
    ) -> dict[str, Any]:
        """Generate order predictions enhanced by learned patterns.

        For each item with inventory data, predicts demand using
        ``predict_demand`` and adjusts the base prediction using
        ``predict_adjusted_order_quantity`` based on learned overrides.

        Returns a summary of predictions by item.
        """
        prediction_end = (date.fromisoformat(as_of_date) + timedelta(days=lead_time_days + order_cycle_days)).isoformat()

        # Get items with active predictions
        with self.workspace.connect() as conn:
            items = conn.execute(
                """SELECT DISTINCT p.item_id, i.item_name,
                          p.suggested_order_quantity, p.lead_time_days,
                          p.order_cycle_days
                   FROM order_predictions p
                   JOIN order_batches b ON b.batch_id=p.batch_id
                   LEFT JOIN items i ON i.item_id=p.item_id
                   WHERE b.as_of_date=?""",
                (as_of_date,),
            ).fetchall()

            # Get context for this date
            business_date = as_of_date
            weekday = date.fromisoformat(as_of_date).weekday()
            forecast = conn.execute(
                "SELECT predicted_net_sales FROM demand_forecasts WHERE forecast_date=? ORDER BY created_at DESC LIMIT 1",
                (business_date,),
            ).fetchone()

            weather = conn.execute(
                "SELECT weather_code, temperature_max_f, precipitation_probability FROM weather_daily WHERE weather_date=?",
                (business_date,),
            ).fetchone()

            events = conn.execute(
                """SELECT event_name, category, expected_sales_impact_percent
                   FROM local_events WHERE event_date<=? AND end_date>=?
                   ORDER BY ABS(CAST(expected_sales_impact_percent AS REAL)) DESC LIMIT 5""",
                (business_date, business_date),
            ).fetchall()

        context = {
            "business_date": business_date,
            "weekday": weekday,
            "weather_code": str(weather["weather_code"]) if weather and weather["weather_code"] else "",
            "temperature": str(weather["temperature_max_f"]) if weather and weather["temperature_max_f"] else "",
            "precipitation_probability": str(weather["precipitation_probability"]) if weather and weather["precipitation_probability"] else "",
            "event_type": ", ".join(str(e["category"] or "") for e in events) if events else "",
            "event_impact": ", ".join(str(e["expected_sales_impact_percent"]) for e in events) if events else "",
            "forecast_sales": str(forecast["predicted_net_sales"]) if forecast else "",
        }

        predictions = []
        for item in items:
            item_id = str(item["item_id"])
            base_qty = qty(item["suggested_order_quantity"])

            # Predict demand for the lead time period
            demand_pred = self.predict_demand(item_id, prediction_end)

            # Adjust the base order quantity using learned patterns
            adjusted_qty, confidence, explanation = self.predict_adjusted_order_quantity(
                item_id, base_qty, context, lookback_decisions=lookback_decisions
            )

            # Blend: if confidence > 0.5, use learned adjustment; otherwise keep base
            if confidence >= Decimal("0.50"):
                final_qty = adjusted_qty
                source = "learned"
            else:
                final_qty = base_qty
                source = "base_prediction"

            # Update the recommendation cache
            with self.workspace.connect() as conn:
                rec_json = json.dumps(json_safe({
                    "item_id": item_id,
                    "item_name": item["item_name"],
                    "suggested_order_quantity": f"{base_qty:.4f}",
                    "learned_adjusted_quantity": f"{adjusted_qty:.4f}",
                    "final_quantity": f"{final_qty:.4f}",
                    "adjustment_confidence": f"{confidence:.2f}",
                    "demand_prediction": demand_pred,
                    "explanation": explanation,
                }), separators=(",", ":"))
                conn.execute(
                    "INSERT OR REPLACE INTO recommendation_cache (item_id,location_id,as_of_date,recommendation_json,generated_at) VALUES(?,?,?,?,?)",
                    (item_id, self.location_id, as_of_date, rec_json, now_iso()),
                )

            predictions.append({
                "item_id": item_id,
                "item_name": item["item_name"] or "",
                "base_quantity": f"{base_qty:.4f}",
                "adjusted_quantity": f"{adjusted_qty:.4f}",
                "final_quantity": f"{final_qty:.4f}",
                "confidence": f"{confidence:.2f}",
                "source": source,
                "demand_forecast": demand_pred["predicted_demand"],
                "explanation": explanation,
            })

        return {
            "as_of_date": as_of_date,
            "prediction_end": prediction_end,
            "items_predicted": len(predictions),
            "predictions": predictions,
        }
