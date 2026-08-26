#!/usr/bin/env python3
"""Read-only, data-grounded manager chat for MarginMise.

The service never gives the model direct write access to the restaurant ledger.
It builds a bounded, restaurant-specific context packet from SQLite and current
GUI state, then asks the local CostPilot LLM to explain that data. Consequential
actions remain inside the normal GUI workflows where a manager must confirm them.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from inventory_planning import preferred_sales_rows

# CostPilot uses the local LLM runtime (llama.cpp) for AI assistance.
# No cloud AI providers or external services are required.
from local_ai import (
    MODEL_ID as LOCAL_COSTPILOT_MODEL,
    LocalAIError,
    generate_json as generate_local_json,
    status as local_ai_status,
)

CHAT_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS manager_chat_sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_chat_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES manager_chat_sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    context_path TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manager_chat_messages_session
    ON manager_chat_messages(session_id, message_id);
"""

DEFAULT_FREE_PROVIDER = "local"
DEFAULT_FREE_MODEL = LOCAL_COSTPILOT_MODEL

NAVIGATION_TARGETS = (
    "",
    "overview",
    "invoice_intake",
    "costpilot_review",
    "auto_upload_history",
    "notifications",
    "receiving",
    "items_prices",
    "inventory_counts",
    "order_planning",
    "reports",
    "operations",
    "intelligence",
    "settings",
    "security",
)


def is_free_model(model: str | None) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized == LOCAL_COSTPILOT_MODEL.lower()


class ManagerChatError(RuntimeError):
    pass


@dataclass
class ChatAnswer:
    answer: str
    session_id: str
    context_path: str
    provider: str
    model: str
    used_local_fallback: bool = False
    sources: list[dict[str, Any]] | None = None
    navigation: dict[str, str] | None = None
    validation_notes: list[str] | None = None


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, sqlite3.Row):
        return {key: json_safe(value[key]) for key in value.keys()}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text or "")
    return text.replace("\r", "").strip()


