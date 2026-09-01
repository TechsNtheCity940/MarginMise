"""Read-only data service for the MarginMise Overview dashboard.

The service deliberately queries the existing operational ledger instead of
creating a second data model.  Every returned value is derived from stored
restaurant data; unavailable values are explicitly marked unavailable.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from inventory_planning import preferred_sales_rows


DATE_RANGE_OPTIONS = (
    "Today",
    "Yesterday",
    "Last 7 Days",
    "Last 30 Days",
    "This Month",
    "Last Month",
    "Custom Range",
)

SEVERITY_ORDER = {
    "Critical": 0,
    "High": 1,
    "Warning": 2,
    "Medium": 2,
    "Info": 3,
    "Low": 3,
}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value: Any) -> float:
    return float(_decimal(value).quantize(Decimal("0.01")))


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _percent_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def resolve_date_range(
    selection: str,
    *,
    today: date | None = None,
    custom_start: str | date | None = None,
    custom_end: str | date | None = None,
) -> tuple[date, date]:
    """Return inclusive start/end dates for a dashboard range selection."""
    current = today or date.today()
    if selection == "Today":
        return current, current
    if selection == "Yesterday":
        yesterday = current - timedelta(days=1)
        return yesterday, yesterday
    if selection == "Last 30 Days":
        return current - timedelta(days=29), current
    if selection == "This Month":
        return current.replace(day=1), current
    if selection == "Last Month":
        first_this_month = current.replace(day=1)
        last_previous = first_this_month - timedelta(days=1)
        return last_previous.replace(day=1), last_previous
    if selection == "Custom Range":
        start = date.fromisoformat(custom_start) if isinstance(custom_start, str) else custom_start
        end = date.fromisoformat(custom_end) if isinstance(custom_end, str) else custom_end
        if not start or not end:
            raise ValueError("A custom dashboard range requires a start and end date.")
        if start > end:
            raise ValueError("The custom start date must be on or before the end date.")
        return start, end
    return current - timedelta(days=6), current


def _previous_range(start: date, end: date) -> tuple[date, date]:
    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    return previous_end - timedelta(days=days - 1), previous_end


class DashboardService:
    """Builds cached, read-only dashboard models from an ``InvoicePipeline``."""

    def __init__(self, pipeline: Any, *, cache_seconds: float = 30.0):
        self.pipeline = pipeline
        self.workspace = pipeline.workspace
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def get_filter_options(self) -> dict[str, list[str]]:
        """Return only filter dimensions that have meaningful stored values."""
        with self.workspace.connect() as conn:
            vendors = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT vendor FROM invoices WHERE TRIM(COALESCE(vendor,''))<>'' ORDER BY vendor"
                ).fetchall()
            ]
            categories = [
                str(row[0])
                for row in conn.execute(
                    """SELECT category FROM (
                           SELECT DISTINCT category FROM invoice_lines
                           UNION SELECT DISTINCT category FROM operating_costs
                           UNION SELECT DISTINCT category FROM menu_items
                       ) WHERE TRIM(COALESCE(category,''))<>'' ORDER BY category"""
                ).fetchall()
            ]
        return {
            "vendors": vendors if len(vendors) > 1 else [],
            "categories": categories if len(categories) > 1 else [],
        }

    def get_dashboard_summary(
        self,
        date_range: str = "Last 7 Days",
        *,
        vendor: str = "",
        category: str = "",
        custom_start: str | date | None = None,
        custom_end: str | date | None = None,
        today: date | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        start, end = resolve_date_range(
            date_range,
            today=today,
            custom_start=custom_start,
            custom_end=custom_end,
        )
        prior_start, prior_end = _previous_range(start, end)
        key = (
            date_range,
            start.isoformat(),
            end.isoformat(),
            vendor.strip().casefold(),
            category.strip().casefold(),
        )
        cached = self._cache.get(key)
        if not force and cached and time.monotonic() - cached[0] <= self.cache_seconds:
            return cached[1]

        current = self._period_metrics(start, end, vendor=vendor, category=category)
        previous = self._period_metrics(prior_start, prior_end, vendor=vendor, category=category)
        sales_trend = self.get_sales_trend(start, end, vendor=vendor, category=category)
        margin_trend = self.get_margin_trend(start, end, vendor=vendor, category=category)
        breakdown = self.get_cost_breakdown(start, end, vendor=vendor, category=category)
        filter_options = self.get_filter_options()
        attention = self.get_attention_items(start, end)
        if current["labor_available"] and current["labor_percent"] > 100.0:
            attention.insert(
                0,
                {
                    "title": f"Labor cost is {current['labor_percent']:.1f}% of sales",
                    "detail": (
                        f"${current['labor_cost']:,.0f} labor against "
                        f"${current['sales']:,.0f} sales in the selected period."
                    ),
                    "severity": "High",
                    "source_type": "labor_metric",
                    "source_id": f"{start.isoformat()}:{end.isoformat()}",
                    "action": "reports",
                    "permission": "reports.view",
                },
            )
        priorities = {
            "attention": attention[:3],
            "watchlist": self.get_watchlist_items(start, end),
            "on_track": self.get_on_track_items(current),
            "tasks": self.get_today_tasks(),
        }
        operational_brief = self.get_operational_brief(start, end)
        setup_items = self.get_setup_progress()
        kpis = self.get_kpi_metrics(current, previous, sales_trend, margin_trend, start, end)
        sales_trend["change_text"] = kpis[0]["change_text"]
        sales_trend["change"] = kpis[0]["change"]
        sales_trend["direction"] = kpis[0]["direction"]
        settings = self.workspace.load_settings()
        restaurant_name = settings.get("restaurant_name") or self.workspace.root.name
        summary = {
            "restaurant_name": restaurant_name,
            "generated_at": datetime.now().isoformat(timespec="minutes"),
            "range": {
                "label": date_range,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "previous_start": prior_start.isoformat(),
                "previous_end": prior_end.isoformat(),
                "comparison_label": self._comparison_label(date_range, start, end),
            },
            "filters": {
                "vendor": vendor,
                "category": category,
                **filter_options,
            },
            "kpis": kpis,
            "sales_trend": sales_trend,
            "margin_trend": margin_trend,
            "cost_breakdown": breakdown,
            "priorities": priorities,
            "operational_brief": operational_brief,
            "setup_items": setup_items,
            "has_operational_data": bool(
                current["sales_available"]
                or current["product_cost_available"]
                or current["inventory_available"]
                or priorities["attention"]
                or priorities["tasks"]
            ),
            "calculation_notes": {
                "sales": current["sales_source"],
                "product_cost": current["product_cost_source"],
                "inventory": current["inventory_source"],
                "margin": (
                    "Gross margin is net sales less estimated product COGS. Product COGS uses finalized "
                    "inventory COGS when available, otherwise recipe-based theoretical usage when POS and "
                    "recipes are available, and only falls back to approved purchases when neither exists."
                ),
            },
        }
        summary["costpilot_context"] = self._costpilot_context(summary)
        self._cache[key] = (time.monotonic(), summary)
        return summary

    def get_operational_brief(self, start: date, end: date) -> dict[str, Any]:
        """Return the manager's daily operating brief from the existing ledger."""
        today = date.today()
        target_day = end if end <= today else today
        try:
            prior_year = target_day.replace(year=target_day.year - 1)
        except ValueError:
            prior_year = target_day.replace(year=target_day.year - 1, day=28)
        prior_rows, prior_source = self._sales_rows(prior_year, prior_year)
        prior_year_sales = sum(float(row["value"]) for row in prior_rows)
        today_rows, today_source = self._sales_rows(target_day, target_day)
        today_sales = sum(float(row["value"]) for row in today_rows)
        low_stock: list[dict[str, Any]] = []
        with self.workspace.connect() as conn:
            try:
                rows = conn.execute(
                    """SELECT i.item_id,i.item_name,i.vendor_name,i.category,i.count_unit,
                              i.estimated_on_hand,op.par_quantity_count_units,op.safety_stock_days,
                              op.average_daily_usage,op.current_price
                       FROM items i JOIN (
                           SELECT item_id,MAX(prediction_id) prediction_id
                           FROM order_predictions GROUP BY item_id
                       ) latest ON latest.item_id=i.item_id
                       JOIN order_predictions op ON op.prediction_id=latest.prediction_id
                       WHERE i.active=1 AND i.estimated_on_hand IS NOT NULL
                       ORDER BY CASE WHEN CAST(i.estimated_on_hand AS REAL) <=
                                      CAST(op.safety_stock_days AS REAL)*CAST(op.average_daily_usage AS REAL)
                                     THEN 0 ELSE 1 END,
                                CAST(i.estimated_on_hand AS REAL) ASC
                       LIMIT 8"""
                ).fetchall()
                for row in rows:
                    on_hand = _decimal(row["estimated_on_hand"])
                    daily = _decimal(row["average_daily_usage"])
                    safety_days = _decimal(row["safety_stock_days"])
                    threshold = daily * safety_days
                    if on_hand <= threshold and daily > 0:
                        low_stock.append({
                            "item_id": row["item_id"], "item_name": row["item_name"],
                            "vendor": row["vendor_name"] or "Unknown vendor", "category": row["category"] or "",
                            "on_hand": float(on_hand), "unit": row["count_unit"] or "each",
                            "threshold": float(threshold), "days_remaining": float(on_hand / daily) if daily else None,
                            "severity": "Critical" if on_hand <= 0 else "Warning",
                            "action": "inventory", "permission": "inventory.count",
                        })
            except sqlite3.Error:
                pass
            event_rows = conn.execute(
                """SELECT event_name,event_date,end_date,category,expected_sales_impact_percent,source
                   FROM local_events WHERE end_date>=? AND event_date<=?
                   ORDER BY event_date LIMIT 8""",
                (target_day.isoformat(), (target_day + timedelta(days=7)).isoformat()),
            ).fetchall()
            weather_rows = conn.execute(
                "SELECT * FROM weather_daily WHERE weather_date BETWEEN ? AND ? ORDER BY weather_date LIMIT 8",
                (target_day.isoformat(), (target_day + timedelta(days=7)).isoformat()),
            ).fetchall()
        events = [dict(row) for row in event_rows]
        forecast = None
        memory_learning: dict[str, Any] = {"factors_learned": 0, "par_recommendations": []}
        try:
            phase3 = getattr(self.pipeline, "phase3", None)
            if phase3:
                memory_learning = phase3.learn_from_actuals()
                forecast = phase3.forecast_sales(target_day)
        except Exception:
            forecast = None
        with self.workspace.connect() as conn:
            order_rows = conn.execute("""SELECT item_name,suggested_order_quantity,manager_order_quantity,current_price,estimated_order_cost,status
                FROM order_predictions WHERE prediction_id IN (SELECT MAX(prediction_id) FROM order_predictions GROUP BY item_id)
                AND CAST(COALESCE(suggested_order_quantity,'0') AS REAL)>0 ORDER BY CAST(COALESCE(estimated_order_cost,'0') AS REAL) DESC LIMIT 8""").fetchall()
        recommended_orders = [dict(row) for row in order_rows]
        holidays = self._us_holidays(target_day, target_day + timedelta(days=7))
        event_names = {str(row["event_name"]).casefold() for row in events}
        for holiday in holidays:
            if holiday["event_name"].casefold() not in event_names:
                events.append({**holiday, "category": "Holiday", "source": "US calendar", "expected_sales_impact_percent": 0})
        return {
            "today": target_day.isoformat(),
            "today_sales": today_sales,
            "today_sales_available": bool(today_rows),
            "today_sales_source": today_source,
            "same_day_last_year": prior_year_sales,
            "same_day_last_year_available": bool(prior_rows),
            "same_day_last_year_date": prior_year.isoformat(),
            "same_day_change_percent": _percent_change(today_sales, prior_year_sales),
            "low_stock": low_stock,
            "events": events[:8],
            "weather": [dict(row) for row in weather_rows],
            "sales_forecast": {
                "predicted_sales": float(forecast.predicted_sales) if forecast else None,
                "baseline_sales": float(forecast.baseline_sales) if forecast else None,
                "explanation": forecast.explanation if forecast else {},
            },
            "recommended_orders": recommended_orders,
            "memory": {
                "factors_learned": int(memory_learning.get("factors_learned", 0)),
                "par_recommendations": memory_learning.get("par_recommendations", []),
            },
        }

    @staticmethod
    def _us_holidays(start: date, end: date) -> list[dict[str, Any]]:
        """Small dependency-free US holiday calendar for operational planning."""
        def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
            d = date(year, month, 1)
            return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        def last_weekday(year: int, month: int, weekday: int) -> date:
            d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year + 1, 1, 1) - timedelta(days=1)
            return d - timedelta(days=(d.weekday() - weekday) % 7)
        def observed(d: date) -> date:
            return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d
        output: list[dict[str, Any]] = []
        for year in {start.year, end.year}:
            fixed = [("New Year's Day", date(year,1,1)), ("Juneteenth", date(year,6,19)), ("Independence Day", date(year,7,4)), ("Veterans Day", date(year,11,11)), ("Christmas Day", date(year,12,25))]
            holidays = fixed + [
                ("Martin Luther King Jr. Day", nth_weekday(year,1,0,3)),
                ("Presidents' Day", nth_weekday(year,2,0,3)),
                ("Memorial Day", last_weekday(year,5,0)),
                ("Labor Day", nth_weekday(year,9,0,1)),
                ("Columbus Day", nth_weekday(year,10,0,2)),
                ("Thanksgiving Day", nth_weekday(year,11,3,4)),
            ]
            for name, day in holidays:
                actual = observed(day)
                if start <= actual <= end:
                    output.append({"event_name": name, "event_date": actual.isoformat(), "end_date": actual.isoformat()})
        return sorted(output, key=lambda row: row["event_date"])

    def get_kpi_metrics(
        self,
        current: dict[str, Any],
        previous: dict[str, Any],
        sales_trend: dict[str, Any],
        margin_trend: dict[str, Any],
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        comparison = f"vs {self._short_previous_label(start, end)}"
        sales_change = _percent_change(current["sales"], previous["sales"])
        margin_change = (
            current["gross_margin_percent"] - previous["gross_margin_percent"]
            if current["gross_margin_available"] and previous["gross_margin_available"]
            else None
        )
        product_change = (
            current["product_cost_percent"] - previous["product_cost_percent"]
            if current["product_cost_percent_available"] and previous["product_cost_percent_available"]
            else None
        )
        inventory_change = _percent_change(current["inventory_value"], previous["inventory_value"])

        kpis = [
            self._kpi(
                "total_sales",
                "Total Sales",
                current["sales"],
                "currency",
                current["sales_available"],
                sales_change,
                comparison,
                True,
                sales_trend.get("values", []),
                "sales",
                "No POS or sales-summary data is available for this period.",
            ),
            self._kpi(
                "gross_margin",
                "Gross Margin %",
                current["gross_margin_percent"],
                "percent",
                current["gross_margin_available"],
                margin_change,
                comparison,
                True,
                margin_trend.get("actual", []),
                "reports",
                "No approved product costs in this period.",
                change_unit="points",
            ),
            self._kpi(
                "product_cost",
                current["product_cost_label"],
                current["product_cost_percent"],
                "percent",
                current["product_cost_percent_available"],
                product_change,
                comparison,
                False,
                current["product_cost_sparkline"],
                "reports",
                "No approved product costs in this period.",
                change_unit="points",
            ),
        ]

        total_cost_change = _percent_change(
            current["total_estimated_costs"], previous["total_estimated_costs"]
        )
        kpis.append(
            self._kpi(
                "estimated_costs",
                "Estimated Total Costs",
                current["total_estimated_costs"],
                "currency",
                current["sales_available"],
                total_cost_change,
                comparison,
                False,
                [current["total_estimated_costs"]],
                "reports",
                "No sales data is available for this period.",
                neutral_message=(
                    f"{current['total_cost_percent']:.1f}% of sales · "
                    f"COGS + waste + labor + operating costs"
                ),
            )
        )

        if current["labor_available"]:
            labor_change = (
                current["labor_percent"] - previous["labor_percent"]
                if previous["labor_available"]
                else None
            )
            labor_kpi = self._kpi(
                    "labor_cost",
                    "Labor Cost %",
                    current["labor_percent"],
                    "percent",
                    True,
                    labor_change,
                    comparison,
                    False,
                    current["labor_sparkline"],
                    "reports",
                    "",
                    change_unit="points",
                    neutral_message=(
                        f"${current['labor_cost']:,.0f} labor / "
                        f"${current['sales']:,.0f} sales"
                    ),
                )
            if current["labor_percent"] > 100.0:
                labor_kpi["direction"] = "bad"
                labor_kpi["change_text"] = (
                    f"${current['labor_cost']:,.0f} labor / "
                    f"${current['sales']:,.0f} sales"
                )
            kpis.append(labor_kpi)
        else:
            labor_change = (
                current["labor_percent"] - previous["labor_percent"]
                if previous["sales"] > 0 and current["sales"] > 0
                else None
            )
            kpis.append(
                self._kpi(
                    "labor_cost",
                    "Labor Cost %",
                    current["labor_percent"],
                    "percent",
                    bool(current["sales_available"]),
                    labor_change,
                    comparison,
                    False,
                    current["labor_sparkline"],
                    "reports",
                    "No sales data is available for labor estimation.",
                    change_unit="points",
                    neutral_message=(
                        f"Estimated at {current['estimated_labor_percent']:.1f}% of sales "
                        "until actual labor is imported."
                    ),
                )
            )

        kpis.append(
            self._kpi(
                "inventory_value",
                "Inventory Value",
                current["inventory_value"],
                "currency",
                current["inventory_available"],
                inventory_change,
                comparison,
                True,
                current["inventory_sparkline"],
                "inventory",
                "No finalized inventory valuation is available.",
            )
        )
        return kpis

    def _kpi(
        self,
        key: str,
        title: str,
        value: float,
        value_type: str,
        available: bool,
        change: float | None,
        comparison: str,
        higher_is_better: bool,
        sparkline: Iterable[float],
        action: str,
        empty_message: str,
        *,
        change_unit: str = "percent",
        neutral_message: str = "",
    ) -> dict[str, Any]:
        if not available:
            display = "—"
        elif value_type == "currency":
            display = f"${value:,.0f}"
        elif value_type == "percent":
            display = f"{value:.1f}%"
        else:
            display = f"{int(value):,}"
        if change is None:
            change_text = neutral_message or "Prior period unavailable"
            direction = "neutral"
        else:
            suffix = " pts" if change_unit == "points" else "%"
            change_text = f"{abs(change):.1f}{suffix} {comparison}"
            good = change >= 0 if higher_is_better else change <= 0
            direction = "good" if good else "bad"
        return {
            "key": key,
            "title": title,
            "value": value if available else None,
            "display": display,
            "available": available,
            "change": change,
            "change_text": change_text,
            "direction": direction,
            "sparkline": [float(value) for value in sparkline],
            "action": action,
            "empty_message": empty_message,
        }

    def get_sales_trend(
        self,
        start: date,
        end: date,
        *,
        vendor: str = "",
        category: str = "",
    ) -> dict[str, Any]:
        rows, source = self._sales_rows(start, end, category=category)
        values_by_date = {row["date"]: float(row["value"]) for row in rows}
        labels, values = self._series_for_range(start, end, values_by_date)
        available = bool(rows)
        return {
            "labels": labels,
            "values": values if available else [],
            "total": sum(values) if available else 0.0,
            "available": available,
            "source": source,
            "empty_message": "No sales data available for this period.",
            "action": "sales",
        }

    def get_margin_trend(
        self,
        start: date,
        end: date,
        *,
        vendor: str = "",
        category: str = "",
    ) -> dict[str, Any]:
        sales_rows, _source = self._sales_rows(start, end, category=category)
        cost_rows = self._product_cost_rows(start, end, vendor=vendor, category=category)
        sales = {row["date"]: float(row["value"]) for row in sales_rows}
        costs = {row["date"]: float(row["value"]) for row in cost_rows}
        actual = []
        labels = []
        days = (end - start).days + 1
        aggregation = "weekly" if days > 14 else "daily"
        if aggregation == "weekly":
            periods: list[tuple[date, date]] = []
            period_end = end
            while period_end >= start:
                period_start = max(start, period_end - timedelta(days=6))
                periods.append((period_start, period_end))
                period_end = period_start - timedelta(days=1)
            for period_start, period_end in reversed(periods):
                period_sales = sum(
                    value
                    for day, value in sales.items()
                    if period_start.isoformat() <= day <= period_end.isoformat()
                )
                if period_sales <= 0:
                    continue
                period_cost = sum(
                    value
                    for day, value in costs.items()
                    if period_start.isoformat() <= day <= period_end.isoformat()
                )
                has_period_cost = any(
                    period_start.isoformat() <= day <= period_end.isoformat()
                    for day in costs
                )
                if not has_period_cost:
                    continue
                labels.append(self._chart_period_label(period_start, period_end))
                actual.append((period_sales - period_cost) / period_sales * 100.0)
        else:
            for day in sorted(set(sales) | set(costs)):
                day_sales = sales.get(day, 0.0)
                if day_sales <= 0 or day not in costs:
                    continue
                labels.append(self._chart_date_label(day))
                actual.append((day_sales - costs.get(day, 0.0)) / day_sales * 100.0)
        target = 100.0 - float(
            _decimal(self.workspace.load_settings().get("target_menu_food_cost_percent", 30.0))
        )
        return {
            "labels": labels,
            "actual": actual,
            "target": [target] * len(actual),
            "target_value": target,
            "aggregation": aggregation,
            "available": bool(actual),
            "empty_message": "No margin history available for this period.",
            "action": "reports",
        }

    def get_cost_breakdown(
        self,
        start: date,
        end: date,
        *,
        vendor: str = "",
        category: str = "",
    ) -> dict[str, Any]:
        # The Overview cost chart reports economic cost, not cash purchases.
        # A large invoice can increase inventory without becoming COGS in the
        # same period, so purchases are shown separately in report details.
        sales_rows, _ = self._sales_rows(start, end, category=category)
        purchase_rows = self._product_cost_rows(start, end, vendor=vendor, category=category)
        product_cogs, product_source, _ = self._estimated_product_cost(
            start, end, vendor=vendor, category=category, purchase_rows=purchase_rows
        )
        waste = self._waste_cost(start, end) if not vendor and not category else 0.0
        operating = self._operating_cost_total(start, end) if not vendor and not category else 0.0
        labor_rows = self._labor_rows(start, end)
        sales_total = sum(float(row["value"]) for row in sales_rows)
        settings = self.workspace.load_settings()
        labor_percent = float(_decimal(settings.get("estimated_labor_percent", 30.0)))
        labor = sum(float(row["value"]) for row in labor_rows) if labor_rows else sales_total * labor_percent / 100.0
        components = [
            ("Product COGS", product_cogs),
            ("Waste / Spoilage", waste),
            ("Labor" + (" (Estimated)" if not labor_rows and sales_total else ""), labor),
            ("Operating Costs", operating),
        ]
        components = [(name, amount) for name, amount in components if amount > 0]
        grand_total = sum(amount for _name, amount in components)
        return {
            "items": [
                {"category": name, "amount": amount, "percent": amount / grand_total * 100.0 if grand_total else 0.0}
                for name, amount in components
            ],
            "total": grand_total,
            "available": bool(components),
            "empty_message": "No estimated cost data available for this period.",
            "action": "reports",
            "product_source": product_source,
            "purchase_spend": sum(float(row["value"]) for row in purchase_rows),
            "sales": sales_total,
        }

        totals: defaultdict[str, float] = defaultdict(float)
        invoice_where, invoice_params = self._invoice_filters(
            start, end, vendor=vendor, category=category, line_alias="l", invoice_alias="i"
        )
        with self.workspace.connect() as conn:
            rows = conn.execute(
                f"""SELECT COALESCE(NULLIF(TRIM(l.category),''),'Product') AS category,
                           COALESCE(SUM(CAST(l.line_total AS REAL)),0) AS total
                    FROM invoice_lines l
                    JOIN invoices i ON i.invoice_id=l.invoice_id
                    WHERE i.status='Approved' AND {invoice_where}
                    GROUP BY COALESCE(NULLIF(TRIM(l.category),''),'Product')""",
                invoice_params,
            ).fetchall()
            for row in rows:
                totals[self._display_category(row["category"])] += float(row["total"] or 0)

            if not vendor:
                where = ["cost_date>=?", "cost_date<=?"]
                params: list[Any] = [start.isoformat(), end.isoformat()]
                if category:
                    where.append("LOWER(category)=LOWER(?)")
                    params.append(category)
                for row in conn.execute(
                    f"""SELECT COALESCE(NULLIF(TRIM(category),''),'Operating Costs') AS category,
                               COALESCE(SUM(CAST(amount AS REAL)),0) AS total
                        FROM operating_costs WHERE {' AND '.join(where)}
                        GROUP BY COALESCE(NULLIF(TRIM(category),''),'Operating Costs')""",
                    params,
                ).fetchall():
                    totals[self._display_category(row["category"])] += float(row["total"] or 0)

                waste_where = ["event_date>=?", "event_date<=?"]
                waste_params: list[Any] = [start.isoformat(), end.isoformat()]
                if category and category.casefold() != "waste":
                    waste_where.append("1=0")
                waste = conn.execute(
                    f"SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) FROM waste_events WHERE {' AND '.join(waste_where)}",
                    waste_params,
                ).fetchone()[0]
                if float(waste or 0) > 0:
                    totals["Waste"] += float(waste)

        ordered = sorted(
            ((name, value) for name, value in totals.items() if value > 0),
            key=lambda item: (-item[1], item[0]),
        )
        grand_total = sum(value for _name, value in ordered)
        items = [
            {
                "category": name,
                "amount": value,
                "percent": value / grand_total * 100.0 if grand_total else 0.0,
            }
            for name, value in ordered
        ]
        return {
            "items": items,
            "total": grand_total,
            "available": bool(items),
            "empty_message": "No cost data available for this period.",
            "action": "reports",
        }

    def get_attention_items(self, start: date, end: date) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            cases = [_row_dict(row) for row in self.pipeline.list_costpilot_review_cases()]
        except Exception:
            cases = []
        for case in cases:
            severity = str(case.get("severity") or "Warning")
            items.append(
                {
                    "title": str(case.get("problem") or case.get("document_label") or "CostPilot review required"),
                    "detail": str(case.get("recommendation") or "Review this case."),
                    "severity": severity,
                    "source_type": str(case.get("case_type") or "review"),
                    "source_id": str(case.get("case_id") or ""),
                    "payload": {
                        "case_id": str(case.get("case_id") or ""),
                        "session_id": str(case.get("entity_id") or ""),
                        "invoice_id": str(case.get("document_id") or ""),
                        "vendor": str(case.get("vendor") or ""),
                        "document_label": str(case.get("document_label") or ""),
                    },
                    "action": "review",
                    "permission": "reviews.center",
                }
            )
        try:
            exceptions = [_row_dict(row) for row in self.pipeline.list_exceptions(limit=200)]
        except Exception:
            exceptions = []
        existing = {(item["source_type"], item["source_id"]) for item in items}
        for row in exceptions:
            severity = str(row.get("severity") or "Info")
            if SEVERITY_ORDER.get(severity, 9) > SEVERITY_ORDER["High"]:
                continue
            marker = (str(row.get("source_type") or "exception"), str(row.get("source_id") or row.get("exception_id") or ""))
            if marker in existing:
                continue
            items.append(
                {
                    "title": str(row.get("title") or "Operational exception"),
                    "detail": str(row.get("recommended_action") or row.get("message") or "Review this exception."),
                    "severity": severity,
                    "source_type": marker[0],
                    "source_id": marker[1],
                    "payload": self._json_dict(row.get("source_json")),
                    "action": "source",
                    "permission": "exceptions.view",
                }
            )
        items = self._consolidate_receiving_attention(items)
        items.sort(
            key=lambda item: (
                SEVERITY_ORDER.get(item["severity"], 9),
                0 if self._receiving_attention_key(item) else 1,
                item["title"].casefold(),
            )
        )
        return items[:3]

    def get_watchlist_items(self, start: date, end: date) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            exceptions = [_row_dict(row) for row in self.pipeline.list_exceptions(limit=200)]
        except Exception:
            exceptions = []
        for row in exceptions:
            severity = str(row.get("severity") or "Info")
            if SEVERITY_ORDER.get(severity, 9) < SEVERITY_ORDER["Warning"]:
                continue
            items.append(
                {
                    "title": str(row.get("title") or "Watch item"),
                    "detail": str(row.get("recommended_action") or row.get("message") or ""),
                    "severity": severity,
                    "source_type": str(row.get("source_type") or "exception"),
                    "source_id": str(row.get("source_id") or row.get("exception_id") or ""),
                    "payload": self._json_dict(row.get("source_json")),
                    "action": "source",
                    "permission": "exceptions.view",
                }
            )
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT price_id,item_id,item_description,vendor_name,price_change_percent
                   FROM price_history
                   WHERE price_alert=1 AND invoice_date>=? AND invoice_date<=?
                   ORDER BY ABS(CAST(price_change_percent AS REAL)) DESC LIMIT 12""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        seen = {item["title"].casefold() for item in items}
        for raw in rows:
            row = _row_dict(raw)
            change = float(row.get("price_change_percent") or 0)
            title = f"{row.get('item_description') or 'Item'} cost {'increased' if change >= 0 else 'decreased'} {abs(change):.1f}%"
            if title.casefold() in seen:
                continue
            items.append(
                {
                    "title": title,
                    "detail": f"Vendor: {row.get('vendor_name') or 'Unknown'}",
                    "severity": "Warning",
                    "source_type": "item",
                    "source_id": str(row.get("item_id") or row.get("price_id") or ""),
                    "action": "source",
                    "permission": "items.edit",
                }
            )
        items.sort(
            key=lambda item: (
                SEVERITY_ORDER.get(item["severity"], 9),
                item["title"].casefold(),
            )
        )
        return items[:3]

    def get_on_track_items(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        target = metrics["target_gross_margin_percent"]
        if metrics["gross_margin_available"] and metrics["gross_margin_percent"] >= target:
            items.append(
                {
                    "title": f"Gross margin is above the {target:.1f}% target",
                    "detail": f"Actual gross margin is {metrics['gross_margin_percent']:.1f}%.",
                    "severity": "Success",
                    "action": "reports",
                    "permission": "reports.view",
                }
            )
        target_cost = metrics["target_product_cost_percent"]
        if metrics["product_cost_percent_available"] and metrics["product_cost_percent"] <= target_cost:
            items.append(
                {
                    "title": f"{metrics['product_cost_label']} is within target",
                    "detail": f"Actual {metrics['product_cost_percent']:.1f}% vs {target_cost:.1f}% target.",
                    "severity": "Success",
                    "action": "reports",
                    "permission": "reports.view",
                }
            )
        with self.workspace.connect() as conn:
            receiving_total = conn.execute("SELECT COUNT(*) FROM receiving_sessions").fetchone()[0]
            receiving_open = conn.execute(
                "SELECT COUNT(*) FROM receiving_sessions WHERE status<>'Verified'"
            ).fetchone()[0]
            finalized_counts = conn.execute(
                "SELECT COUNT(*) FROM inventory_counts WHERE finalized=1"
            ).fetchone()[0]
        if receiving_total and not receiving_open:
            items.append(
                {
                    "title": "All recorded deliveries are verified",
                    "detail": f"{int(receiving_total)} receiving record(s) are complete.",
                    "severity": "Success",
                    "action": "receiving",
                    "permission": "receiving.verify",
                }
            )
        if finalized_counts:
            items.append(
                {
                    "title": "Finalized inventory counts are available",
                    "detail": f"{int(finalized_counts)} finalized item count record(s).",
                    "severity": "Success",
                    "action": "inventory",
                    "permission": "inventory.count",
                }
            )
        return items[:3]

    def get_today_tasks(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        upload_suffixes = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".json"}
        upload_folder = self.workspace.folders.get("upload")
        pending_uploads = (
            sum(
                1
                for path in upload_folder.iterdir()
                if path.is_file() and path.suffix.casefold() in upload_suffixes
            )
            if upload_folder and upload_folder.exists()
            else 0
        )
        if pending_uploads:
            tasks.append(
                {
                    "title": f"Process {pending_uploads} automatic-upload file(s)",
                    "detail": "Files in the restaurant upload folder are waiting for processing.",
                    "severity": "Medium",
                    "action": "invoice_intake",
                    "permission": "invoices.upload",
                }
            )
        try:
            review = self.pipeline.costpilot_review_summary()
        except Exception:
            review = {}
        if int(review.get("open") or 0):
            tasks.append(
                {
                    "title": f"Resolve {int(review.get('open') or 0)} CostPilot review case(s)",
                    "detail": "Open invoice and receiving cases are waiting for manager review.",
                    "severity": "High" if int(review.get("critical") or 0) else "Medium",
                    "action": "review",
                    "permission": "reviews.center",
                }
            )
        with self.workspace.connect() as conn:
            receiving = conn.execute(
                "SELECT COUNT(*) FROM receiving_sessions WHERE status<>'Verified'"
            ).fetchone()[0]
            purchase_orders = conn.execute(
                "SELECT COUNT(*) FROM purchase_orders WHERE status IN ('Draft','Pending Approval')"
            ).fetchone()[0]
            mobile_counts = conn.execute(
                "SELECT COUNT(*) FROM mobile_count_sessions WHERE status IN ('Open','Submitted')"
            ).fetchone()[0]
            recommendations = conn.execute(
                """SELECT recommendation_id,explanation,estimated_value,supporting_decision_ids_json
                   FROM margin_memory_recommendations
                   WHERE status IN ('Pending','Open','Generated')
                   ORDER BY confidence DESC,generated_at DESC LIMIT 1"""
            ).fetchall()
            pending_decisions = conn.execute(
                """SELECT decision_id,decision_type,subject_name,manager_note,status
                   FROM margin_memory_decisions
                   WHERE status IN ('Pending Outcome','Pending Review')
                   ORDER BY decision_time DESC LIMIT 1"""
            ).fetchall()
            item_reviews = int(conn.execute(
                "SELECT COUNT(*) FROM items WHERE review_status<>'Approved'"
            ).fetchone()[0])
        try:
            ready_to_close = sum(
                1
                for row in self.pipeline.planning.year_summary(date.today().year)
                if row.get("count_status") == "Open - count preview (not closed)"
                and str(row.get("period_start") or "") <= date.today().isoformat()
            )
        except Exception:
            ready_to_close = 0
        receiving_already_in_review = min(
            int(receiving or 0),
            int(review.get("receiving_cases") or 0),
        )
        remaining_receiving = max(0, int(receiving or 0) - receiving_already_in_review)
        if remaining_receiving:
            tasks.append(
                {
                    "title": f"Resolve {remaining_receiving} receiving exception(s)",
                    "detail": "Confirm quantities, shortages, substitutions, or credits.",
                    "severity": "High",
                    "action": "receiving",
                    "permission": "receiving.verify",
                }
            )
        if purchase_orders:
            tasks.append(
                {
                    "title": f"Review {int(purchase_orders)} purchase order(s)",
                    "detail": "Draft or pending purchase orders are waiting.",
                    "severity": "Medium",
                    "action": "orders",
                    "permission": "orders.edit",
                }
            )
        if mobile_counts:
            tasks.append(
                {
                    "title": f"Complete {int(mobile_counts)} inventory count(s)",
                    "detail": "Open or submitted counts still require completion.",
                    "severity": "Medium",
                    "action": "inventory",
                    "permission": "mobile_counts.manage",
                }
            )
        if item_reviews:
            tasks.append(
                {
                    "title": f"Configure {item_reviews} product(s)",
                    "detail": "New invoice products still need count-unit or planning confirmation.",
                    "severity": "Medium",
                    "action": "items",
                    "permission": "items.edit",
                }
            )
        if ready_to_close:
            tasks.append(
                {
                    "title": f"Review and close {ready_to_close} inventory month(s)",
                    "detail": "Imported beginning and ending counts are complete; calculations are already previewed.",
                    "severity": "Low",
                    "action": "inventory",
                    "permission": "inventory.count",
                }
            )
        if recommendations:
            row = _row_dict(recommendations[0])
            tasks.append(
                {
                    "title": "Review a MarginMemory recommendation",
                    "detail": str(row.get("explanation") or "Relevant prior decision evidence is available."),
                    "severity": "Low",
                    "action": "margin_memory",
                    "source_id": str(row.get("recommendation_id") or ""),
                    "permission": "margin_memory.view",
                }
            )
        elif pending_decisions:
            row = _row_dict(pending_decisions[0])
            subject = str(row.get("subject_name") or row.get("decision_type") or "manager decision")
            note = str(row.get("manager_note") or "").strip()
            tasks.append(
                {
                    "title": "Review pending MarginMemory evidence",
                    "detail": note or f"Outcome evidence is pending for {subject}.",
                    "severity": "Low",
                    "action": "margin_memory",
                    "source_id": str(row.get("decision_id") or ""),
                    "permission": "margin_memory.view",
                }
            )
        return tasks[:3]

    def get_setup_progress(self) -> list[dict[str, Any]]:
        settings = self.workspace.load_settings()
        with self.workspace.connect() as conn:
            sales = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            pos = conn.execute("SELECT COUNT(*) FROM pos_sales_lines").fetchone()[0]
            invoices = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            counts = conn.execute(
                "SELECT COUNT(*) FROM inventory_counts WHERE finalized=1"
            ).fetchone()[0]
            vendors = conn.execute("SELECT COUNT(*) FROM vendors WHERE recognized=1").fetchone()[0]
        steps = []
        if not sales and not pos:
            steps.append(
                {
                    "title": "Import sales data",
                    "detail": "Add a POS or sales summary to activate sales and margin trends.",
                    "action": "sales_import",
                    "permission": "sales.import",
                }
            )
        if not invoices:
            steps.append(
                {
                    "title": "Add invoices",
                    "detail": "Upload vendor invoices to calculate product cost.",
                    "action": "invoice_intake",
                    "permission": "invoices.upload",
                }
            )
        if not counts:
            steps.append(
                {
                    "title": "Complete the first inventory count",
                    "detail": "A finalized count activates current inventory valuation.",
                    "action": "inventory",
                    "permission": "inventory.count",
                }
            )
        if not vendors:
            steps.append(
                {
                    "title": "Configure a vendor",
                    "detail": "Recognized vendors improve invoice automation and filtering.",
                    "action": "settings",
                    "permission": "settings.view",
                }
            )
        if settings.get("target_menu_food_cost_percent") in (None, ""):
            steps.append(
                {
                    "title": "Set the target product cost",
                    "detail": "A target enables margin performance comparisons.",
                    "action": "settings",
                    "permission": "settings.view",
                }
            )
        return steps[:5]

    def _period_metrics(
        self,
        start: date,
        end: date,
        *,
        vendor: str = "",
        category: str = "",
    ) -> dict[str, Any]:
        sales_rows, sales_source = self._sales_rows(start, end, category=category)
        purchase_rows = self._product_cost_rows(start, end, vendor=vendor, category=category)
        sales = sum(float(row["value"]) for row in sales_rows)
        purchase_spend = sum(float(row["value"]) for row in purchase_rows)
        product_cost, product_source, estimated_product_rows = self._estimated_product_cost(
            start, end, vendor=vendor, category=category, purchase_rows=purchase_rows
        )
        waste = self._waste_cost(start, end) if not vendor and not category else 0.0
        labor_rows = self._labor_rows(start, end)
        labor_actual = sum(float(row["value"]) for row in labor_rows)
        settings = self.workspace.load_settings()
        labor_target = float(_decimal(settings.get("estimated_labor_percent", 30.0)))
        labor_estimated = sales * labor_target / 100.0
        labor_available = bool(labor_rows) and sales > 0
        labor = labor_actual if labor_available else labor_estimated
        operating_costs = self._operating_cost_total(start, end) if not vendor and not category else 0.0
        total_estimated_costs = product_cost + waste + operating_costs + labor
        gross_margin_available = bool(sales_rows) and product_cost >= 0
        gross_margin_percent = (sales - product_cost) / sales * 100.0 if gross_margin_available and sales else 0.0
        product_percent_available = gross_margin_available and sales > 0
        product_percent = product_cost / sales * 100.0 if product_percent_available else 0.0
        total_cost_percent = total_estimated_costs / sales * 100.0 if sales else 0.0
        inventory_value, inventory_source = self._inventory_value_as_of(end)
        inventory_previous_points = self._inventory_history(end, 7)
        exception_series = self._review_exception_series(start, end)
        target_cost = float(_decimal(settings.get("target_menu_food_cost_percent", 30.0)))
        return {
            "sales": sales,
            "sales_available": bool(sales_rows),
            "sales_source": sales_source,
            "purchase_spend": purchase_spend,
            "purchase_spend_source": "Approved invoice purchases",
            "product_cost": product_cost,
            "product_cost_available": gross_margin_available,
            "product_cost_source": product_source,
            "product_cost_label": "Product COGS %",
            "product_cost_percent": product_percent,
            "product_cost_percent_available": product_percent_available,
            "gross_margin_percent": gross_margin_percent,
            "gross_margin_available": gross_margin_available,
            "waste_cost": waste,
            "operating_costs": operating_costs,
            "labor_cost": labor,
            "labor_actual": labor_actual,
            "labor_percent": labor / sales * 100.0 if sales else 0.0,
            "labor_available": labor_available,
            "labor_estimated": not labor_available and sales > 0,
            "estimated_labor_percent": labor_target,
            "total_estimated_costs": total_estimated_costs,
            "total_cost_percent": total_cost_percent,
            "estimated_operating_profit": sales - total_estimated_costs,
            "inventory_value": inventory_value,
            "inventory_available": inventory_source != "Unavailable",
            "inventory_source": inventory_source,
            "review_exceptions": sum(exception_series.values()),
            "target_product_cost_percent": target_cost,
            "target_gross_margin_percent": 100.0 - target_cost,
            "product_cost_sparkline": self._ratio_series(
                sales_rows,
                estimated_product_rows,
                numerator_is_cost=True,
            ),
            "labor_sparkline": self._ratio_series(
                sales_rows,
                labor_rows or [{"date": row["date"], "value": sales * labor_target / 100.0} for row in sales_rows],
                numerator_is_cost=True,
            ),
            "exception_sparkline": list(exception_series.values()),
            "inventory_sparkline": inventory_previous_points,
        }

    def _estimated_product_cost(
        self,
        start: date,
        end: date,
        *,
        vendor: str = "",
        category: str = "",
        purchase_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[float, str, list[dict[str, Any]]]:
        """Estimate product COGS without confusing purchases with usage.

        Priority is physical inventory COGS, then recipe/theoretical COGS from
        POS sales, then purchase spend as an explicitly labeled fallback. A
        restaurant can buy $20k of product and still use only $10k in the period;
        purchases are cash/inventory movement, not automatically cost of sales.
        """
        purchase_rows = purchase_rows if purchase_rows is not None else self._product_cost_rows(
            start, end, vendor=vendor, category=category
        )
        if not vendor and not category:
            closed = self._closed_cogs(start, end)
            if closed is not None:
                return closed, "Finalized inventory COGS", self._rows_from_total(closed, start, end)
        theoretical: list[dict[str, Any]] = []
        try:
            menu_rows = self.pipeline.phase2.list_menu_costs(start.isoformat(), end.isoformat())
            for row in menu_rows:
                if category and str(row.get("category") or "").casefold() != category.casefold():
                    continue
                amount = float(row.get("theoretical_food_cost") or 0)
                if amount > 0:
                    theoretical.append({"date": end.isoformat(), "value": amount})
            # Aggregate the per-menu theoretical costs into one period total.
            theoretical_total = sum(float(row["value"]) for row in theoretical)
        except Exception:
            theoretical = self._theoretical_product_cost_rows(start, end, category=category)
            theoretical_total = sum(float(row["value"]) for row in theoretical)
        if theoretical_total > 0:
            return theoretical_total, "Recipe-based theoretical COGS from POS sales", self._rows_from_total(theoretical_total, start, end)
        purchases = sum(float(row["value"]) for row in purchase_rows)
        return purchases, "Approved invoice purchases (fallback estimate)", purchase_rows

    @staticmethod
    def _rows_from_total(total: float, start: date, end: date) -> list[dict[str, Any]]:
        if total <= 0:
            return []
        return [{"date": end.isoformat(), "value": float(total)}]

    def _theoretical_product_cost_rows(
        self,
        start: date,
        end: date,
        *,
        category: str = "",
    ) -> list[dict[str, Any]]:
        params: list[Any] = [start.isoformat(), end.isoformat()]
        category_where = ""
        if category:
            category_where = " AND LOWER(COALESCE(m.category,''))=LOWER(?)"
            params.append(category)
        with self.workspace.connect() as conn:
            rows = conn.execute(
                f"""SELECT s.business_date AS day,
                           COALESCE(SUM(
                               CAST(s.quantity AS REAL) *
                               CAST(r.quantity_count_units AS REAL) /
                               CASE WHEN CAST(r.yield_percent AS REAL)>0 THEN CAST(r.yield_percent AS REAL)/100.0 ELSE 1 END *
                               CAST(COALESCE(i.current_price,'0') AS REAL) /
                               CASE WHEN CAST(COALESCE(i.units_per_purchase_unit,'1') AS REAL)>0
                                    THEN CAST(COALESCE(i.units_per_purchase_unit,'1') AS REAL) ELSE 1 END
                           ),0) AS value
                    FROM pos_sales_lines s
                    JOIN recipe_ingredients r ON r.menu_item_id=s.menu_item_id
                    JOIN items i ON i.item_id=r.item_id
                    JOIN menu_items m ON m.menu_item_id=s.menu_item_id
                    WHERE s.business_date>=? AND s.business_date<=? {category_where}
                    GROUP BY s.business_date ORDER BY s.business_date""",
                params,
            ).fetchall()
        return [{"date": str(row["day"]), "value": float(row["value"] or 0)} for row in rows]

    def _waste_cost(self, start: date, end: date) -> float:
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) AS total FROM waste_events WHERE event_date>=? AND event_date<=?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return float(row["total"] or 0)

    def _operating_cost_total(self, start: date, end: date) -> float:
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS total FROM operating_costs WHERE cost_date>=? AND cost_date<=?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return float(row["total"] or 0)

    def _sales_rows(
        self,
        start: date,
        end: date,
        *,
        category: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        params: list[Any] = [start.isoformat(), end.isoformat()]
        category_join = ""
        category_where = ""
        if category:
            category_join = "LEFT JOIN menu_items m ON m.menu_item_id=p.menu_item_id"
            category_where = " AND LOWER(COALESCE(m.category,''))=LOWER(?)"
            params.append(category)
        with self.workspace.connect() as conn:
            pos = conn.execute(
                f"""SELECT p.business_date AS day,COALESCE(SUM(CAST(p.net_sales AS REAL)),0) AS value
                    FROM pos_sales_lines p {category_join}
                    WHERE p.business_date>=? AND p.business_date<=? {category_where}
                    GROUP BY p.business_date ORDER BY p.business_date""",
                params,
            ).fetchall()
            if pos:
                return (
                    [{"date": str(row["day"]), "value": float(row["value"] or 0)} for row in pos],
                    "Item-level POS sales",
                )
            if category:
                return [], "No category-level POS sales"
            summary_totals: dict[str, float] = defaultdict(float)
            for row in preferred_sales_rows(conn, start, end):
                summary_totals[str(row["period_end"])] += float(row["net_sales"] or 0)
        return (
            [{"date": day, "value": value} for day, value in sorted(summary_totals.items())],
            "Imported sales summaries",
        )

    def _product_cost_rows(
        self,
        start: date,
        end: date,
        *,
        vendor: str = "",
        category: str = "",
    ) -> list[dict[str, Any]]:
        where, params = self._invoice_filters(
            start, end, vendor=vendor, category=category, line_alias="l", invoice_alias="i"
        )
        with self.workspace.connect() as conn:
            rows = conn.execute(
                f"""SELECT i.invoice_date AS day,
                           COALESCE(SUM(CAST(l.line_total AS REAL)),0) AS value
                    FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
                    WHERE i.status='Approved' AND {where}
                    GROUP BY i.invoice_date ORDER BY i.invoice_date""",
                params,
            ).fetchall()
            if rows or category:
                return [{"date": str(row["day"]), "value": float(row["value"] or 0)} for row in rows]
            invoice_where = ["invoice_date>=?", "invoice_date<=?", "status='Approved'"]
            invoice_params: list[Any] = [start.isoformat(), end.isoformat()]
            if vendor:
                invoice_where.append("LOWER(vendor)=LOWER(?)")
                invoice_params.append(vendor)
            rows = conn.execute(
                f"""SELECT invoice_date AS day,COALESCE(SUM(CAST(total AS REAL)),0) AS value
                    FROM invoices WHERE {' AND '.join(invoice_where)}
                    GROUP BY invoice_date ORDER BY invoice_date""",
                invoice_params,
            ).fetchall()
        return [{"date": str(row["day"]), "value": float(row["value"] or 0)} for row in rows]

    def _labor_rows(self, start: date, end: date) -> list[dict[str, Any]]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT cost_date AS day,COALESCE(SUM(CAST(amount AS REAL)),0) AS value
                   FROM operating_costs
                   WHERE cost_date>=? AND cost_date<=?
                     AND (LOWER(category) LIKE '%labor%' OR LOWER(category) LIKE '%payroll%'
                          OR LOWER(category) LIKE '%wage%')
                   GROUP BY cost_date ORDER BY cost_date""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [{"date": str(row["day"]), "value": float(row["value"] or 0)} for row in rows]

    def _closed_cogs(self, start: date, end: date) -> float | None:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT estimated_cogs FROM monthly_closes
                   WHERE period_start>=? AND period_end<=? AND count_status<>'Open - purchase estimate only'""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        if not rows:
            return None
        return sum(float(row["estimated_cogs"] or 0) for row in rows)

    def _inventory_value_as_of(self, as_of: date) -> tuple[float, str]:
        with self.workspace.connect() as conn:
            row = conn.execute(
                """WITH ranked AS (
                       SELECT inventory_value,
                              ROW_NUMBER() OVER (
                                  PARTITION BY item_id
                                  ORDER BY count_date DESC,count_id DESC
                              ) AS row_rank
                       FROM inventory_counts
                       WHERE finalized=1 AND count_date<=?
                   )
                   SELECT COALESCE(SUM(CAST(inventory_value AS REAL)),0) AS value,
                          COUNT(*) AS row_count
                   FROM ranked
                   WHERE row_rank=1""",
                (as_of.isoformat(),),
            ).fetchone()
        if row and int(row["row_count"] or 0):
            return float(row["value"] or 0), "Latest finalized count per item"
        return 0.0, "Unavailable"

    def _inventory_history(self, as_of: date, points: int) -> list[float]:
        with self.workspace.connect() as conn:
            dates = [
                str(row[0])
                for row in conn.execute(
                    """SELECT DISTINCT count_date FROM inventory_counts
                       WHERE finalized=1 AND count_date<=?
                       ORDER BY count_date DESC LIMIT ?""",
                    (as_of.isoformat(), points),
                ).fetchall()
            ]
            values = []
            for day in reversed(dates):
                value = conn.execute(
                    """WITH ranked AS (
                           SELECT inventory_value,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY item_id
                                      ORDER BY count_date DESC,count_id DESC
                                  ) AS row_rank
                           FROM inventory_counts
                           WHERE finalized=1 AND count_date<=?
                       )
                       SELECT COALESCE(SUM(CAST(inventory_value AS REAL)),0)
                       FROM ranked
                       WHERE row_rank=1""",
                    (day,),
                ).fetchone()[0]
                values.append(float(value or 0))
        return values

    def _review_exception_series(self, start: date, end: date) -> dict[str, int]:
        days = (end - start).days + 1
        if days > 60:
            start = end - timedelta(days=59)
        series = {
            (start + timedelta(days=offset)).isoformat(): 0
            for offset in range((end - start).days + 1)
        }
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT SUBSTR(created_at,1,10) AS day,COUNT(*) AS value
                   FROM reviews WHERE status='Open' AND SUBSTR(created_at,1,10)>=? AND SUBSTR(created_at,1,10)<=?
                   GROUP BY SUBSTR(created_at,1,10)""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        for row in rows:
            if row["day"] in series:
                series[str(row["day"])] = int(row["value"] or 0)
        return series

    def _invoice_filters(
        self,
        start: date,
        end: date,
        *,
        vendor: str,
        category: str,
        line_alias: str,
        invoice_alias: str,
    ) -> tuple[str, list[Any]]:
        where = [f"{invoice_alias}.invoice_date>=?", f"{invoice_alias}.invoice_date<=?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if vendor:
            where.append(f"LOWER({invoice_alias}.vendor)=LOWER(?)")
            params.append(vendor)
        if category:
            where.append(f"LOWER(COALESCE({line_alias}.category,''))=LOWER(?)")
            params.append(category)
        return " AND ".join(where), params

    def _ratio_series(
        self,
        sales_rows: Iterable[dict[str, Any]],
        numerator_rows: Iterable[dict[str, Any]],
        *,
        numerator_is_cost: bool,
    ) -> list[float]:
        sales = {row["date"]: float(row["value"]) for row in sales_rows}
        numerator = {row["date"]: float(row["value"]) for row in numerator_rows}
        result = []
        for day in sorted(sales):
            if sales[day] <= 0:
                continue
            ratio = numerator.get(day, 0.0) / sales[day] * 100.0
            result.append(ratio if numerator_is_cost else 100.0 - ratio)
        return result

    def _series_for_range(
        self,
        start: date,
        end: date,
        values_by_date: dict[str, float],
    ) -> tuple[list[str], list[float]]:
        days = (end - start).days + 1
        if days <= 31:
            labels = []
            values = []
            for offset in range(days):
                day = start + timedelta(days=offset)
                labels.append(self._chart_date_label(day.isoformat()))
                values.append(values_by_date.get(day.isoformat(), 0.0))
            return labels, values
        return (
            [self._chart_date_label(day) for day in sorted(values_by_date)],
            [values_by_date[day] for day in sorted(values_by_date)],
        )

    def _costpilot_context(self, summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "date_range": summary["range"],
            "restaurant": summary["restaurant_name"],
            "filters": {
                "vendor": summary["filters"]["vendor"],
                "category": summary["filters"]["category"],
            },
            "kpis": [
                {
                    "name": item["title"],
                    "value": item["display"],
                    "change": item["change_text"],
                    "available": item["available"],
                }
                for item in summary["kpis"]
            ],
            "exceptions": [
                {"title": item["title"], "severity": item["severity"]}
                for item in summary["priorities"]["attention"]
            ],
            "watchlist": [item["title"] for item in summary["priorities"]["watchlist"]],
            "tasks": [item["title"] for item in summary["priorities"]["tasks"]],
            "margin_memory": [
                {"title": item["title"], "detail": item.get("detail", "")}
                for item in summary["priorities"]["tasks"]
                if item.get("action") == "margin_memory"
            ],
            "cost_breakdown": summary["cost_breakdown"]["items"][:8],
            "operational_brief": summary.get("operational_brief", {}),
        }

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _consolidate_receiving_attention(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Show one actionable task while retaining every related source record."""
        standalone: list[dict[str, Any]] = []
        receiving_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            key = self._receiving_attention_key(item)
            if key:
                receiving_groups[key].append(item)
            else:
                standalone.append(item)

        for related in receiving_groups.values():
            primary = min(
                related,
                key=lambda item: (
                    item.get("action") != "review",
                    SEVERITY_ORDER.get(str(item.get("severity") or "Info"), 9),
                    str(item.get("title") or "").casefold(),
                ),
            ).copy()
            primary["severity"] = min(
                (str(item.get("severity") or "Info") for item in related),
                key=lambda value: SEVERITY_ORDER.get(value, 9),
            )
            primary["related_count"] = len(related)
            primary["related_sources"] = [
                {
                    "source_type": str(item.get("source_type") or ""),
                    "source_id": str(item.get("source_id") or ""),
                    "action": str(item.get("action") or ""),
                    "title": str(item.get("title") or ""),
                    "payload": dict(item.get("payload") or {}),
                }
                for item in related
            ]
            if len(related) > 1:
                primary["detail"] = (
                    str(primary.get("detail") or "Review this receiving issue.").rstrip()
                    + f" {len(related)} related receiving records are consolidated in this task."
                )
            standalone.append(primary)
        return standalone

    @staticmethod
    def _receiving_attention_key(item: dict[str, Any]) -> str:
        payload = dict(item.get("payload") or {})
        source_type = str(item.get("source_type") or "").casefold()
        searchable = " ".join(
            (
                str(item.get("title") or ""),
                str(item.get("detail") or ""),
                str(payload.get("receiving_status") or ""),
            )
        ).casefold()
        receiving_related = (
            source_type == "receiving"
            or bool(payload.get("session_id"))
            or bool(payload.get("receiving_status"))
            or any(word in searchable for word in ("receiving", "delivery", "received less", "shortage"))
        )
        if not receiving_related:
            return ""
        invoice_id = str(
            payload.get("invoice_id")
            or payload.get("document_id")
            or ""
        ).strip()
        if invoice_id:
            return f"invoice:{invoice_id.casefold()}"
        session_id = str(
            payload.get("session_id")
            or payload.get("entity_id")
            or (item.get("source_id") if source_type == "receiving" else "")
            or ""
        ).strip()
        if session_id:
            return f"session:{session_id.casefold()}"
        return ""

    @staticmethod
    def _display_category(value: Any) -> str:
        text = str(value or "Other").strip()
        aliases = {
            "labor cost": "Labor",
            "payroll": "Labor",
            "paper & packaging": "Paper & Packaging",
            "packaging": "Paper & Packaging",
        }
        return aliases.get(text.casefold(), text.title())

    @staticmethod
    def _chart_date_label(value: str) -> str:
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            return value
        return f"{parsed.strftime('%b')} {parsed.day}"

    @classmethod
    def _chart_period_label(cls, start: date, end: date) -> str:
        if start == end:
            return cls._chart_date_label(start.isoformat())
        if start.month == end.month:
            return f"{start.strftime('%b')} {start.day}-{end.day}"
        return (
            f"{cls._chart_date_label(start.isoformat())}-"
            f"{cls._chart_date_label(end.isoformat())}"
        )

    @staticmethod
    def _comparison_label(selection: str, start: date, end: date) -> str:
        if selection == "Today":
            return "vs yesterday"
        if selection == "Yesterday":
            return "vs prior day"
        if selection == "Last 7 Days":
            return "vs prior 7 days"
        if selection == "Last 30 Days":
            return "vs prior 30 days"
        if selection in {"This Month", "Last Month"}:
            return "vs prior equivalent period"
        days = (end - start).days + 1
        return f"vs prior {days} days"

    @staticmethod
    def _short_previous_label(start: date, end: date) -> str:
        days = (end - start).days + 1
        return "prior day" if days == 1 else f"prior {days} days"
