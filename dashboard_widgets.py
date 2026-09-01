"""Reusable Tkinter widgets for the MarginMise Overview dashboard."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

FigureCanvasTkAgg = None
Figure = None
MATPLOTLIB_AVAILABLE = False


def _load_matplotlib() -> bool:
    """Load charting only when the dashboard has chart data to render."""
    global FigureCanvasTkAgg, Figure, MATPLOTLIB_AVAILABLE
    if MATPLOTLIB_AVAILABLE:
        return True
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as canvas_type
        from matplotlib.figure import Figure as figure_type
    except ImportError:
        return False
    FigureCanvasTkAgg = canvas_type
    Figure = figure_type
    MATPLOTLIB_AVAILABLE = True
    return True

from src.theme import (
    BORDER_COLOR,
    BURGUNDY,
    CARD_BG_COLOR,
    CHARCOAL,
    CHART_COLORS,
    ERROR,
    FIRE_ORANGE,
    FONT_FAMILY,
    FROST_WHITE,
    LIGHT_SLATE,
    OCEAN_TEAL,
    PRIMARY_NAVY,
    SLATE,
    SUBTLE_GRID,
    SUCCESS,
    WARNING,
    WHITE,
)


def _rounded_rectangle(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    **kwargs: Any,
) -> int:
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class Card(tk.Canvas):
    """Canvas-backed card with a subtle rounded border and a normal Tk body."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        height: int,
        accent: str | None = None,
        background: str = CARD_BG_COLOR,
    ):
        super().__init__(
            parent,
            height=height,
            bg=FROST_WHITE,
            bd=0,
            highlightthickness=0,
        )
        self._height = height
        self._accent = accent
        self._card_background = background
        self.body = tk.Frame(self, bg=background, padx=12, pady=6)
        self._body_window = self.create_window(8, 7, anchor="nw", window=self.body)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event: tk.Event) -> None:
        width = max(40, event.width)
        height = max(40, event.height)
        self.delete("card-shape")
        _rounded_rectangle(
            self,
            2,
            2,
            width - 2,
            height - 2,
            10,
            fill=self._card_background,
            outline=self._accent or BORDER_COLOR,
            width=2 if self._accent else 1,
            tags=("card-shape",),
        )
        self.tag_lower("card-shape")
        self.coords(self._body_window, 9, 8)
        self.itemconfigure(
            self._body_window,
            width=max(20, width - 18),
            height=max(20, height - 16),
        )


class Sparkline(tk.Canvas):
    def __init__(self, parent: tk.Misc, values: list[float], color: str = OCEAN_TEAL):
        super().__init__(
            parent,
            height=18,
            bg=CARD_BG_COLOR,
            bd=0,
            highlightthickness=0,
        )
        self.values = values
        self.color = color
        self.bind("<Configure>", self._draw)

    def _draw(self, event: tk.Event | None = None) -> None:
        self.delete("all")
        if len(self.values) < 2:
            self.create_line(4, 22, max(5, self.winfo_width() - 4), 22, fill=SUBTLE_GRID)
            return
        width = max(20, self.winfo_width())
        height = max(20, self.winfo_height())
        minimum = min(self.values)
        maximum = max(self.values)
        span = maximum - minimum or 1.0
        points = []
        for index, value in enumerate(self.values):
            x = 4 + index * (width - 8) / max(1, len(self.values) - 1)
            y = height - 5 - (value - minimum) / span * (height - 12)
            points.extend((x, y))
        self.create_line(*points, fill=self.color, width=2, smooth=True)