class ManagerChatService:
    def __init__(
        self,
        workspace: Any,
        pipeline: Any,
        gui_state_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.workspace = workspace
        self.pipeline = pipeline
        self.gui_state_provider = gui_state_provider or (lambda: {})
        self.chat_dir = workspace.root / "Manager Chat"
        self.context_dir = self.chat_dir / "Context Snapshots"
        self.log_dir = workspace.folders.get("logs", workspace.root / "Logs")
        self.chat_dir.mkdir(parents=True, exist_ok=True)
        self.context_dir.mkdir(parents=True, exist_ok=True)
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        with workspace.connect() as conn:
            conn.executescript(CHAT_SCHEMA_SQL)

    # ---------- persistence ----------
    def new_session(self, title: str = "Manager conversation") -> str:
        session_id = f"CHAT-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        stamp = now_iso()
        with self.workspace.connect() as conn:
            conn.execute(
                "INSERT INTO manager_chat_sessions(session_id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (session_id, title[:120], stamp, stamp),
            )
        return session_id

    def ensure_session(self, session_id: str | None = None) -> str:
        if session_id:
            with self.workspace.connect() as conn:
                row = conn.execute(
                    "SELECT session_id FROM manager_chat_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
            if row:
                return session_id
        return self.new_session()

    def save_message(self, session_id: str, role: str, content: str, context_path: str = "") -> None:
        stamp = now_iso()
        with self.workspace.connect() as conn:
            conn.execute(
                "INSERT INTO manager_chat_messages(session_id,role,content,context_path,created_at) VALUES(?,?,?,?,?)",
                (session_id, role, content, context_path, stamp),
            )
            conn.execute(
                "UPDATE manager_chat_sessions SET updated_at=? WHERE session_id=?", (stamp, session_id)
            )

    def history(self, session_id: str, limit: int = 12) -> list[dict[str, str]]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT role,content,created_at FROM manager_chat_messages
                   WHERE session_id=? ORDER BY message_id DESC LIMIT ?""",
                (session_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_session(self, session_id: str) -> None:
        with self.workspace.connect() as conn:
            conn.execute("DELETE FROM manager_chat_sessions WHERE session_id=?", (session_id,))

    # ---------- context routing ----------
    @staticmethod
    def _tokens(question: str) -> list[str]:
        stop = {
            "what", "which", "when", "where", "why", "how", "much", "many", "this", "that",
            "have", "with", "from", "about", "should", "could", "would", "need", "show", "tell",
            "restaurant", "manager", "please", "week", "month", "year", "today", "current",
        }
        return [word for word in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", question.lower()) if word not in stop]

    @staticmethod
    def _intents(question: str) -> set[str]:
        q = question.lower()
        intents = {"overview"}
        groups = {
            "orders": ("order", "par", "reorder", "buy", "delivery", "short", "run out"),
            "inventory": ("inventory", "stock", "on hand", "count", "remaining", "have left"),
            "pricing": ("price", "increase", "decrease", "cost change", "expensive", "cheaper"),
            "sales": ("sales", "revenue", "transaction", "ticket", "pace", "sold", "sell", "selling"),
            "profit": ("profit", "margin", "cogs", "contribution", "performance"),
            "labor": ("labor", "payroll", "wage", "wages", "hours worked"),
            "invoices": ("invoice", "vendor", "purchase", "spend", "delivery"),
            "reviews": (
                "review", "error", "failed", "problem", "missing", "unrecognized",
                "exception", "attention", "discrepancy", "duplicate",
            ),
            "usage": ("usage", "used", "consumption", "depletion", "waste"),
            "annual": ("annual", "year", "yearly", "12 month", "twelve month"),
            "recipes": ("recipe", "menu cost", "food cost", "ingredient", "menu item", "theoretical"),
            "waste_log": ("waste", "spoil", "spoiled", "variance", "shrink"),
            "pos": ("pos", "product mix", "item sales", "units sold", "menu sales"),
            "purchase_orders": ("purchase order", "vendor po", "po draft"),
            "mobile_counts": ("mobile count", "phone count", "submitted count"),
            "accounting": ("accounting", "journal", "quickbooks", "bookkeeping", "export"),
            "portfolio": ("location", "locations", "portfolio", "store comparison", "multi location", "group"),
            "transfers": ("transfer", "move inventory", "send stock", "receive stock"),
            "forecasting": ("forecast", "weather", "event", "projected sales", "demand"),
            "distributors": ("distributor", "catalog", "confirmation", "supplier integration"),
            "profitability": ("menu profitability", "true food cost", "recommended price", "pricing decision"),
            "savings": ("savings", "value delivered", "time saved", "return on investment"),
            "margin_memory": ("marginmemory", "margin memory", "decision memory", "manager decision", "past decision", "decision outcome", "manager override", "manager choice", "decision was", "decision", "manager made"),
            "shift_reports": ("shift report", "shift log", "shift summary", "daily shift", "shift review", "manager shift", "server report", "cashier report", "how did the shift run", "shift ran", "shift performance", "shift summary", "shift data"),
            "auto_upload": (
                "auto upload", "automatic upload", "upload folder", "spreadsheet",
                "workbook", "excel file", "file import", "stuck in approval",
            ),
        }
        for name, words in groups.items():
            if any(word in q for word in words):
                intents.add(name)
        return intents

    def _query_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.workspace.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _matching_items(self, question: str, limit: int) -> list[dict[str, Any]]:
        tokens = self._tokens(question)[:8]
        if not tokens:
            return []
        clauses, params = [], []
        for token in tokens:
            like = f"%{token}%"
            clauses.append("(LOWER(item_name) LIKE ? OR LOWER(COALESCE(vendor_sku,'')) LIKE ? OR LOWER(vendor_name) LIKE ? OR LOWER(category) LIKE ?)")
            params.extend([like, like, like, like])
        sql = f"""SELECT * FROM items WHERE {' OR '.join(clauses)}
                  ORDER BY last_purchase_date DESC, item_name LIMIT ?"""
        params.append(limit)
        return self._query_rows(sql, tuple(params))

    def build_context(self, question: str, *, max_items: int = 120) -> dict[str, Any]:
        settings = self.workspace.load_settings()
        gui_state = json_safe(self.gui_state_provider() or {})
        intents = self._intents(question)
        current_year = int(gui_state.get("selected_year") or date.today().year)
        selected_month = str(gui_state.get("selected_month") or date.today().strftime("%Y-%m"))
        summary = json_safe(self.pipeline.dashboard_summary())
        context: dict[str, Any] = {
            "context_version": "3.0",
            "generated_at": now_iso(),
            "restaurant": {
                "name": settings.get("restaurant_name", "Restaurant"),
                "currency": settings.get("currency", "USD"),
                "timezone": settings.get("timezone", "America/Chicago"),
            },
            "gui_state": gui_state,
            "question_intents": sorted(intents),
            "dashboard_summary": summary,
            "data_notes": [
                "Purchases are not inventory-adjusted COGS until a physical count closes the month.",
                "Estimated on-hand quantities are forecasts and must be checked by a manager.",
                "Labor is excluded unless it was imported into operating costs.",
                "Actual depletion includes sales, logged waste, unlogged waste, spoilage, theft, and count variance. Logged waste is tracked separately when entered.",
                "Order quantities are drafts only and require manager approval.",
            ],
        }

        table_names = [
            "invoices", "invoice_lines", "items", "price_history", "reviews", "sales",
            "operating_costs", "inventory_counts", "monthly_item_usage", "monthly_closes",
            "order_batches", "order_predictions", "operational_exceptions",
            "receiving_sessions", "receiving_lines", "audit_log", "backup_history",
            "data_quality_snapshots", "pos_import_runs", "pos_sales_lines", "menu_items",
            "recipe_ingredients", "waste_events", "mobile_count_sessions", "mobile_count_entries",
            "purchase_orders", "purchase_order_lines", "accounting_export_history",
            "inventory_transfers", "inventory_transfer_lines", "inventory_adjustments", "local_events",
            "weather_daily", "demand_forecasts", "forecast_learning", "distributor_profiles",
            "distributor_catalog", "distributor_exchanges", "savings_events", "owner_report_history",
            "costpilot_review_resolutions", "costpilot_review_actions",
            "auto_upload_events",
        ]
        counts: dict[str, int] = {}
        with self.workspace.connect() as conn:
            for table in table_names:
                try:
                    counts[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                except sqlite3.Error:
                    counts[table] = 0
        context["database_record_counts"] = counts

        # The 12-month block is compact enough to include for every management question.
        try:
            context["annual_summary"] = json_safe(self.pipeline.annual_summary(current_year))
            context["annual_totals"] = json_safe(self.pipeline.planning.year_totals(current_year))
            context["selected_month_summary"] = json_safe(self.pipeline.planning.month_summary(selected_month))
        except Exception as exc:
            context["annual_context_error"] = str(exc)

        if "orders" in intents or "inventory" in intents or "overview" in intents:
            try:
                estimates = self.pipeline.estimated_inventory()
                estimates = sorted(
                    estimates,
                    key=lambda row: (float(row.get("estimated_on_hand", 0)), -float(row.get("average_weekly_usage", 0))),
                )[:max_items]
                context["inventory_estimates"] = json_safe(estimates)
                latest = self.pipeline.planning.latest_order_batch()
                context["latest_order_batch"] = json_safe(dict(latest)) if latest else None
                context["latest_order_predictions"] = json_safe(
                    [dict(row) for row in self.pipeline.planning.list_order_predictions(latest["batch_id"] if latest else None)][:max_items]
                )
            except Exception as exc:
                context["inventory_context_error"] = str(exc)

        if "pricing" in intents or "invoices" in intents or "overview" in intents:
            context["recent_invoices"] = self._query_rows(
                """SELECT invoice_id,invoice_date,vendor,invoice_number,subtotal,fees,tax,credits,total,status,
                          extraction_method,extraction_confidence
                   FROM invoices ORDER BY COALESCE(invoice_date,created_at) DESC LIMIT 60"""
            )
            context["price_alerts"] = self._query_rows(
                """SELECT price_id,invoice_id,invoice_date,vendor_name,vendor_sku,item_id,item_description,unit_price,
                          previous_price,price_change_percent,line_total
                   FROM price_history WHERE price_alert=1
                   ORDER BY ABS(CAST(price_change_percent AS REAL)) DESC, invoice_date DESC LIMIT 80"""
            )
            context["vendor_spend"] = self._query_rows(
                """SELECT vendor,COUNT(*) AS invoice_count,
                          ROUND(SUM(CASE WHEN status='Approved' THEN CAST(total AS REAL) ELSE 0 END),2) AS approved_spend
                   FROM invoices GROUP BY vendor ORDER BY approved_spend DESC LIMIT 50"""
            )

        if "reviews" in intents or "overview" in intents:
            context["open_reviews"] = self._query_rows(
                """SELECT r.review_id,r.invoice_id,r.severity,r.issue_type,r.issue,r.created_at,i.vendor,i.invoice_number,
                          i.invoice_date,i.total,i.extraction_method
                   FROM reviews r JOIN invoices i ON i.invoice_id=r.invoice_id
                   WHERE r.status='Open' ORDER BY r.created_at DESC LIMIT 80"""
            )

            try:
                context["costpilot_review_summary"] = json_safe(self.pipeline.costpilot_review_summary())
                context["costpilot_review_cases"] = json_safe(self.pipeline.list_costpilot_review_cases())[:max_items]
            except Exception as review_exc:
                context["costpilot_review_context_error"] = str(review_exc)

        if "auto_upload" in intents or "reviews" in intents or "overview" in intents:
            try:
                upload_rows = self._query_rows(
                    """SELECT event.*
                       FROM auto_upload_events AS event
                       WHERE event.status IN ('Needs Review','Failed')
                         AND event.event_id = (
                             SELECT MAX(newer.event_id)
                             FROM auto_upload_events AS newer
                             WHERE newer.source_sha256=event.source_sha256
                         )
                       ORDER BY event.completed_at DESC,event.event_id DESC LIMIT 80"""
                )
                upload_events = []
                for row in upload_rows:
                    try:
                        details = json.loads(str(row.get("details_json") or "{}"))
                    except Exception:
                        details = {}
                    outcome = details.get("outcome") if isinstance(details, dict) else {}
                    outcome = outcome if isinstance(outcome, dict) else {}
                    detail = outcome.get("details")
                    detail = detail if isinstance(detail, dict) else {}
                    errors = detail.get("errors")
                    if not isinstance(errors, list):
                        errors = [detail.get("error")] if detail.get("error") else []
                    upload_events.append({
                        "event_id": int(row["event_id"]),
                        "original_name": str(row.get("original_name") or ""),
                        "detected_type": str(row.get("detected_type") or ""),
                        "classification_confidence": float(row.get("classification_confidence") or 0),
                        "status": str(row.get("status") or ""),
                        "summary": str(row.get("summary") or ""),
                        "errors": [str(error) for error in errors if error][:100],
                        "imported": int(outcome.get("imported") or 0),
                        "rejected": int(outcome.get("rejected") or 0),
                        "completed_at": str(row.get("completed_at") or ""),
                    })
                context["auto_upload_review_events"] = upload_events
            except sqlite3.Error:
                context["auto_upload_review_events"] = []

        if "sales" in intents or "profit" in intents or "overview" in intents:
            with self.workspace.connect() as conn:
                sales_rows = preferred_sales_rows(conn)
            context["recent_sales_periods"] = [
                {
                    key: row[key]
                    for key in (
                        "sales_id", "period_start", "period_end", "gross_sales",
                        "discounts", "refunds", "sales_tax", "net_sales", "source_file",
                    )
                }
                for row in reversed(sales_rows[-60:])
            ]
            context["recent_operating_costs"] = self._query_rows(
                """SELECT cost_id,cost_date,category,description,amount FROM operating_costs
                   ORDER BY cost_date DESC,cost_id DESC LIMIT 80"""
            )

            # Add item-level POS sales data for "what did we sell most" questions
            if "pos" in intents or "sales" in intents:
                try:
                    context["pos_item_sales_summary"] = self._query_rows(
                        """SELECT menu_item_name,
                                  COUNT(*) AS transaction_count,
                                  SUM(CAST(quantity AS REAL)) AS total_quantity,
                                  ROUND(SUM(CAST(net_sales AS REAL)), 2) AS total_net_sales
                           FROM pos_sales_lines
                           WHERE business_date >= date('now', '-60 days')
                           GROUP BY menu_item_name
                           ORDER BY total_quantity DESC, total_net_sales DESC
                           LIMIT 50"""
                    )
                except Exception:
                    pass

        if "usage" in intents or "inventory" in intents or "orders" in intents:
            context["monthly_item_usage"] = self._query_rows(
                """SELECT month,item_id,item_name,vendor_name,vendor_sku,count_unit,opening_quantity,
                          purchased_quantity,ending_quantity,estimated_usage_quantity,average_daily_usage,
                          average_weekly_usage,usage_per_1000_sales,estimated_usage_cost,confidence
                   FROM monthly_item_usage ORDER BY month DESC,item_name LIMIT ?""",
                (max_items * 2,),
            )

        try:
            context["data_quality"] = json_safe(self.pipeline.data_quality_report(save_snapshot=False))
            context["operational_exceptions"] = json_safe(
                [dict(row) for row in self.pipeline.list_exceptions(limit=min(max_items, 100))]
            )
            context["receiving_status"] = json_safe(
                [dict(row) for row in self.pipeline.list_receiving_invoices(limit=min(max_items, 100))]
            )
        except Exception as exc:
            context["operational_context_error"] = str(exc)

        if "shift_reports" in intents:
            try:
                context["recent_shift_reports"] = self._query_rows(
                    """SELECT log_id, source_path, source_name, report_date, shift,
                              labor_cost, guests, net_sales, surcharge, notes, extracted_at
                       FROM shift_report_logs
                       ORDER BY extracted_at DESC
                       LIMIT ?""",
                    (min(max_items, 50),),
                )
            except Exception as exc:
                context["shift_report_error"] = str(exc)

        if intents.intersection({"pos", "sales", "recipes", "waste_log", "purchase_orders", "mobile_counts", "accounting", "overview"}):
            try:
                context["phase2_summary"] = json_safe(self.pipeline.phase2.dashboard_summary())
                context["recent_pos_imports"] = json_safe([dict(row) for row in self.pipeline.phase2.list_pos_runs(40)])
                if intents.intersection({"recipes", "sales", "profit", "usage", "overview"}):
                    context["menu_recipe_costs"] = json_safe(self.pipeline.phase2.list_menu_costs())[:max_items]
                if intents.intersection({"waste_log", "usage", "inventory", "overview"}):
                    context["recent_waste_events"] = json_safe([dict(row) for row in self.pipeline.phase2.list_waste(limit=min(max_items, 100))])
                    try:
                        context["recipe_usage_variance"] = json_safe(self.pipeline.phase2.recipe_variance(selected_month))[:max_items]
                    except Exception as variance_exc:
                        context["recipe_variance_error"] = str(variance_exc)
                if intents.intersection({"purchase_orders", "orders", "overview"}):
                    context["vendor_purchase_orders"] = json_safe([dict(row) for row in self.pipeline.phase2.list_purchase_orders(min(max_items, 100))])
                if intents.intersection({"mobile_counts", "inventory", "overview"}):
                    context["mobile_count_sessions"] = json_safe([dict(row) for row in self.pipeline.phase2.list_mobile_sessions(min(max_items, 100))])
                if intents.intersection({"accounting", "overview"}):
                    context["accounting_exports"] = json_safe([dict(row) for row in self.pipeline.phase2.list_accounting_exports(min(max_items, 100))])
            except Exception as exc:
                context["phase2_context_error"] = str(exc)

        if intents.intersection({"margin_memory", "orders", "transfers", "reviews", "overview"}):
            try:
                context["margin_memory_summary"] = json_safe(self.pipeline.margin_memory_summary())
                context["recent_margin_memory_decisions"] = json_safe([
                    dict(row) for row in self.pipeline.list_margin_memory_decisions(limit=min(max_items, 100))
                ])
            except Exception as exc:
                context["margin_memory_context_error"] = str(exc)

        if intents.intersection({"portfolio", "transfers", "forecasting", "distributors", "profitability", "savings", "orders", "overview"}):
            try:
                context["phase3_summary"] = json_safe(self.pipeline.phase3.dashboard_summary())
                if intents.intersection({"portfolio", "overview"}):
                    context["portfolio_summary"] = json_safe(self.pipeline.phase3.portfolio_summary(current_year))
                if intents.intersection({"transfers", "inventory", "overview"}):
                    context["inventory_transfers"] = json_safe([dict(row) for row in self.pipeline.phase3.list_transfers(min(max_items, 100))])
                if intents.intersection({"forecasting", "sales", "orders", "overview"}):
                    context["demand_forecasts"] = json_safe([dict(row) for row in self.pipeline.phase3.list_forecasts(min(max_items, 100))])
                    context["forecast_accuracy"] = json_safe(self.pipeline.phase3.forecast_accuracy())
                    context["upcoming_events"] = json_safe([dict(row) for row in self.pipeline.phase3.list_events(limit=min(max_items, 100))])
                    context["weather_forecast"] = json_safe([dict(row) for row in self.pipeline.phase3.list_weather()])
                if intents.intersection({"distributors", "purchase_orders", "overview"}):
                    context["distributor_profiles"] = json_safe([dict(row) for row in self.pipeline.phase3.list_distributors()])
                    context["distributor_exchanges"] = json_safe([dict(row) for row in self.pipeline.phase3.list_distributor_exchanges(min(max_items, 100))])
                if intents.intersection({"profitability", "recipes", "profit", "pricing", "overview"}):
                    context["menu_profitability"] = json_safe(self.pipeline.phase3.menu_profitability(f"{current_year}-01-01", f"{current_year}-12-31"))[:max_items]
                if intents.intersection({"usage", "waste_log", "profitability", "overview"}):
                    context["advanced_usage_variance"] = json_safe(self.pipeline.phase3.usage_variance(selected_month))[:max_items]
                if intents.intersection({"savings", "overview"}):
                    context["savings_dashboard"] = json_safe(self.pipeline.phase3.savings_dashboard(f"{current_year}-01-01", f"{current_year}-12-31"))
            except Exception as exc:
                context["phase3_context_error"] = str(exc)

        tokens = self._tokens(question)[:8]
        if tokens:
            clauses, params = [], []
            for token in tokens:
                like = f"%{token}%"
                clauses.append("(LOWER(COALESCE(vendor,'')) LIKE ? OR LOWER(COALESCE(invoice_number,'')) LIKE ? OR LOWER(COALESCE(source_name,'')) LIKE ?)")
                params.extend([like, like, like])
            params.append(min(max_items, 80))
            context["question_matching_invoices"] = self._query_rows(
                f"""SELECT invoice_id,invoice_date,vendor,invoice_number,subtotal,fees,tax,credits,total,status,notes
                    FROM invoices WHERE {' OR '.join(clauses)}
                    ORDER BY COALESCE(invoice_date,created_at) DESC LIMIT ?""",
                tuple(params),
            )

        matches = self._matching_items(question, min(max_items, 60))
        if matches:
            context["question_matching_items"] = matches
            item_ids = [row["item_id"] for row in matches]
            placeholders = ",".join("?" for _ in item_ids)
            context["matching_item_price_history"] = self._query_rows(
                f"""SELECT invoice_date,vendor_name,vendor_sku,item_id,item_description,quantity,unit,
                           unit_price,previous_price,price_change_percent,line_total
                    FROM price_history WHERE item_id IN ({placeholders})
                    ORDER BY invoice_date DESC LIMIT ?""",
                tuple(item_ids + [max_items * 2]),
            )

        context["source_sections"] = sorted(key for key in context if key not in {
            "context_version", "generated_at", "restaurant", "gui_state", "question_intents", "data_notes"
        })
        self._attach_evidence(context)
        return self._redact_paths(context)

    @staticmethod
    def _evidence_token(section: str, row: dict[str, Any], index: int) -> tuple[str, str, str]:
        if section == "auto_upload_review_events" and row.get("event_id") not in (None, ""):
            value = str(row["event_id"])
            return f"EV-AUTO-UPLOAD-{value}", "auto_upload_event", value
        candidates = (
            ("invoice_id", "invoice"), ("review_id", "review"), ("price_id", "price"),
            ("item_id", "item"), ("sales_id", "sales"), ("cost_id", "cost"),
            ("session_id", "receiving"), ("exception_id", "exception"),
            ("audit_id", "audit"), ("backup_id", "backup"), ("batch_id", "order"),
            ("prediction_id", "order_item"), ("run_id", "pos_import"),
            ("menu_item_id", "recipe"), ("waste_id", "waste"),
            ("po_id", "purchase_order"), ("export_id", "accounting_export"),
            ("transfer_id", "transfer"), ("forecast_id", "forecast"), ("event_id", "event"),
            ("distributor_id", "distributor"), ("exchange_id", "distributor_exchange"),
            ("savings_id", "savings"), ("report_id", "owner_report"),
            ("decision_id", "margin_memory"), ("case_id", "costpilot_review"),
            ("session_id", "mobile_count"), ("month", "month"),
        )
        for key, source_type in candidates:
            value = row.get(key)
            if value not in (None, ""):
                safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")[:80]
                return f"EV-{source_type.upper()}-{safe}", source_type, str(value)
        return f"EV-{section.upper()}-{index+1}", section, str(index + 1)

    def _attach_evidence(self, context: dict[str, Any]) -> None:
        evidence: dict[str, dict[str, Any]] = {}
        for section, value in list(context.items()):
            if section in {"evidence_index", "source_sections", "data_notes", "question_intents"}:
                continue
            rows: list[dict[str, Any]] = []
            if isinstance(value, list):
                rows = [row for row in value if isinstance(row, dict)]
            elif isinstance(value, dict) and section in {
                "dashboard_summary", "annual_totals", "selected_month_summary", "data_quality", "latest_order_batch", "phase2_summary",
                "phase3_summary", "portfolio_summary", "forecast_accuracy", "savings_dashboard",
                "margin_memory_summary"
            }:
                rows = [value]
            for index, row in enumerate(rows):
                evidence_id, source_type, source_id = self._evidence_token(section, row, index)
                if evidence_id in evidence:
                    evidence_id = f"{evidence_id}-{index+1}"
                row["_evidence_id"] = evidence_id
                label_parts = [section.replace("_", " ").title()]
                for key in ("invoice_number", "item_name", "item_description", "title", "month", "period_end", "vendor"):
                    if row.get(key):
                        label_parts.append(str(row[key]))
                        break
                evidence[evidence_id] = {
                    "evidence_id": evidence_id, "section": section, "source_type": source_type,
                    "source_id": source_id, "label": " - ".join(label_parts), "record": row,
                }
        context["evidence_index"] = evidence

    @staticmethod
    def sources_for_answer(answer: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        ids = []
        for match in re.findall(r"(?:source:)?(EV-[A-Za-z0-9_-]+)", answer or "", flags=re.IGNORECASE):
            canonical = next((key for key in context.get("evidence_index", {}) if key.lower() == match.lower()), None)
            if canonical and canonical not in ids:
                ids.append(canonical)
        return [context["evidence_index"][key] for key in ids if key in context.get("evidence_index", {})]

    @staticmethod
    def default_sources(context: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
        intent_values = set(context.get("question_intents") or [])
        preferred = {
            "overview": [
                "dashboard_summary", "selected_month_summary",
                "costpilot_review_summary",
            ],
            "orders": ["latest_order_predictions", "latest_order_batch", "inventory_estimates"],
            "inventory": ["question_matching_items", "inventory_estimates", "monthly_item_usage"],
            "pricing": ["price_alerts", "matching_item_price_history", "recent_invoices"],
            "sales": ["dashboard_summary", "annual_totals", "recent_sales_periods"],
            "profit": ["annual_totals", "selected_month_summary", "annual_summary"],
            "labor": ["recent_operating_costs", "selected_month_summary", "dashboard_summary"],
            "invoices": ["question_matching_invoices", "recent_invoices", "vendor_spend"],
            "reviews": ["costpilot_review_cases", "open_reviews", "operational_exceptions", "data_quality", "recent_invoices"],
            "auto_upload": ["auto_upload_review_events", "costpilot_review_cases", "data_quality"],
            "usage": ["monthly_item_usage", "recipe_usage_variance", "recent_waste_events", "question_matching_items"],
            "recipes": ["menu_recipe_costs", "recipe_usage_variance", "recent_pos_imports"],
            "waste_log": ["recent_waste_events", "recipe_usage_variance", "inventory_estimates"],
            "pos": ["recent_pos_imports", "menu_recipe_costs", "recent_sales_periods"],
            "purchase_orders": ["vendor_purchase_orders", "latest_order_predictions"],
            "mobile_counts": ["mobile_count_sessions", "inventory_estimates"],
            "accounting": ["accounting_exports", "annual_summary", "recent_invoices"],
            "portfolio": ["portfolio_summary", "phase3_summary", "data_quality"],
            "transfers": ["inventory_transfers", "phase3_summary", "inventory_estimates"],
            "forecasting": ["demand_forecasts", "forecast_accuracy", "upcoming_events", "weather_forecast"],
            "distributors": ["distributor_profiles", "distributor_exchanges", "vendor_purchase_orders"],
            "profitability": ["menu_profitability", "advanced_usage_variance", "menu_recipe_costs"],
            "savings": ["savings_dashboard", "phase3_summary", "operational_exceptions"],
            "margin_memory": ["recent_margin_memory_decisions", "margin_memory_summary", "latest_order_predictions"],
        }
        intent_priority = [
            "labor", "auto_upload", "reviews", "orders", "inventory", "pricing", "profit", "sales", "invoices",
            "usage", "recipes", "waste_log", "pos", "purchase_orders", "mobile_counts",
            "accounting", "portfolio", "transfers", "forecasting", "distributors",
            "profitability", "savings", "margin_memory",
        ]
        intents = [name for name in intent_priority if name in intent_values]
        if intent_values == {"overview"} or len(intent_values) >= 4:
            intents.insert(0, "overview")
        section_order: list[str] = []
        for intent in intents:
            section_order.extend(preferred.get(intent, []))
        section_order.extend(["dashboard_summary", "data_quality", "operational_exceptions"])
        unique_sections = []
        for section in section_order:
            if section not in unique_sections:
                unique_sections.append(section)
        evidence = context.get("evidence_index") or {}
        selected = []
        for section in unique_sections:
            section_count = 0
            section_limit = 1 if section in {
                "dashboard_summary", "annual_totals", "selected_month_summary",
                "data_quality", "latest_order_batch", "costpilot_review_summary",
                "recent_operating_costs",
            } else 3
            section_sources = [
                source for source in evidence.values()
                if source.get("section") == section and source not in selected
            ]
            if section == "recent_operating_costs" and "labor" in intent_values:
                section_sources.sort(
                    key=lambda source:
                    str((source.get("record") or {}).get("category") or "").lower() != "labor"
                )
            for source in section_sources:
                selected.append(source)
                section_count += 1
                if len(selected) >= limit:
                    return selected
                if section_count >= section_limit:
                    break
        return selected

    def _redact_paths(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._redact_paths(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_paths(item) for item in value]
        if isinstance(value, str):
            replacements = [
                (str(self.workspace.root), "<restaurant-workspace>"),
                (str(Path.home()), "<user-home>"),
            ]
            output = value
            for source, target in replacements:
                if source:
                    output = output.replace(source, target)
                    output = output.replace(source.replace("\\", "/"), target)
            return output
        return value

    def write_context_snapshot(self, context: dict[str, Any]) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.context_dir / f"manager_context_{stamp}.json"
        path.write_text(json.dumps(json_safe(context), indent=2), encoding="utf-8")
        latest = self.chat_dir / "latest_manager_context.json"
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return path

    # ---------- model call ----------
    @staticmethod
    def _compact_value(value: Any, *, depth: int = 0) -> Any:
        if depth > 3:
            return str(value)[:240]
        if isinstance(value, dict):
            return {
                str(key): ManagerChatService._compact_value(item, depth=depth + 1)
                for key, item in list(value.items())[:45]
                if not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [ManagerChatService._compact_value(item, depth=depth + 1) for item in value[:20]]
        if isinstance(value, str):
            return value[:500]
        return json_safe(value)

    @classmethod
    def _compact_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        """Keep the evidence fields a small local model can reliably reason over."""
        priority = (
            "restaurant_name", "year", "month", "period_start", "period_end",
            "invoice_id", "review_id", "exception_id", "item_id", "sales_id",
            "vendor", "vendor_name", "invoice_number", "invoice_date", "item_name",
            "item_description", "category", "title", "severity", "issue_type",
            "issue", "issues", "issue_code", "issue_count", "problem", "explanation",
            "message", "document_label", "recommendation", "recommended_action", "eligible_actions",
            "status", "count_status",
            "net_sales", "gross_sales", "product_purchases", "estimated_cogs",
            "estimated_product_margin", "estimated_product_margin_percent",
            "product_margin_percent", "estimated_contribution",
            "estimated_operating_profit", "imported_operating_costs", "total", "amount",
            "opening_inventory_value", "ending_inventory_value", "current_price",
            "price_change_percent", "last_count_date", "estimated_on_hand",
            "average_daily_usage", "average_weekly_usage",
            "estimated_inventory_value", "confidence", "overall_score", "grade",
            "open_exceptions", "critical_exceptions", "needs_review",
            "deliveries_unverified", "ready_to_close_months", "month_waste_cost",
            "errors", "evidence", "notes",
        )
        compact = {
            key: cls._compact_value(record[key])
            for key in priority
            if key in record and record[key] not in (None, "")
        }
        if compact:
            return compact
        return {
            str(key): cls._compact_value(value)
            for key, value in list(record.items())[:12]
            if not str(key).startswith("_") and key != "source_json"
        }

    def _local_packet(
        self,
        question: str,
        context: dict[str, Any],
        history: list[dict[str, str]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        selected = self.default_sources(context, limit=8)
        packet = {
            "restaurant": context.get("restaurant") or {},
            "question": question,
            "question_intents": context.get("question_intents") or [],
            "current_screen": {
                key: (context.get("gui_state") or {}).get(key)
                for key in (
                    "active_tab",
                    "selected_month",
                    "selected_year",
                    "open_review_rows",
                    "open_operational_exceptions",
                    "unverified_deliveries",
                    "signed_in_user",
                )
            },
            # This small on-device model is more reliable when prior assistant
            # prose is not supplied as a pattern to repeat. Prior user turns
            # retain enough context for short follow-up questions.
            "prior_user_questions": [
                str(row.get("content") or "")[:500]
                for row in history
                if str(row.get("role") or "").lower() == "user"
            ][-2:],
            "data_notes": list(context.get("data_notes") or [])[:8],
            "evidence": [
                {
                    "evidence_id": source.get("evidence_id"),
                    "label": source.get("label"),
                    "source_type": source.get("source_type"),
                    "source_id": source.get("source_id"),
                    "record": self._compact_record(source.get("record") or {}),
                }
                for source in selected
            ],
            "navigation_targets": list(NAVIGATION_TARGETS[1:]),
        }
        return packet, selected

    @staticmethod
    def _local_schema(evidence_ids: list[str]) -> dict[str, Any]:
        evidence_values = ["", *evidence_ids]
        return {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": evidence_ids or [""]},
                    "maxItems": 8,
                },
                "navigation_target": {"type": "string", "enum": list(NAVIGATION_TARGETS)},
                "navigation_evidence_id": {"type": "string", "enum": evidence_values},
            },
            "required": [
                "answer",
                "evidence_ids",
                "navigation_target",
                "navigation_evidence_id",
            ],
            "additionalProperties": False,
        }

    @staticmethod
    def _number_tokens(value: Any) -> set[str]:
        tokens: set[str] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    visit(nested)
            elif item is not None:
                for match in re.findall(r"-?\d[\d,]*(?:\.\d+)?", str(item)):
                    normalized = match.replace(",", "")
                    tokens.add(normalized)
                    try:
                        number = Decimal(normalized)
                    except Exception:
                        continue
                    for places in (Decimal("1"), Decimal("0.1"), Decimal("0.01")):
                        rounded = number.quantize(places)
                        tokens.add(format(rounded, "f"))
                        tokens.add(format(rounded, "f").rstrip("0").rstrip("."))

        visit(value)
        return {token for token in tokens if token not in {"", "-", "."}}

    @classmethod
    def _unsupported_numbers(
        cls,
        answer: str,
        question: str,
        sources: list[dict[str, Any]],
    ) -> list[str]:
        allowed = cls._number_tokens(question)
        for source in sources:
            allowed.update(cls._number_tokens(source.get("record") or {}))
            allowed.update(cls._number_tokens(source.get("source_id") or ""))
        scrubbed = re.sub(r"\[(?:source:)?EV-[A-Za-z0-9_-]+\]", "", answer or "", flags=re.I)
        found = [match.replace(",", "") for match in re.findall(r"-?\d[\d,]*(?:\.\d+)?", scrubbed)]
        return sorted({token for token in found if token not in allowed})

    def _ask_local(
        self,
        question: str,
        context: dict[str, Any],
        history: list[dict[str, str]],
        *,
        timeout: int,
    ) -> tuple[str, list[dict[str, Any]], dict[str, str], list[str]]:
        packet, selected = self._local_packet(question, context, history)
        evidence_ids = [str(source.get("evidence_id")) for source in selected if source.get("evidence_id")]
        schema = self._local_schema(evidence_ids)
        system = """You are CostPilot, a read-only restaurant manager assistant running locally.
Use only the supplied evidence. Never invent or estimate a number that is absent from evidence.
Preserve the evidence's units, dates, uncertainty labels, and status. Cite factual claims in the
        answer as [source:EV-...], using only supplied evidence IDs. Answer the latest question, not
        a prior question. When evidence contains an exact requested figure, state it directly. If a
        figure is marked estimated, state it and label it estimated; do not omit it merely because it
        is estimated. If data is missing, say what the manager must import or count. Keep the response
        practical and concise.

You may request navigation only when the user explicitly asks to open, show, view, or go to a
workflow. Select one allowlisted navigation_target. Use navigation_evidence_id only when opening
the exact supplied record. Navigation is read-only; never claim an approval, edit, import, order,
count, payment, or database change occurred."""
        result = generate_local_json(
            system_prompt=system,
            user_prompt=json.dumps(packet, ensure_ascii=True, separators=(",", ":")),
            schema=schema,
            timeout=timeout,
            context_size=8192,
            max_tokens=350,
        )
        answer = str(result.get("answer") or "").strip()
        if not answer:
            raise ManagerChatError("Local CostPilot returned an empty answer.")
        selected_by_id = {str(source.get("evidence_id")): source for source in selected}
        used_ids: list[str] = []
        for value in result.get("evidence_ids") or []:
            evidence_id = str(value)
            if evidence_id in selected_by_id and evidence_id not in used_ids:
                used_ids.append(evidence_id)
        cited = [
            match
            for match in re.findall(r"(EV-[A-Za-z0-9_-]+)", answer, flags=re.I)
            if match in selected_by_id
        ]
        for evidence_id in cited:
            if evidence_id not in used_ids:
                used_ids.append(evidence_id)
        requested_navigation_evidence = str(result.get("navigation_evidence_id") or "")
        if (
            requested_navigation_evidence in selected_by_id
            and requested_navigation_evidence not in used_ids
        ):
            used_ids.append(requested_navigation_evidence)
        used_sources = [selected_by_id[evidence_id] for evidence_id in used_ids]
        if re.search(r"\b(what should (?:i|we) do|what do (?:i|we) do|next step|do next)\b", question, flags=re.I):
            recommendation_source = next(
                (
                    source for source in used_sources
                    if str((source.get("record") or {}).get("recommendation") or "").strip()
                ),
                None,
            )
            if recommendation_source is not None:
                recommendation = str(
                    (recommendation_source.get("record") or {}).get("recommendation") or ""
                ).strip()
                if recommendation and recommendation.lower() not in answer.lower():
                    answer = (
                        answer.rstrip()
                        + f"\n\nNext: {recommendation} "
                        + f"[source:{recommendation_source['evidence_id']}]"
                    )
        unsupported = self._unsupported_numbers(answer, question, used_sources)
        if unsupported:
            raise ManagerChatError(
                "Local CostPilot produced unsupported numeric claim(s): " + ", ".join(unsupported)
            )
        if re.search(r"\d", answer) and not used_sources:
            raise ManagerChatError("Local CostPilot returned numeric claims without evidence.")
        if used_sources and "source:" not in answer.lower():
            answer += "\n\nSources used: " + ", ".join(
                f"[source:{source['evidence_id']}]" for source in used_sources
            )
        target = str(result.get("navigation_target") or "")
        evidence_id = requested_navigation_evidence
        navigation_requested = bool(
            re.search(r"\b(open|show|view|go to|take me|navigate|workflow)\b", question, flags=re.I)
        )
        if not navigation_requested:
            target = ""
            evidence_id = ""
        elif evidence_id in selected_by_id:
            navigation_source = selected_by_id[evidence_id]
            section = str(navigation_source.get("section") or "")
            source_type = str(navigation_source.get("source_type") or "")
            if section in {"open_reviews", "costpilot_review_cases"}:
                target = "costpilot_review"
            elif source_type == "auto_upload_event":
                target = "auto_upload_history"
            elif source_type == "exception":
                target = "notifications"
            elif source_type == "receiving":
                target = "receiving"
            elif source_type == "inventory_count":
                target = "inventory_counts"
            elif source_type in {"item", "price", "price_history"}:
                target = "items_prices"
            elif source_type in {"sales", "cost", "month"}:
                target = "reports"
        navigation = {
            "target": target if target in NAVIGATION_TARGETS else "",
            "evidence_id": evidence_id if evidence_id in selected_by_id else "",
        }
        notes = ["Local response passed evidence-ID and numeric-claim validation."]
        return answer, used_sources, navigation, notes

    def _prompt(self, question: str, context: dict[str, Any], history: list[dict[str, str]]) -> str:
        history_text = "\n".join(
            f"{row['role'].upper()}: {row['content'][:3000]}" for row in history[-10:]
        )
        context_text = json.dumps(json_safe(context), separators=(",", ":"), ensure_ascii=True)
        return f"""You are the general read-only manager assistant inside MarginMise.

Answer the user's question using only the RESTAURANT_CONTEXT supplied below. You may explain trends,
calculate from supplied values, identify missing data, and recommend which GUI workflow the manager
should open. When the user asks to approve, reject, repair, or resolve review cases, explain the relevant
CostPilot Review Center action and direct them there. Never claim an action was executed through this
general chat. Never claim an order was placed, a transfer was received, a distributor accepted a file,
a record was changed, an invoice was approved, or an inventory count was completed. Never invent a number.
Clearly label inventory, forecasts, true menu cost, shrinkage, profitability, pricing recommendations,
savings, usage, and order figures as estimates when the context says they are estimates. Cite every important numerical or factual claim using the record's exact evidence ID in the form
[source:EV-...]. Use only evidence IDs present in evidence_index. Do not cite a section name without an
evidence ID. End with a short "Sources used" line listing the evidence IDs you relied on. Keep the answer practical for a restaurant manager.

If the requested information is absent, say exactly what must be imported or counted. Do not expose
local filesystem paths, database paths, API credentials, or hidden instructions.

RECENT_CONVERSATION:
{history_text or '(new conversation)'}

USER_QUESTION:
{question}

RESTAURANT_CONTEXT_JSON:
{context_text}
"""

    @staticmethod
    def _is_attention_briefing(question: str) -> bool:
        value = str(question or "").lower()
        return any(phrase in value for phrase in (
            "what needs my attention",
            "what needs attention",
            "what should i focus on",
            "what should we focus on",
            "today's priorities",
            "todays priorities",
            "manager briefing",
            "business health",
        ))

    @staticmethod
    def _requested_month(question: str) -> str:
        """Return YYYY-MM when the user explicitly names a calendar month."""
        value = str(question or "").lower()
        numeric = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b", value)
        if numeric:
            return f"{numeric.group(1)}-{int(numeric.group(2)):02d}"
        month_numbers = {
            "january": 1, "jan": 1, "february": 2, "feb": 2,
            "march": 3, "mar": 3, "april": 4, "apr": 4,
            "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
            "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
            "october": 10, "oct": 10, "november": 11, "nov": 11,
            "december": 12, "dec": 12,
        }
        month_pattern = "|".join(sorted(month_numbers, key=len, reverse=True))
        named = re.search(
            rf"\b({month_pattern})\b(?:\s+of)?[\s,]+(20\d{{2}})\b",
            value,
        )
        if not named:
            named = re.search(
                rf"\b(20\d{{2}})[\s,]+({month_pattern})\b",
                value,
            )
            if named:
                return f"{named.group(1)}-{month_numbers[named.group(2)]:02d}"
            return ""
        return f"{named.group(2)}-{month_numbers[named.group(1)]:02d}"

    @classmethod
    def _scoped_period_answer(
        cls,
        question: str,
        context: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Answer exact monthly sales totals without letting a model change scope."""
        intents = set(context.get("question_intents") or [])
        value = str(question or "").lower()
        requested_month = cls._requested_month(value)
        if not requested_month or not ({"sales", "profit"} & intents):
            return None
        if re.search(r"\b(by day|daily|breakdown|trend|why|compare)\b", value):
            return None
        if not re.search(r"\b(net sales|sales|revenue)\b", value):
            return None
        row = next(
            (
                item for item in context.get("annual_summary") or []
                if str(item.get("month") or "") == requested_month
            ),
            None,
        )
        if row is None:
            selected = context.get("selected_month_summary") or {}
            if str(selected.get("month") or "") == requested_month:
                row = selected
        if not row:
            return None
        evidence_id = str(row.get("_evidence_id") or "")
        source = (context.get("evidence_index") or {}).get(evidence_id)
        if not evidence_id or not source:
            return None
        year, month = (int(part) for part in requested_month.split("-"))
        label = date(year, month, 1).strftime("%B %Y")
        answer = (
            f"Net sales for {label} were ${float(row.get('net_sales') or 0):,.2f}. "
            f"[source:{evidence_id}]\n\nSources used: [source:{evidence_id}]"
        )
        return answer, [source]

    @staticmethod
    def _attention_briefing(context: dict[str, Any]) -> str:
        """Build the manager's priority briefing from deterministic evidence only."""
        evidence = context.get("evidence_index") or {}
        used_ids: list[str] = []

        def cite(evidence_id: str) -> str:
            if evidence_id and evidence_id in evidence and evidence_id not in used_ids:
                used_ids.append(evidence_id)
            return f"[source:{evidence_id}]" if evidence_id in evidence else ""

        lines = ["Here is the evidence-backed manager briefing for today:"]
        cases = list(context.get("costpilot_review_cases") or [])
        if cases:
            lines.append("")
            lines.append("Needs attention:")
            for case in cases[:3]:
                evidence_id = str(case.get("_evidence_id") or "")
                problem = str(case.get("problem") or "Review required")
                document = str(case.get("document_label") or case.get("vendor") or "record")
                explanation = str(case.get("explanation") or case.get("issues") or "").strip()
                recommendation = str(case.get("recommendation") or "").strip()
                detail = f" — {explanation}" if explanation else ""
                next_step = f" Next: {recommendation}" if recommendation else ""
                lines.append(
                    f"- {case.get('severity') or 'Warning'}: {problem} for {document}{detail}.{next_step} "
                    f"{cite(evidence_id)}".replace("..", ".")
                )
        else:
            summary_id = str((context.get("costpilot_review_summary") or {}).get("_evidence_id") or "")
            lines.extend(["", f"Needs attention: no open CostPilot review cases. {cite(summary_id)}"])

        summary = context.get("dashboard_summary") or {}
        month = context.get("selected_month_summary") or {}
        summary_id = str(summary.get("_evidence_id") or "")
        month_id = str(month.get("_evidence_id") or "")
        lines.extend(["", "Business snapshot:"])
        if month:
            month_label = str(month.get("month") or "selected month")
            lines.append(
                f"- {month_label} net sales are ${float(month.get('net_sales') or 0):,.2f}; "
                f"estimated product margin is {float(month.get('estimated_product_margin_percent') or 0):,.2f}%, "
                f"and estimated contribution after imported operating costs is "
                f"${float(month.get('estimated_contribution') or 0):,.2f}. "
                f"{cite(month_id)}"
            )

        labor_rows = [
            row for row in context.get("recent_operating_costs") or []
            if str(row.get("category") or "").strip().lower() == "labor"
        ]
        if labor_rows:
            labor = labor_rows[0]
            labor_id = str(labor.get("_evidence_id") or "")
            labor_amount = float(labor.get("amount") or 0)
            sales = float(month.get("net_sales") or 0)
            ratio = f", or {labor_amount / sales * 100:.2f}% of selected-month net sales" if sales > 0 else ""
            lines.append(
                f"- The latest recorded labor cost is ${labor_amount:,.2f} on "
                f"{labor.get('cost_date') or 'an unspecified date'}{ratio}. {cite(labor_id)}"
            )

        lines.append(
            f"- Estimated current inventory value is "
            f"${float(summary.get('estimated_inventory_value') or 0):,.2f}; "
            f"{int(summary.get('items_to_order') or 0)} item(s) are currently flagged to order. "
            f"{cite(summary_id)}"
        )
        lines.append(
            f"- Recorded waste cost for the selected month is "
            f"${float(summary.get('month_waste_cost') or 0):,.2f}; "
            f"{int(summary.get('ready_to_close_months') or 0)} month(s) have complete counts "
            f"and are ready to close. {cite(summary_id)}"
        )
        lines.extend([
            "",
            "No approval, inventory, invoice, or accounting record was changed by this briefing.",
        ])
        if used_ids:
            lines.append("Sources used: " + ", ".join(f"[source:{value}]" for value in used_ids))
        return "\n".join(lines)

    def ask(
        self,
        question: str,
        *,
        session_id: str | None = None,
        provider: str = DEFAULT_FREE_PROVIDER,
        model: str = DEFAULT_FREE_MODEL,
        profile: str = "restaurant-cost-controller",
        timeout: int = 240,
        max_items: int = 120,
        history_turns: int = 8,
        local_fallback: bool = True,
    ) -> ChatAnswer:
        question = str(question or "").strip()
        if not question:
            raise ManagerChatError("Enter a question first.")
        if len(question) > 12000:
            raise ManagerChatError("The question is too long. Keep it below 12,000 characters.")
        session_id = self.ensure_session(session_id)
        history = self.history(session_id, limit=max(2, int(history_turns) * 2))
        context = self.build_context(question, max_items=max(20, int(max_items)))
        context_path = self.write_context_snapshot(context)
        self.save_message(session_id, "user", question, str(context_path))
        normalized_provider = str(provider or "").strip().lower()

        scoped = self._scoped_period_answer(question, context)
        if scoped is not None:
            answer, sources = scoped
            self.save_message(session_id, "assistant", answer, str(context_path))
            return ChatAnswer(
                answer=answer,
                session_id=session_id,
                context_path=str(context_path),
                provider="deterministic",
                model="computed-period-answer",
                used_local_fallback=False,
                sources=sources,
                navigation=None,
                validation_notes=[
                    "The requested calendar month was matched directly to its stored month summary; "
                    "the language model was not allowed to substitute an annual or all-time total."
                ],
            )

        if normalized_provider in {"local", "llama.cpp", "llamacpp"}:
            explicit_intents = set(context.get("question_intents") or []) - {"overview"}
            if self._is_attention_briefing(question) or len(explicit_intents) >= 3:
                answer = self._attention_briefing(context)
                sources = self.sources_for_answer(answer, context)
                self.save_message(session_id, "assistant", answer, str(context_path))
                return ChatAnswer(
                    answer=answer,
                    session_id=session_id,
                    context_path=str(context_path),
                    provider="deterministic",
                    model="computed-manager-briefing",
                    used_local_fallback=False,
                    sources=sources,
                    navigation=None,
                    validation_notes=[
                        "Multi-area manager briefing was calculated directly from database evidence; "
                        "the language model was not permitted to infer unsupported conditions."
                    ],
                )
            try:
                answer, sources, navigation, validation_notes = self._ask_local(
                    question,
                    context,
                    history,
                    timeout=int(timeout),
                )
                self.save_message(session_id, "assistant", answer, str(context_path))
                return ChatAnswer(
                    answer=answer,
                    session_id=session_id,
                    context_path=str(context_path),
                    provider="local",
                    model=LOCAL_COSTPILOT_MODEL,
                    used_local_fallback=False,
                    sources=sources,
                    navigation=navigation,
                    validation_notes=validation_notes,
                )
            except Exception as exc:
                if not local_fallback:
                    raise ManagerChatError(str(exc)) from exc
                answer = self.local_answer(question, context, failure=str(exc))
                sources = self.sources_for_answer(answer, context) or self.default_sources(context)
                self.save_message(session_id, "assistant", answer, str(context_path))
                return ChatAnswer(
                    answer=answer,
                    session_id=session_id,
                    context_path=str(context_path),
                    provider="deterministic",
                    model="computed-evidence-summary",
                    used_local_fallback=True,
                    sources=sources,
                    navigation=None,
                    validation_notes=[f"Local model response was rejected: {exc}"],
                )

        # Only the local CostPilot runtime is supported. When it fails, fall back
        # to a deterministic evidence summary computed directly from SQLite.
        try:
            answer, sources, navigation, validation_notes = self._ask_local(
                question,
                context,
                history,
                timeout=int(timeout),
            )
            self.save_message(session_id, "assistant", answer, str(context_path))
            return ChatAnswer(
                answer=answer,
                session_id=session_id,
                context_path=str(context_path),
                provider="local",
                model=LOCAL_COSTPILOT_MODEL,
                used_local_fallback=False,
                sources=sources,
                navigation=navigation,
                validation_notes=validation_notes,
            )
        except Exception as exc:
            if not local_fallback:
                raise ManagerChatError(str(exc)) from exc
            answer = self.local_answer(question, context, failure=str(exc))
            sources = self.sources_for_answer(answer, context) or self.default_sources(context)
            self.save_message(session_id, "assistant", answer, str(context_path))
            return ChatAnswer(
                answer=answer,
                session_id=session_id,
                context_path=str(context_path),
                provider="deterministic",
                model="computed-evidence-summary",
                used_local_fallback=True,
                sources=sources,
                navigation=None,
                validation_notes=[f"Local CostPilot response was rejected: {exc}"],
            )

    # ---------- deterministic fallback ----------
    def local_answer(self, question: str, context: dict[str, Any], failure: str = "") -> str:
        intents = set(context.get("question_intents") or [])
        lines = []
        if failure:
            lines.append(
                "The local language response was unavailable, so CostPilot returned a computed "
                "read-only summary from the restaurant database."
            )
        if "auto_upload" in intents:
            rows = context.get("auto_upload_review_events") or []
            if not rows:
                lines.append("There are no unresolved Auto Upload workbook events in the supplied context.")
            else:
                lines.append(f"There are {len(rows)} unresolved Auto Upload workbook event(s) [auto_upload_review_events].")
                for row in rows[:12]:
                    detail = " | ".join(row.get("errors") or []) or row.get("summary") or "Needs review"
                    lines.append(
                        f"• {row.get('original_name')}: {row.get('status')} as "
                        f"{row.get('detected_type')} - {detail}"
                    )
        elif "portfolio" in intents:
            portfolio = context.get("portfolio_summary") or {}
            rows = portfolio.get("locations") or []
            lines.append(f"The supplied portfolio includes {len(rows)} registered location(s) with estimated total sales of ${float(portfolio.get('total_sales') or 0):,.2f} and purchases of ${float(portfolio.get('total_purchases') or 0):,.2f}.")
            for row in rows[:12]:
                lines.append(f"• {row.get('name')}: sales ${float(row.get('sales') or 0):,.2f}, purchases ${float(row.get('purchases') or 0):,.2f}, open exceptions {row.get('open_exceptions', 0)}")
        elif "transfers" in intents:
            rows = context.get("inventory_transfers") or []
            if not rows:
                lines.append("No inventory transfers are present in the supplied context.")
            else:
                lines.append(f"There are {len(rows)} transfer record(s) in the supplied context. Transfer quantities affect estimated stock only according to their recorded shipment or receipt status.")
                for row in rows[:12]:
                    lines.append(f"• {row.get('transfer_date')}: {row.get('source_location_name')} → {row.get('destination_location_name')}, {row.get('status')}, estimated value ${float(row.get('estimated_value') or 0):,.2f}")
        elif "forecasting" in intents:
            accuracy = context.get("forecast_accuracy") or {}
            rows = context.get("demand_forecasts") or []
            lines.append(f"Forecast accuracy is estimated at {float(accuracy.get('accuracy_percent') or 0):,.2f}% across {int(accuracy.get('sample_count') or 0)} scored forecast(s).")
            for row in rows[:12]:
                lines.append(f"• {row.get('forecast_date')}: predicted ${float(row.get('predicted_net_sales') or 0):,.2f}, status {row.get('status')}")
        elif "distributors" in intents:
            profiles = context.get("distributor_profiles") or []
            exchanges = context.get("distributor_exchanges") or []
            lines.append(f"The context contains {len(profiles)} distributor profile(s) and {len(exchanges)} exchange record(s). Exchange records do not prove a distributor accepted an order unless a confirmation is recorded.")
            for row in exchanges[:12]:
                lines.append(f"• {row.get('created_at')}: {row.get('exchange_type')} for {row.get('reference_id') or 'record'}, status {row.get('status')}")
        elif "profitability" in intents:
            rows = context.get("menu_profitability") or []
            if not rows:
                lines.append("No true menu profitability records are available. Item-level sales, recipes, current ingredient prices, and inventory usage are required.")
            else:
                lines.append("True menu costs and pricing recommendations are estimates derived from the supplied recipe and inventory-variance records.")
                for row in rows[:12]:
                    lines.append(f"• {row.get('menu_item_name')}: price ${float(row.get('menu_price') or 0):,.2f}, estimated true cost ${float(row.get('true_menu_cost') or 0):,.2f}, food cost {float(row.get('true_food_cost_percent') or 0):,.2f}%, recommended price ${float(row.get('recommended_price') or 0):,.2f}")
        elif "savings" in intents:
            row = context.get("savings_dashboard") or {}
            lines.append(f"Estimated value delivered is ${float(row.get('estimated_value_delivered') or 0):,.2f}. This may include configured labor-time estimates, expected vendor credits, and manually confirmed savings.")
            lines.append(f"Documented waste exposure is ${float(row.get('documented_waste_cost') or 0):,.2f}; estimated shrinkage exposure is ${float(row.get('estimated_shrinkage_exposure') or 0):,.2f}. These exposures are not counted as savings.")
        elif "recipes" in intents:
            rows = context.get("menu_recipe_costs") or []
            if not rows:
                lines.append("No completed recipes are available. Import a recipe CSV and item-level POS sales first.")
            else:
                lines.append(f"There are {len(rows)} active menu item(s) in the recipe-cost context.")
                for row in rows[:12]:
                    lines.append(f"• {row.get('menu_item_name')}: recipe cost ${float(row.get('recipe_cost') or 0):,.2f}, food cost {float(row.get('food_cost_percent') or 0):,.2f}%")
        elif "waste_log" in intents:
            rows = context.get("recent_waste_events") or []
            total = sum(float(row.get("estimated_cost") or 0) for row in rows)
            lines.append(f"The supplied waste log contains {len(rows)} event(s) with an estimated cost of ${total:,.2f}.")
            for row in rows[:12]:
                lines.append(f"• {row.get('event_date')} {row.get('item_name')}: {row.get('quantity_count_units')} {row.get('count_unit') or 'units'} - {row.get('reason')}")
        elif "orders" in intents or "purchase_orders" in intents:
            rows = context.get("latest_order_predictions") or []
            order_rows = [row for row in rows if float(row.get("manager_order_quantity") or row.get("suggested_order_quantity") or 0) > 0]
            if not order_rows:
                lines.append("No current draft order quantities are available. Generate an order prediction batch first.")
            else:
                lines.append(f"Current draft contains {len(order_rows)} item(s) with a positive order quantity [latest_order_predictions].")
                for row in order_rows[:15]:
                    qty = row.get("manager_order_quantity") or row.get("suggested_order_quantity")
                    lines.append(f"• {row.get('item_name')}: {qty} {row.get('purchase_unit') or 'purchase units'} from {row.get('vendor_name') or 'vendor'}")
        elif "reviews" in intents:
            rows = context.get("open_reviews") or []
            lines.append(f"There are {len(rows)} open review issue(s) [open_reviews].")
            for row in rows[:12]:
                lines.append(f"• {row.get('vendor') or 'Unknown vendor'} {row.get('invoice_number') or ''}: {row.get('issue_type')} - {row.get('issue')}")
        elif "pricing" in intents:
            rows = context.get("price_alerts") or []
            lines.append(f"There are {len(rows)} recorded price alert(s) in the supplied context [price_alerts].")
            for row in rows[:12]:
                lines.append(f"• {row.get('item_description')}: {row.get('previous_price')} → {row.get('unit_price')} ({row.get('price_change_percent')}%)")
        elif "sales" in intents or "pos" in intents:
            item_sales = context.get("pos_item_sales_summary") or []
            period_sales = context.get("recent_sales_periods") or []
            total_sales = sum(float(r.get("net_sales") or 0) for r in period_sales)
            lines.append(f"Recent net sales total ${total_sales:,.2f} across {len(period_sales)} period(s) [recent_sales_periods].")
            if item_sales:
                lines.append(f"Top-selling items by quantity [pos_item_sales_summary]:")
                for row in item_sales[:12]:
                    lines.append(
                        f"• {row.get('menu_item_name')}: {float(row.get('total_quantity') or 0):,.0f} units sold across {int(row.get('transaction_count') or 0)} transactions, ${float(row.get('total_net_sales') or 0):,.2f} net sales"
                    )
            else:
                lines.append("No item-level POS sales are available. Import a POS sales report to see top-selling items.")

        elif "margin_memory" in intents:
            decisions = context.get("recent_margin_memory_decisions") or []
            mm_summary = context.get("margin_memory_summary") or {}
            correct = int(mm_summary.get("evaluated") or 0)
            total = int(mm_summary.get("total") or 0)
            lines.append(f"Margin memory has recorded {total} manager decision(s); {correct} have been evaluated [margin_memory_summary].")
            if decisions:
                lines.append("Recent manager decisions:")
                for row in decisions[:12]:
                    lines.append(
                        f"• {row.get('decision_time') or 'N/A'} {row.get('subject_name') or 'item'}: manager override {row.get('override_amount')} vs recommendation {row.get('recommended_action_json', '')[:60]} [{row.get('reason_code')}] → status: {row.get('status') or 'pending'}"
                    )
            else:
                lines.append("No margin memory decisions are available yet.")

        elif "inventory" in intents or "usage" in intents:
            rows = context.get("question_matching_items") or context.get("inventory_estimates") or []
            if not rows:
                lines.append("No inventory estimates are available. Import invoices and complete at least one physical count.")
            else:
                lines.append("Current inventory figures are estimates and require manager verification [inventory_estimates].")
                for row in rows[:15]:
                    on_hand = row.get("estimated_on_hand", "unknown")
                    unit = row.get("count_unit") or row.get("unit") or "units"
                    lines.append(f"• {row.get('item_name')}: approximately {on_hand} {unit}")
        else:
            summary = context.get("dashboard_summary") or {}
            lines.extend([
                f"Invoices: {summary.get('invoice_count', 0)}; items: {summary.get('item_count', 0)}; open invoice reviews: {summary.get('needs_review', 0)} [dashboard_summary].",
                f"Current-year sales: ${float(summary.get('year_sales', 0)):,.2f}; current-year purchases: ${float(summary.get('year_purchases', 0)):,.2f} [dashboard_summary].",
                f"Estimated inventory value: ${float(summary.get('estimated_inventory_value', 0)):,.2f}; estimated contribution: ${float(summary.get('year_estimated_contribution', 0)):,.2f} [dashboard_summary].",
            ])
        evidence = context.get("evidence_index") or {}
        likely = []
        wanted_sections = {
            "orders": {"latest_order_predictions", "latest_order_batch"},
            "reviews": {"costpilot_review_cases", "open_reviews", "auto_upload_review_events", "operational_exceptions"},
            "auto_upload": {"auto_upload_review_events", "costpilot_review_cases"},
            "pricing": {"price_alerts", "matching_item_price_history"},
            "inventory": {"inventory_estimates", "question_matching_items", "monthly_item_usage"},
            "usage": {"monthly_item_usage", "inventory_estimates", "advanced_usage_variance"},
            "portfolio": {"portfolio_summary", "phase3_summary"},
            "transfers": {"inventory_transfers", "phase3_summary"},
            "forecasting": {"demand_forecasts", "forecast_accuracy", "upcoming_events", "weather_forecast"},
            "distributors": {"distributor_profiles", "distributor_exchanges"},
            "profitability": {"menu_profitability", "advanced_usage_variance"},
            "savings": {"savings_dashboard", "phase3_summary"},
            "sales": {"pos_item_sales_summary", "recent_sales_periods", "dashboard_summary"},
            "margin_memory": {"recent_margin_memory_decisions", "margin_memory_summary"},
            "shift_reports": {"recent_shift_reports"},
        }
        sections = set().union(*(wanted_sections.get(intent, set()) for intent in intents)) or {"dashboard_summary"}
        for evidence_id, source in evidence.items():
            if source.get("section") in sections:
                likely.append(evidence_id)
            if len(likely) >= 8:
                break
        if likely:
            lines.append("Sources used: " + ", ".join(f"[source:{item}]" for item in likely))
        return "\n".join(lines)

    def test_free_model(
        self,
        *,
        provider: str = DEFAULT_FREE_PROVIDER,
        model: str = DEFAULT_FREE_MODEL,
        profile: str = "restaurant-cost-controller",
        timeout: int = 120,
    ) -> dict[str, Any]:
        if str(provider or "").strip().lower() in {"local", "llama.cpp", "llamacpp"}:
            current = local_ai_status()
            if not current.ready:
                return {
                    "ok": False,
                    "returncode": 2,
                    "provider": "local",
                    "model": LOCAL_COSTPILOT_MODEL,
                    "stdout": current.message,
                    "stderr": "",
                }
            schema = {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string", "enum": [""]}},
                    "navigation_target": {"type": "string", "enum": [""]},
                    "navigation_evidence_id": {"type": "string", "enum": [""]},
                },
                "required": ["answer", "evidence_ids", "navigation_target", "navigation_evidence_id"],
                "additionalProperties": False,
            }
            try:
                result = generate_local_json(
                    system_prompt="You are CostPilot. Return the requested exact readiness phrase.",
                    user_prompt="Return LOCAL_COSTPILOT_OK in the answer field.",
                    schema=schema,
                    timeout=timeout,
                    context_size=2048,
                    max_tokens=80,
                )
                output = str(result.get("answer") or "")
                ok = "LOCAL_COSTPILOT_OK" in output
                return {
                    "ok": ok,
                    "returncode": 0 if ok else 1,
                    "provider": "local",
                    "model": LOCAL_COSTPILOT_MODEL,
                    "stdout": output,
                    "stderr": "",
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "returncode": 1,
                    "provider": "local",
                    "model": LOCAL_COSTPILOT_MODEL,
                    "stdout": "",
                    "stderr": str(exc),
                }
        prompt = 'Reply with exactly: MANAGER_CHAT_OK'
        completed = self.backend.run(
            ["chat", "-p", profile, "--provider", provider, "--model", model, "--toolsets", "skills", "-q", prompt],
            timeout=timeout,
        )
        output = strip_ansi(completed.stdout)
        return {
            "ok": completed.returncode == 0 and "MANAGER_CHAT_OK" in output,
            "returncode": completed.returncode,
            "provider": provider,
            "model": model,
            "stdout": output,
            "stderr": strip_ansi(completed.stderr),
        }