class DashboardView:
    """Responsive Overview page renderer.

    ``navigate`` receives an action key and, optionally, a dashboard item.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        navigate: Callable[[str, dict[str, Any] | None], None],
        refresh: Callable[[], None],
        date_changed: Callable[[str], None],
        open_filters: Callable[[], None],
        ask_costpilot: Callable[[], None],
    ):
        self.parent = parent
        self.navigate = navigate
        self.refresh_callback = refresh
        self.date_changed_callback = date_changed
        self.open_filters_callback = open_filters
        self.ask_costpilot_callback = ask_costpilot
        self.summary: dict[str, Any] | None = None
        self._figures: list[Any] = []
        self._chart_canvases: list[Any] = []
        self._layout_signature: tuple[int, int, int] | None = None
        self._resize_job: str | None = None

        for child in parent.winfo_children():
            child.destroy()
        if isinstance(parent, (tk.Frame, tk.Canvas)):
            parent.configure(bg=FROST_WHITE)

        self.canvas = tk.Canvas(parent, bg=FROST_WHITE, bd=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.content = tk.Frame(self.canvas, bg=FROST_WHITE, padx=14, pady=8)
        self.content_window = self.canvas.create_window((0, 0), anchor="nw", window=self.content)
        self.content.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind_all("<MouseWheel>", self._mousewheel, add="+")

        self._build_header()
        self.setup_section = tk.Frame(self.content, bg=FROST_WHITE)
        self.brief_section = tk.Frame(self.content, bg=FROST_WHITE)
        self.kpi_section = tk.Frame(self.content, bg=FROST_WHITE)
        self.analytics_section = tk.Frame(self.content, bg=FROST_WHITE)
        self.priority_section = tk.Frame(self.content, bg=FROST_WHITE)
        self.footer = tk.Frame(self.content, bg=FROST_WHITE)

        self.kpi_section.pack(fill="x", pady=(5, 6))
        self.analytics_section.pack(fill="x", pady=(0, 6))
        self.priority_section.pack(fill="x", pady=(0, 4))
        self.footer.pack(fill="x", pady=(0, 3))

        self.kpi_cards: list[Card] = []
        self.analytics_cards: list[Card] = []
        self.priority_cards: list[Card] = []
        self.last_updated_var = tk.StringVar(value="")
        tk.Label(
            self.footer,
            textvariable=self.last_updated_var,
            bg=FROST_WHITE,
            fg=SLATE,
            font=(FONT_FAMILY, 9),
        ).pack(side="right")

    def destroy(self) -> None:
        self._clear_figures()
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass

    def _build_header(self) -> None:
        header = tk.Frame(self.content, bg=FROST_WHITE)
        header.pack(fill="x", pady=(0, 4))
        left = tk.Frame(header, bg=FROST_WHITE)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(
            left,
            text="Overview",
            bg=FROST_WHITE,
            fg=PRIMARY_NAVY,
            font=(FONT_FAMILY, 21, "bold"),
        ).pack(anchor="w")
        self.welcome_var = tk.StringVar(value="Select a restaurant to see its operating overview.")
        tk.Label(
            left,
            textvariable=self.welcome_var,
            bg=FROST_WHITE,
            fg=SLATE,
            font=(FONT_FAMILY, 10),
        ).pack(anchor="w", pady=(2, 0))

        controls = tk.Frame(header, bg=FROST_WHITE)
        controls.pack(side="right", anchor="ne", padx=(12, 0))
        self.date_range_var = tk.StringVar(value="Last 7 Days")
        self.date_combo = ttk.Combobox(
            controls,
            textvariable=self.date_range_var,
            values=(
                "Today",
                "Yesterday",
                "Last 7 Days",
                "Last 30 Days",
                "This Month",
                "Last Month",
                "Custom Range",
            ),
            state="readonly",
            width=16,
        )
        self.date_combo.pack(side="left", ipady=4)
        self.date_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.date_changed_callback(self.date_range_var.get()),
        )
        self.filter_button = tk.Button(
            controls,
            text="☰  Filters",
            command=self.open_filters_callback,
            bg=PRIMARY_NAVY,
            fg=WHITE,
            activebackground=OCEAN_TEAL,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
            font=(FONT_FAMILY, 9, "bold"),
        )
        self.filter_button.pack(side="left", padx=(8, 0))
        tk.Button(
            controls,
            text="↻",
            command=self.refresh_callback,
            bg=FROST_WHITE,
            fg=OCEAN_TEAL,
            activebackground=WHITE,
            activeforeground=PRIMARY_NAVY,
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            cursor="hand2",
            font=(FONT_FAMILY, 13, "bold"),
        ).pack(side="left", padx=(3, 0))

    def set_empty(self, message: str = "Select or add a restaurant workspace.") -> None:
        self.summary = None
        self.welcome_var.set(message)
        self._clear_section(self.setup_section)
        self._clear_section(self.brief_section)
        self._clear_section(self.kpi_section)
        self._clear_section(self.analytics_section)
        self._clear_section(self.priority_section)
        self.setup_section.pack(fill="x", pady=(8, 12), before=self.footer)
        card = Card(self.setup_section, height=150, accent=OCEAN_TEAL)
        card.pack(fill="x")
        tk.Label(
            card.body,
            text="Welcome to MarginMise",
            bg=WHITE,
            fg=PRIMARY_NAVY,
            font=(FONT_FAMILY, 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            card.body,
            text=message,
            bg=WHITE,
            fg=SLATE,
            font=(FONT_FAMILY, 10),
            wraplength=720,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))
        self.last_updated_var.set("")

    def render(
        self,
        summary: dict[str, Any],
        *,
        first_name: str,
        multi_location: bool,
    ) -> None:
        self.summary = summary
        plural = "restaurants" if multi_location else "restaurant"
        self.welcome_var.set(
            f"Welcome back, {first_name}. Here’s what’s happening across your {plural} today."
        )
        self.date_range_var.set(summary["range"]["label"])
        active_filters = [
            value
            for value in (summary["filters"].get("vendor"), summary["filters"].get("category"))
            if value
        ]
        self.filter_button.configure(
            text=f"☰  Filters ({len(active_filters)})" if active_filters else "☰  Filters"
        )
        self._clear_figures()
        self._clear_section(self.setup_section)
        self._clear_section(self.brief_section)
        self._clear_section(self.kpi_section)
        self._clear_section(self.analytics_section)
        self._clear_section(self.priority_section)
        self.kpi_cards = []
        self.analytics_cards = []
        self.priority_cards = []

        first_run = not summary["has_operational_data"] and bool(summary["setup_items"])
        if first_run:
            self.kpi_section.pack_forget()
            self.analytics_section.pack_forget()
            self.setup_section.pack(fill="x", pady=(8, 12), before=self.priority_section)
            self._render_setup(summary["setup_items"])
        else:
            self.setup_section.pack_forget()
            self.brief_section.pack(fill="x", pady=(4, 8), before=self.kpi_section)
            if not self.analytics_section.winfo_manager():
                self.analytics_section.pack(fill="x", pady=(0, 6), before=self.priority_section)
            if not self.kpi_section.winfo_manager():
                self.kpi_section.pack(fill="x", pady=(5, 6), before=self.analytics_section)
            self._render_brief(summary.get("operational_brief", {}))
            self._render_kpis(summary["kpis"])
            self._render_analytics(summary)
        self._render_priorities(summary["priorities"])
        generated = summary.get("generated_at", "").replace("T", " ")
        self.last_updated_var.set(f"Last updated: {generated}")
        self._layout_signature = None
        self._apply_responsive_layout(max(600, self.canvas.winfo_width()))

    def _render_setup(self, items: list[dict[str, Any]]) -> None:
        tk.Label(
            self.setup_section,
            text="Finish setting up your overview",
            bg=FROST_WHITE,
            fg=PRIMARY_NAVY,
            font=(FONT_FAMILY, 15, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for index, item in enumerate(items):
            card = Card(self.setup_section, height=116, accent=FIRE_ORANGE)
            card.grid(row=1 + index // 2, column=index % 2, sticky="nsew", padx=4, pady=4)
            tk.Label(
                card.body,
                text=f"{index + 1}.  {item['title']}",
                bg=WHITE,
                fg=PRIMARY_NAVY,
                font=(FONT_FAMILY, 11, "bold"),
            ).pack(anchor="w")
            label = tk.Label(
                card.body,
                text=item["detail"],
                bg=WHITE,
                fg=SLATE,
                font=(FONT_FAMILY, 9),
                wraplength=480,
                justify="left",
                cursor="hand2",
            )
            label.pack(anchor="w", pady=(5, 0))
            self._bind_click(card.body, lambda _e, row=item: self.navigate(row["action"], row))
        self.setup_section.columnconfigure(0, weight=1)
        self.setup_section.columnconfigure(1, weight=1)

    def _render_brief(self, brief: dict[str, Any]) -> None:
        title = tk.Frame(self.brief_section, bg=FROST_WHITE)
        title.pack(fill="x", pady=(0, 4))
        tk.Label(title, text="Daily Operating Brief", bg=FROST_WHITE, fg=PRIMARY_NAVY,
                 font=(FONT_FAMILY, 14, "bold")).pack(side="left")
        tk.Label(title, text="Sales, inventory, conditions and decisions that can change today's operation",
                 bg=FROST_WHITE, fg=SLATE, font=(FONT_FAMILY, 8)).pack(side="left", padx=8)
        grid = tk.Frame(self.brief_section, bg=FROST_WHITE)
        grid.pack(fill="x")
        cards = []
        today_sales = brief.get("today_sales")
        last_year = brief.get("same_day_last_year")
        change = brief.get("same_day_change_percent")
        sales_text = "—" if not brief.get("today_sales_available") else f"${today_sales:,.0f}"
        comparison = "No same-day prior-year data" if not brief.get("same_day_last_year_available") else f"${last_year:,.0f} same day last year"
        if change is not None:
            comparison += f" · {'▲' if change >= 0 else '▼'} {abs(change):.1f}%"
        cards.append(("TODAY'S SALES", sales_text, comparison, OCEAN_TEAL))
        weather = brief.get("weather", [])
        if weather:
            w = weather[0]
            weather_text = f"{w.get('temperature_max_f', 0):.0f}° / {w.get('temperature_min_f', 0):.0f}°F"
            rain = w.get("precipitation_probability")
            weather_sub = f"Rain chance {rain:.0f}%" if rain is not None else "Forecast available"
        else:
            weather_text, weather_sub = "—", "Weather not yet synced"
        cards.append(("TODAY'S WEATHER", weather_text, weather_sub, OCEAN_TEAL))
        low = brief.get("low_stock", [])
        low_text = str(len(low)) if low else "0"
        low_sub = "critical/low items" if low else "No low-stock alerts"
        cards.append(("LOW STOCK", low_text, low_sub, ERROR if low else SUCCESS))
        events = brief.get("events", [])
        event_text = str(len(events)) if events else "0"
        event_sub = (str(events[0].get("event_name") or "Upcoming event")[:42] if events else "No events in next 7 days")
        cards.append(("EVENTS / HOLIDAYS", event_text, event_sub, FIRE_ORANGE if events else SUCCESS))
        for index, (label, value, sub, accent) in enumerate(cards):
            card = Card(grid, height=92, accent=accent)
            card.grid(row=0, column=index, sticky="nsew", padx=3)
            tk.Label(card.body, text=label, bg=WHITE, fg=SLATE, font=(FONT_FAMILY, 7, "bold")).pack(anchor="w")
            tk.Label(card.body, text=value, bg=WHITE, fg=PRIMARY_NAVY, font=(FONT_FAMILY, 15, "bold")).pack(anchor="w", pady=(1,0))
            tk.Label(card.body, text=sub, bg=WHITE, fg=SLATE, font=(FONT_FAMILY, 7), wraplength=205, justify="left").pack(anchor="w")
            grid.columnconfigure(index, weight=1)
        predictive = brief.get("sales_forecast") or {}
        predicted = predictive.get("predicted_sales")
        forecast_frame = tk.Frame(self.brief_section, bg=FROST_WHITE)
        forecast_frame.pack(fill="x", pady=(6, 0))
        forecast_text = f"TODAY'S PLAN: Forecast sales ${predicted:,.0f}" if predicted is not None else "TODAY'S PLAN: Forecast not available yet"
        orders = brief.get("recommended_orders") or []
        if orders:
            order_spend = sum(float(x.get("estimated_order_cost") or 0) for x in orders)
            forecast_text += f" · {len(orders)} products recommended for ordering · ${order_spend:,.0f} estimated spend"
        memory = brief.get("memory") or {}
        par_recs = memory.get("par_recommendations") or []
        if par_recs:
            forecast_text += f" · MarginMemory recommends reviewing {len(par_recs)} par level{'s' if len(par_recs) != 1 else ''}"
        tk.Label(forecast_frame, text=forecast_text, bg=WHITE, fg=PRIMARY_NAVY,
                 font=(FONT_FAMILY, 8, "bold"), anchor="w", wraplength=1000, padx=8, pady=6).pack(fill="x")
        low_frame = tk.Frame(self.brief_section, bg=FROST_WHITE)
        low_frame.pack(fill="x", pady=(5,0))
        if low:
            text = "LOW STOCK: " + " · ".join(f"{x['item_name']} ({x['on_hand']:.1f} {x['unit']}, ~{x['days_remaining']:.1f} days)" for x in low[:4])
            tk.Label(low_frame, text=text, bg=WHITE, fg=ERROR, font=(FONT_FAMILY, 8, "bold"), anchor="w", wraplength=1000, padx=8, pady=5).pack(fill="x")
        elif events:
            text = "UPCOMING: " + " · ".join(f"{x.get('event_date','')}: {x.get('event_name','')}" for x in events[:4])
            tk.Label(low_frame, text=text, bg=WHITE, fg=CHARCOAL, font=(FONT_FAMILY, 8), anchor="w", wraplength=1000, padx=8, pady=5).pack(fill="x")

    def _render_kpis(self, kpis: list[dict[str, Any]]) -> None:
        for item in kpis:
            accent = {
                "gross_margin": OCEAN_TEAL,
                "product_cost": BURGUNDY,
                "labor_cost": BURGUNDY,
                "inventory_value": FIRE_ORANGE,
            }.get(item["key"], OCEAN_TEAL)
            card = Card(self.kpi_section, height=126)
            self.kpi_cards.append(card)
            top = tk.Frame(card.body, bg=WHITE)
            top.pack(fill="x")
            tk.Label(
                top,
                text="●",
                bg=WHITE,
                fg=accent,
                font=(FONT_FAMILY, 12, "bold"),
            ).pack(side="left")
            tk.Label(
                top,
                text=item["title"],
                bg=WHITE,
                fg=CHARCOAL,
                font=(FONT_FAMILY, 9, "bold"),
            ).pack(side="left", padx=(6, 0))
            tk.Label(
                card.body,
                text=item["display"],
                bg=WHITE,
                fg=PRIMARY_NAVY,
                font=(FONT_FAMILY, 17, "bold"),
            ).pack(anchor="w", padx=(19, 0), pady=(3, 0))
            direction_color = {
                "good": SUCCESS,
                "bad": ERROR,
                "neutral": SLATE,
            }[item["direction"]]
            arrow = ""
            if item["change"] is not None:
                arrow = "▲ " if item["change"] >= 0 else "▼ "
            status_label = tk.Label(
                card.body,
                text=arrow + (item["change_text"] if item["available"] else item["empty_message"]),
                bg=WHITE,
                fg=direction_color if item["available"] else SLATE,
                font=(FONT_FAMILY, 8),
                anchor="w",
                justify="left",
                wraplength=205,
            )
            status_label.pack(fill="x", padx=(19, 0), pady=(1, 0))
            if item["available"]:
                Sparkline(card.body, item["sparkline"]).pack(
                    fill="x",
                    padx=(16, 4),
                    pady=(1, 0),
                )
            self._bind_click(
                card.body,
                lambda _event, row=item: self.navigate(row["action"], row),
            )

    def _render_analytics(self, summary: dict[str, Any]) -> None:
        specifications = [
            ("Sales Trend", summary["sales_trend"], self._draw_sales_chart),
            ("Margin Trend", summary["margin_trend"], self._draw_margin_chart),
            ("Cost Breakdown", summary["cost_breakdown"], self._draw_cost_chart),
        ]
        for title, data, renderer in specifications:
            card = Card(self.analytics_section, height=218)
            self.analytics_cards.append(card)
            header = tk.Frame(card.body, bg=WHITE)
            header.pack(fill="x")
            tk.Label(
                header,
                text=title,
                bg=WHITE,
                fg=PRIMARY_NAVY,
                font=(FONT_FAMILY, 12, "bold"),
            ).pack(side="left")
            if title == "Sales Trend" and data.get("available"):
                total = tk.Frame(header, bg=WHITE)
                total.pack(side="right")
                tk.Label(
                    total,
                    text=f"${data.get('total', 0):,.0f}",
                    bg=WHITE,
                    fg=OCEAN_TEAL,
                    font=(FONT_FAMILY, 13, "bold"),
                ).pack(anchor="e")
                tk.Label(
                    total,
                    text=data.get("change_text", ""),
                    bg=WHITE,
                    fg=SUCCESS if data.get("direction") == "good" else SLATE,
                    font=(FONT_FAMILY, 7),
                ).pack(anchor="e")
            chart_host = tk.Frame(card.body, bg=WHITE)
            chart_host.pack(fill="both", expand=True, pady=(6, 0))
            if data.get("available") and _load_matplotlib():
                renderer(chart_host, data)
            else:
                self._render_fallback_chart(chart_host, data, title)
            if title == "Cost Breakdown":
                tk.Button(
                    card.body,
                    text="View full breakdown  →",
                    command=lambda row=data: self.navigate(row.get("action", "reports"), row),
                    bg=WHITE,
                    fg=OCEAN_TEAL,
                    activebackground=WHITE,
                    activeforeground=PRIMARY_NAVY,
                    relief="flat",
                    bd=0,
                    anchor="e",
                    cursor="hand2",
                    font=(FONT_FAMILY, 8, "bold"),
                ).pack(fill="x")
            self._bind_click(
                card.body,
                lambda _event, row=data: self.navigate(row.get("action", "reports"), row),
            )

    def _render_fallback_chart(
        self, host: tk.Misc, data: dict[str, Any], title: str
    ) -> None:
        """Render lightweight bars when Matplotlib is unavailable."""
        if title == "Sales Trend":
            values = [float(value or 0) for value in data.get("values", [])]
            labels = [str(value) for value in data.get("labels", [])]
        elif title == "Margin Trend":
            values = [float(value or 0) for value in data.get("actual", [])]
            labels = [str(value) for value in data.get("labels", [])]
        else:
            items = data.get("items", [])
            values = [float(item.get("amount") or 0) for item in items]
            labels = [str(item.get("category") or "Other") for item in items]
        if not values:
            self._empty_state(host, data.get("empty_message", "No data available."))
            return
        visible_values = values[:12] if title == "Cost Breakdown" else values[-12:]
        visible_labels = labels[:12] if title == "Cost Breakdown" else labels[-12:]
        minimum = min(0.0, min(visible_values))
        maximum = max(0.0, max(visible_values))
        span = maximum - minimum or 1.0
        for index, value in enumerate(visible_values):
            label_values = visible_labels
            label = label_values[index] if index < len(label_values) else str(index + 1)
            row = tk.Frame(host, bg=WHITE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label[:18], width=18, anchor="w", bg=WHITE, fg=CHARCOAL).pack(side="left")
            track = tk.Frame(row, bg="#E2E8F0", height=14, width=250)
            track.pack(side="left", padx=4)
            track.pack_propagate(False)
            zero_offset = int(250 * (0 - minimum) / span)
            if value >= 0:
                bar_x = zero_offset
                bar_width = max(2, int(250 * value / span))
            else:
                bar_x = int(250 * (value - minimum) / span)
                bar_width = max(2, zero_offset - bar_x)
            bar = tk.Frame(track, bg=SUCCESS if value >= 0 else ERROR, height=14, width=bar_width)
            bar.place(x=bar_x, y=0)
            suffix = "%" if title == "Margin Trend" else ""
            prefix = "$" if title in {"Sales Trend", "Cost Breakdown"} else ""
            tk.Label(row, text=f"{prefix}{value:,.1f}{suffix}", bg=WHITE, fg=SLATE).pack(side="left")

    def _render_priorities(self, priorities: dict[str, list[dict[str, Any]]]) -> None:
        self.priority_header = tk.Frame(self.priority_section, bg=FROST_WHITE)
        self.priority_header.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 3))
        tk.Label(
            self.priority_header,
            text="Today’s Priorities",
            bg=FROST_WHITE,
            fg=PRIMARY_NAVY,
            font=(FONT_FAMILY, 14, "bold"),
        ).pack(side="left")
        tk.Button(
            self.priority_header,
            text="Ask CostPilot about this period  →",
            command=self.ask_costpilot_callback,
            bg=FIRE_ORANGE,
            fg=WHITE,
            activebackground=OCEAN_TEAL,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            font=(FONT_FAMILY, 8, "bold"),
        ).pack(side="right")
        specs = [
            ("Attention Needed", priorities.get("attention", []), ERROR, "Review Now", "review"),
            ("Watchlist", priorities.get("watchlist", []), WARNING, "View Watchlist", "exceptions"),
            ("On Track", priorities.get("on_track", []), SUCCESS, "View Performance", "reports"),
            ("Today’s Tasks", priorities.get("tasks", []), OCEAN_TEAL, "View All Tasks", "work"),
        ]
        empty_messages = {
            "Attention Needed": "No unresolved critical items.",
            "Watchlist": "No developing conditions to watch.",
            "On Track": "No verified on-track conditions available.",
            "Today’s Tasks": "No pending workflow tasks.",
        }
        for title, items, accent, button_text, action in specs:
            card = Card(self.priority_section, height=146, accent=accent)
            self.priority_cards.append(card)
            header = tk.Frame(card.body, bg=WHITE)
            header.pack(fill="x")
            tk.Label(
                header,
                text="●",
                bg=WHITE,
                fg=accent,
                font=(FONT_FAMILY, 13, "bold"),
            ).pack(side="left")
            tk.Label(
                header,
                text=title,
                bg=WHITE,
                fg=accent,
                font=(FONT_FAMILY, 11, "bold"),
            ).pack(side="left", padx=(6, 0))
            rows = tk.Frame(card.body, bg=WHITE)
            rows.pack(fill="both", expand=True, pady=(4, 1))
            if not items:
                tk.Label(
                    rows,
                    text=empty_messages[title],
                    bg=WHITE,
                    fg=SLATE,
                    font=(FONT_FAMILY, 9),
                    wraplength=245,
                    justify="left",
                ).pack(anchor="w", pady=5)
            for item in items[:3]:
                prefix = "✓" if title == "On Track" else "›"
                label = tk.Label(
                    rows,
                    text=f"{prefix}  {item['title']}",
                    bg=WHITE,
                    fg=CHARCOAL,
                    activeforeground=OCEAN_TEAL,
                    font=(FONT_FAMILY, 8),
                    anchor="w",
                    justify="left",
                    wraplength=240,
                    cursor="hand2",
                    pady=1,
                )
                label.pack(fill="x")
                label.bind(
                    "<Button-1>",
                    lambda _event, row=item: self.navigate(row.get("action", action), row),
                )
            tk.Button(
                card.body,
                text=button_text,
                command=(
                    (lambda rows=items: self.navigate(rows[0].get("action", "review"), rows[0]))
                    if title == "Attention Needed" and items
                    else (lambda name=action: self.navigate(name, None))
                ),
                bg=WHITE,
                fg=accent,
                activebackground=WHITE,
                activeforeground=PRIMARY_NAVY,
                relief="flat",
                bd=0,
                anchor="w",
                cursor="hand2",
                font=(FONT_FAMILY, 8, "bold"),
            ).pack(fill="x")

    def _draw_sales_chart(self, host: tk.Misc, data: dict[str, Any]) -> None:
        fig, ax = self._figure(host)
        labels, values = self._sample_series(data["labels"], data["values"], 12)
        ax.bar(range(len(values)), values, color=OCEAN_TEAL, width=0.62)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=7, color=SLATE, rotation=0)
        ax.tick_params(axis="y", labelsize=7, colors=SLATE)
        ax.yaxis.grid(True, color=SUBTLE_GRID, linewidth=0.7)
        ax.set_axisbelow(True)
        self._style_axes(ax)
        fig.tight_layout(pad=0.8)

    def _draw_margin_chart(self, host: tk.Misc, data: dict[str, Any]) -> None:
        fig, ax = self._figure(host)
        labels, actual = self._sample_series(data["labels"], data["actual"], 12)
        target = [data["target_value"]] * len(actual)
        ax.plot(
            range(len(actual)),
            actual,
            color=OCEAN_TEAL,
            marker="o",
            markersize=3,
            linewidth=2,
            label="Actual",
        )
        ax.plot(
            range(len(target)),
            target,
            color=BURGUNDY,
            linewidth=1.5,
            linestyle="--",
            label=f"Target {data['target_value']:.1f}%",
        )
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=7, color=SLATE)
        ax.tick_params(axis="y", labelsize=7, colors=SLATE)
        ax.yaxis.set_major_formatter(lambda value, _position: f"{value:.0f}%")
        ax.yaxis.grid(True, color=SUBTLE_GRID, linewidth=0.7)
        ax.legend(loc="upper left", frameon=False, fontsize=7, ncol=2)
        ax.set_axisbelow(True)
        self._style_axes(ax)
        fig.tight_layout(pad=0.8)

    def _draw_cost_chart(self, host: tk.Misc, data: dict[str, Any]) -> None:
        fig, ax = self._figure(host)
        items = data["items"][:7]
        values = [item["amount"] for item in items]
        labels = [
            f"{item['category']}  ${item['amount']:,.0f}  {item['percent']:.1f}%"
            for item in items
        ]
        colors = list(CHART_COLORS[: len(values)])
        wedges, _texts = ax.pie(
            values,
            colors=colors,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.34, "edgecolor": WHITE, "linewidth": 1},
        )
        ax.text(
            0,
            0.06,
            f"${data['total']:,.0f}",
            ha="center",
            va="center",
            fontsize=10,
            fontweight="bold",
            color=PRIMARY_NAVY,
        )
        ax.text(0, -0.16, "Total Costs", ha="center", va="center", fontsize=7, color=SLATE)
        ax.legend(
            wedges,
            labels,
            loc="center left",
            bbox_to_anchor=(0.96, 0.5),
            frameon=False,
            fontsize=6.5,
            handlelength=1,
        )
        fig.tight_layout(pad=0.7)

    def _figure(self, host: tk.Misc) -> tuple[Any, Any]:
        fig = Figure(figsize=(4.2, 2.35), dpi=80, facecolor=WHITE)
        ax = fig.add_subplot(111)
        ax.set_facecolor(WHITE)
        canvas = FigureCanvasTkAgg(fig, master=host)
        widget = canvas.get_tk_widget()
        widget.configure(bg=WHITE, highlightthickness=0)
        widget.pack(fill="both", expand=True)
        canvas.draw_idle()
        self._figures.append(fig)
        self._chart_canvases.append(canvas)
        return fig, ax

    @staticmethod
    def _style_axes(ax: Any) -> None:
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(SUBTLE_GRID)
        ax.spines["bottom"].set_color(SUBTLE_GRID)

    @staticmethod
    def _sample_series(
        labels: list[str],
        values: list[float],
        maximum: int,
    ) -> tuple[list[str], list[float]]:
        if len(values) <= maximum:
            return labels, values
        step = max(1, math.ceil(len(values) / maximum))
        indices = list(range(0, len(values), step))
        if indices[-1] != len(values) - 1:
            indices.append(len(values) - 1)
        return [labels[index] for index in indices], [values[index] for index in indices]

    @staticmethod
    def _empty_state(parent: tk.Misc, message: str) -> None:
        tk.Label(
            parent,
            text=message,
            bg=WHITE,
            fg=SLATE,
            font=(FONT_FAMILY, 9),
            justify="center",
            wraplength=280,
        ).pack(expand=True, fill="both", padx=12, pady=20)

    def _clear_figures(self) -> None:
        for canvas in self._chart_canvases:
            try:
                canvas.get_tk_widget().destroy()
            except (tk.TclError, AttributeError):
                pass
        for figure in self._figures:
            try:
                figure.clear()
            except AttributeError:
                pass
        self._chart_canvases.clear()
        self._figures.clear()

    @staticmethod
    def _clear_section(section: tk.Misc) -> None:
        for child in section.winfo_children():
            child.destroy()

    @staticmethod
    def _bind_click(widget: tk.Misc, callback: Callable[[tk.Event], None]) -> None:
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass
        widget.bind("<Button-1>", callback)
        for child in widget.winfo_children():
            DashboardView._bind_click(child, callback)

    def _canvas_resized(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.content_window, width=max(360, event.width))
        if self._resize_job:
            try:
                self.parent.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.parent.after(
            80,
            lambda width=event.width: self._apply_responsive_layout(width),
        )

    def _apply_responsive_layout(self, width: int) -> None:
        if not self.summary:
            return
        kpi_columns = 5 if width >= 1060 else (3 if width >= 760 else 2)
        analytics_columns = 3 if width >= 960 else (2 if width >= 650 else 1)
        priority_columns = 4 if width >= 980 else (2 if width >= 630 else 1)
        signature = (kpi_columns, analytics_columns, priority_columns)
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        self._regrid(self.kpi_section, self.kpi_cards, kpi_columns, start_row=0)
        self._regrid(self.analytics_section, self.analytics_cards, analytics_columns, start_row=0)
        self._regrid(self.priority_section, self.priority_cards, priority_columns, start_row=1)
        if hasattr(self, "priority_header"):
            self.priority_header.grid_configure(columnspan=priority_columns)

    @staticmethod
    def _regrid(
        parent: tk.Misc,
        cards: list[Card],
        columns: int,
        *,
        start_row: int,
    ) -> None:
        for card in cards:
            card.grid_forget()
        for column in range(max(columns, 5)):
            active = column < columns
            parent.columnconfigure(
                column,
                weight=1 if active else 0,
                uniform="dashboard" if active else "",
                minsize=0,
            )
        for index, card in enumerate(cards):
            card.grid(
                row=start_row + index // columns,
                column=index % columns,
                sticky="nsew",
                padx=4,
                pady=4,
            )

    def _mousewheel(self, event: tk.Event) -> None:
        try:
            if self.canvas.winfo_containing(event.x_root, event.y_root):
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass
