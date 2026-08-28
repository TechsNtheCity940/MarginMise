#!/usr/bin/env python3
"""Manager-first interface for MarginMise v3.5.

The complete v3.0 feature set remains available, but the default interface is
organized around the small number of decisions a restaurant manager must make.
Advanced Mode exposes the original specialist screens for owners, bookkeepers,
implementation staff, and support.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from dashboard_service import DashboardService
from dashboard_widgets import DashboardView
from restaurant_cost_gui import RestaurantCostControllerGUI, open_path
from src.theme import (
    BORDER_COLOR,
    CARD_BG_COLOR,
    CHARCOAL,
    DISABLED_TEXT,
    ERROR,
    FIRE_ORANGE,
    FONT_FAMILY,
    FROST_WHITE,
    OCEAN_TEAL,
    PRIMARY_NAVY,
    SIDEBAR_MAX_WIDTH,
    SIDEBAR_DIVIDER,
    SIDEBAR_MIN_WIDTH,
    SIDEBAR_TEXT,
    SOFT_SLATE,
    LIGHT_SLATE,
    SIDEBAR_WIDTH_RATIO,
    SLATE,
    SUCCESS,
    WARNING,
    WHITE,
)


SEVERITY_RANK = {"Critical": 0, "Warning": 1, "Info": 2}


class ManagerFirstRestaurantCostControllerGUI(RestaurantCostControllerGUI):
    """A calm, role-aware shell around the complete operational application."""

    def __init__(self, root: tk.Tk):
        self.nav_buttons: dict[str, tk.Button] = {}
        self.advanced_nav_buttons: list[tuple[tk.Button, str | None]] = []
        self.page_sections: dict[tk.Widget, str] = {}
        self.attention_group_map: dict[str, list[dict[str, Any]]] = {}
        self.order_rows_by_id: dict[str, dict[str, Any]] = {}
        self.home_task_rows: list[dict[str, Any]] = []
        self.margin_memory_rows: dict[str, dict[str, Any]] = {}
        self._active_section = "home"
        self.dashboard_service: DashboardService | None = None
        self.dashboard_view: DashboardView | None = None
        self.dashboard_vendor_filter = ""
        self.dashboard_category_filter = ""
        self.dashboard_custom_start = ""
        self.dashboard_custom_end = ""
        self.dashboard_model: dict[str, Any] = {}
        self._logo_image: tk.PhotoImage | None = None
        super().__init__(root)
        self.root.after(120000, self._scheduled_refresh)

    # ---------- shell and navigation ----------
    def _build_style(self) -> None:
        super()._build_style()
        style = ttk.Style()
        style.configure(".", font=(FONT_FAMILY, 9), background=FROST_WHITE, foreground=CHARCOAL)
        style.configure("TFrame", background=FROST_WHITE)
        style.configure("TLabel", background=FROST_WHITE, foreground=CHARCOAL)
        style.configure("AppTitle.TLabel", font=(FONT_FAMILY, 18, "bold"), foreground=PRIMARY_NAVY)
        style.configure("PageTitle.TLabel", font=(FONT_FAMILY, 18, "bold"), foreground=PRIMARY_NAVY)
        style.configure("Section.TLabel", font=(FONT_FAMILY, 11, "bold"), foreground=CHARCOAL)
        style.configure("HeroMetric.TLabel", font=(FONT_FAMILY, 20, "bold"), foreground=PRIMARY_NAVY)
        style.configure("TaskTitle.TLabel", font=(FONT_FAMILY, 11, "bold"), foreground=CHARCOAL)
        style.configure("Muted.TLabel", foreground=SLATE)
        style.configure("Success.TLabel", foreground=SUCCESS)
        style.configure("WarningText.TLabel", foreground=WARNING)
        style.configure("CriticalText.TLabel", foreground=ERROR)
        style.configure("Primary.TButton", padding=(14, 9), font=(FONT_FAMILY, 10, "bold"))
        style.configure("Card.TFrame", relief="solid", borderwidth=1)
        style.configure("Manager.TNotebook", tabmargins=0)
        style.layout("Manager.TNotebook.Tab", [])

    def _build_shell(self) -> None:
        self.root.title("MarginMise v3.5 - CostPilot Review Automation")
        self.root.geometry("1366x768")
        self.root.minsize(1040, 680)

        self.root.configure(bg=FROST_WHITE)
        body = tk.Frame(self.root, bg=FROST_WHITE)
        body.pack(fill="both", expand=True)

        self.sidebar_outer = tk.Frame(
            body,
            width=SIDEBAR_MIN_WIDTH,
            bg=PRIMARY_NAVY,
            bd=0,
            highlightthickness=0,
        )
        self.sidebar_outer.pack(side="left", fill="y")
        self.sidebar_outer.pack_propagate(False)
        self.sidebar = tk.Frame(self.sidebar_outer, bg=PRIMARY_NAVY, padx=12, pady=14)
        self.sidebar.pack(fill="both", expand=True)

        content = tk.Frame(body, bg=FROST_WHITE)
        content.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(content, bg=FROST_WHITE, padx=18, pady=8)
        topbar.pack(fill="x")

        self.restaurant_var = tk.StringVar()
        self.restaurant_combo = ttk.Combobox(
            topbar, textvariable=self.restaurant_var, state="readonly", width=31
        )
        self.restaurant_combo.pack(side="left")
        self.restaurant_combo.bind("<<ComboboxSelected>>", self._restaurant_selected)

        location_menu_button = ttk.Menubutton(topbar, text="Location ▾")
        location_menu = tk.Menu(location_menu_button, tearoff=False)
        location_menu.add_command(label="Add restaurant", command=self.add_restaurant)
        location_menu.add_command(label="Remove from this computer", command=self.remove_restaurant)
        location_menu.add_separator()
        location_menu.add_command(label="Open workspace folder", command=self.open_workspace)
        location_menu.add_command(label="Open automatic upload folder", command=self.open_auto_upload_folder)
        location_menu.add_command(label="Check upload folders now", command=self.scan_auto_upload_now)
        location_menu_button.configure(menu=location_menu)
        location_menu_button.pack(side="left", padx=3)

        ttk.Button(topbar, text="Sign Out", command=self.sign_out).pack(side="right")
        self.user_status_var = tk.StringVar(value="Not signed in")
        ttk.Label(topbar, textvariable=self.user_status_var, style="Muted.TLabel").pack(side="right", padx=10)

        command_frame = tk.Frame(content, bg=FROST_WHITE, padx=18)
        command_frame.pack(fill="x", pady=(0, 7))
        self.quick_ask_var = tk.StringVar()
        self.quick_ask_entry = ttk.Entry(command_frame, textvariable=self.quick_ask_var)
        self.quick_ask_entry.pack(side="left", fill="x", expand=True)
        self.quick_ask_entry.bind("<Return>", lambda _event: self.quick_ask())
        self.quick_ask_button = tk.Button(
            command_frame,
            text="Ask CostPilot",
            command=self.quick_ask,
            bg=FIRE_ORANGE,
            fg=WHITE,
            activebackground=OCEAN_TEAL,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            padx=14,
            pady=5,
            cursor="hand2",
            font=(FONT_FAMILY, 9, "bold"),
        )
        self.quick_ask_button.pack(side="left", padx=(8, 0))

        self.notebook = ttk.Notebook(content, style="Manager.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        # Simplified destinations.
        self.dashboard_tab = tk.Frame(self.notebook, bg=FROST_WHITE)
        self.work_hub_tab = ttk.Frame(self.notebook, padding=18)
        self.insights_hub_tab = ttk.Frame(self.notebook, padding=18)
        self.chat_tab = ttk.Frame(self.notebook, padding=18)
        self.more_hub_tab = ttk.Frame(self.notebook, padding=18)
        self.simple_settings_tab = ttk.Frame(self.notebook, padding=18)

        # Complete specialist pages retained for deliberate access.
        self.intake_tab = ttk.Frame(self.notebook, padding=12)
        self.review_tab = ttk.Frame(self.notebook, padding=12)
        self.auto_upload_tab = ttk.Frame(self.notebook, padding=12)
        self.exceptions_tab = ttk.Frame(self.notebook, padding=12)
        self.receiving_tab = ttk.Frame(self.notebook, padding=12)
        self.items_tab = ttk.Frame(self.notebook, padding=12)
        self.inventory_tab = ttk.Frame(self.notebook, padding=12)
        self.orders_tab = ttk.Frame(self.notebook, padding=12)
        self.data_tab = ttk.Frame(self.notebook, padding=12)
        self.phase2_tab = ttk.Frame(self.notebook, padding=12)
        self.phase3_tab = ttk.Frame(self.notebook, padding=12)
        self.margin_memory_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=12)
        self.security_tab = ttk.Frame(self.notebook, padding=12)
        self.log_tab = ttk.Frame(self.notebook, padding=12)

        pages = (
            (self.dashboard_tab, "Overview", "home"),
            (self.work_hub_tab, "Work", "work"),
            (self.insights_hub_tab, "Insights", "insights"),
            (self.chat_tab, "CostPilot", "assistant"),
            (self.more_hub_tab, "More", "more"),
            (self.simple_settings_tab, "Restaurant Setup", "more"),
            (self.intake_tab, "Invoice Intake", "work"),
            (self.review_tab, "CostPilot Review", "work"),
            (self.auto_upload_tab, "Auto Upload History", "more"),
            (self.exceptions_tab, "Exceptions & Data Health", "work"),
            (self.receiving_tab, "Receive Delivery", "work"),
            (self.items_tab, "Products & Prices", "more"),
            (self.inventory_tab, "Inventory Count & Month Close", "work"),
            (self.orders_tab, "Weekly Order", "work"),
            (self.data_tab, "Sales, Costs & Reports", "insights"),
            (self.phase2_tab, "Operations Tools", "more"),
            (self.phase3_tab, "Owner Intelligence", "insights"),
            (self.margin_memory_tab, "MarginMemory", "insights"),
            (self.settings_tab, "Advanced Settings", "more"),
            (self.security_tab, "Users, Backups & Audit", "more"),
            (self.log_tab, "Activity Log", "more"),
        )
        for frame, title, section in pages:
            self.notebook.add(frame, text=title)
            self.page_sections[frame] = section

        self._build_sidebar()
        self._page_builders: dict[tk.Widget, Callable[[], None]] = {
            self.dashboard_tab: self._build_dashboard,
            self.work_hub_tab: self._build_work_hub,
            self.insights_hub_tab: self._build_insights_hub,
            self.chat_tab: self._build_chat,
            self.more_hub_tab: self._build_more_hub,
            self.simple_settings_tab: self._build_simple_settings,
            self.intake_tab: self._build_intake,
            self.review_tab: self._build_review,
            self.auto_upload_tab: self._build_auto_upload_history,
            self.exceptions_tab: self._build_exceptions,
            self.receiving_tab: self._build_receiving,
            self.items_tab: self._build_items,
            self.inventory_tab: self._build_inventory,
            self.orders_tab: self._build_orders,
            self.data_tab: self._build_data,
            self.phase2_tab: self._build_phase2,
            self.phase3_tab: self._build_phase3,
            self.margin_memory_tab: self._build_margin_memory,
            self.settings_tab: self._build_settings,
            self.security_tab: self._build_security,
            self.log_tab: self._build_log,
        }
        self._built_pages: set[tk.Widget] = set()
        self._built_pages.add(self.dashboard_tab)

        self.status_var = tk.StringVar(value="Select or add a restaurant workspace.")
        status = ttk.Label(
            content,
            textvariable=self.status_var,
            relief="flat",
            anchor="w",
            padding=(10, 4),
            style="Muted.TLabel",
        )
        status.pack(fill="x", side="bottom")
        self.show_page(self.dashboard_tab, "home")
        self.root.bind("<Configure>", self._resize_shell, add="+")

    def _build_sidebar(self) -> None:
        brand = tk.Frame(self.sidebar, bg=PRIMARY_NAVY)
        brand.pack(fill="x", pady=(0, 18))
        logo_path = Path(__file__).resolve().parent / "assets" / "MarginMiseLogo.png"
        if logo_path.exists():
            try:
                source_logo = tk.PhotoImage(file=str(logo_path))
                width, height = source_logo.width(), source_logo.height()
                side = int(min(width, height) * 0.60)
                left = max(0, (width - side) // 2)
                top = max(0, (height - side) // 2)
                cropped_logo = tk.PhotoImage(width=side, height=side)
                cropped_logo.tk.call(
                    cropped_logo,
                    "copy",
                    source_logo,
                    "-from",
                    left,
                    top,
                    left + side,
                    top + side,
                    "-to",
                    0,
                    0,
                )
                factor = max(1, side // 48)
                self._logo_image = cropped_logo.subsample(factor, factor)
                tk.Label(
                    brand,
                    image=self._logo_image,
                    bg=PRIMARY_NAVY,
                    bd=0,
                ).pack(side="left")
            except Exception:
                self._logo_image = None
        tk.Label(
            brand,
            text="MarginMise",
            bg=PRIMARY_NAVY,
            fg=WHITE,
            font=(FONT_FAMILY, 16, "bold"),
        ).pack(side="left", padx=(8, 0))

        nav_outer = tk.Frame(self.sidebar, bg=PRIMARY_NAVY)
        nav_outer.pack(fill="both", expand=True)
        nav_canvas = tk.Canvas(
            nav_outer,
            bg=PRIMARY_NAVY,
            bd=0,
            highlightthickness=0,
        )
        nav_scroll = ttk.Scrollbar(nav_outer, orient="vertical", command=nav_canvas.yview)
        nav_canvas.configure(yscrollcommand=nav_scroll.set)
        nav_canvas.pack(side="left", fill="both", expand=True)
        nav_scroll.pack(side="right", fill="y")
        self.nav_content = tk.Frame(nav_canvas, bg=PRIMARY_NAVY)
        nav_window = nav_canvas.create_window((0, 0), window=self.nav_content, anchor="nw")
        self.nav_content.bind(
            "<Configure>",
            lambda _event: nav_canvas.configure(scrollregion=nav_canvas.bbox("all")),
        )
        nav_canvas.bind(
            "<Configure>",
            lambda event: nav_canvas.itemconfigure(nav_window, width=event.width),
        )

        main = [
            ("home", "⌂  Overview", self.dashboard_tab),
            ("work", "☑  Work", self.work_hub_tab),
            ("insights", "◫  Insights", self.insights_hub_tab),
            ("assistant", "✦  CostPilot", self.chat_tab),
            ("more", "⋯  More", self.more_hub_tab),
        ]
        for key, label, frame in main:
            button = self._sidebar_button(
                self.nav_content,
                label,
                lambda f=frame, s=key: self.show_page(f, s),
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button

        tk.Frame(self.nav_content, bg=SIDEBAR_DIVIDER, height=1).pack(fill="x", pady=12)
        self.advanced_mode_var = tk.BooleanVar(value=bool(self.registry.data.get("advanced_mode", False)))
        tk.Checkbutton(
            self.nav_content,
            text="Advanced Mode",
            variable=self.advanced_mode_var,
            command=self._toggle_advanced_mode,
            bg=PRIMARY_NAVY,
            fg=SIDEBAR_TEXT,
            activebackground=PRIMARY_NAVY,
            activeforeground=WHITE,
            selectcolor=OCEAN_TEAL,
            font=(FONT_FAMILY, 9),
            bd=0,
        ).pack(anchor="w", padx=8)
        tk.Label(
            self.nav_content,
            text="Shows specialist, setup and diagnostic screens.",
            bg=PRIMARY_NAVY,
            fg=LIGHT_SLATE,
            font=(FONT_FAMILY, 8),
            wraplength=185,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(2, 6))
        self.advanced_nav_frame = tk.Frame(self.nav_content, bg=PRIMARY_NAVY)
        advanced = [
            ("Invoice Intake", self.intake_tab, "invoices.upload"),
            ("CostPilot Review", self.review_tab, "reviews.center"),
            ("Auto Upload History", self.auto_upload_tab, "reviews.center"),
            ("Exceptions", self.exceptions_tab, "exceptions.view"),
            ("Receiving", self.receiving_tab, "receiving.verify"),
            ("Products & Prices", self.items_tab, "items.edit"),
            ("Inventory & Close", self.inventory_tab, "inventory.count"),
            ("Order Planning", self.orders_tab, "orders.edit"),
            ("Reports", self.data_tab, "reports.view"),
            ("Operations Tools", self.phase2_tab, "settings.view"),
            ("Owner Intelligence", self.phase3_tab, "portfolio.view"),
            ("MarginMemory", self.margin_memory_tab, "margin_memory.view"),
            ("Advanced Settings", self.settings_tab, "settings.view"),
            ("Users, Backup & Audit", self.security_tab, "security.access"),
            ("Activity Log", self.log_tab, "audit.view"),
        ]
        for label, frame, permission in advanced:
            button = self._sidebar_button(
                self.advanced_nav_frame,
                label,
                lambda f=frame, s=self.page_sections.get(frame, "more"): self.show_page(f, s),
                compact=True,
            )
            self.advanced_nav_buttons.append((button, permission))

        user = tk.Frame(self.sidebar, bg=PRIMARY_NAVY)
        user.pack(fill="x", pady=(12, 0))
        tk.Frame(user, bg=SIDEBAR_DIVIDER, height=1).pack(fill="x", pady=(0, 12))
        self.sidebar_user_name_var = tk.StringVar(value="Not signed in")
        self.sidebar_user_role_var = tk.StringVar(value="")
        tk.Label(
            user,
            textvariable=self.sidebar_user_name_var,
            bg=PRIMARY_NAVY,
            fg=WHITE,
            font=(FONT_FAMILY, 9, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            user,
            textvariable=self.sidebar_user_role_var,
            bg=PRIMARY_NAVY,
            fg=SOFT_SLATE,
            font=(FONT_FAMILY, 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))
        self._toggle_advanced_mode(save=False)

    def _sidebar_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        compact: bool = False,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            anchor="w",
            padx=12,
            pady=5 if compact else 8,
            bg=PRIMARY_NAVY,
            fg=SIDEBAR_TEXT,
            activebackground=OCEAN_TEAL,
            activeforeground=WHITE,
            disabledforeground=DISABLED_TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            font=(FONT_FAMILY, 8 if compact else 9, "bold" if not compact else "normal"),
        )

    def _resize_shell(self, event: tk.Event) -> None:
        if event.widget is not self.root or not hasattr(self, "sidebar_outer"):
            return
        width = max(
            SIDEBAR_MIN_WIDTH,
            min(SIDEBAR_MAX_WIDTH, int(event.width * SIDEBAR_WIDTH_RATIO)),
        )
        self.sidebar_outer.configure(width=width)

    def show_page(self, frame: tk.Widget, section: str | None = None) -> None:
        if frame not in self._built_pages:
            builder = self._page_builders.get(frame)
            if builder:
                try:
                    builder()
                except Exception:
                    self._built_pages.discard(frame)
                    raise
                self._built_pages.add(frame)
        self.notebook.select(frame)
        self._set_active_section(section or self.page_sections.get(frame, "more"))

    def _set_active_section(self, section: str) -> None:
        self._active_section = section
        for key, button in self.nav_buttons.items():
            active = key == section
            button.configure(
                bg=OCEAN_TEAL if active else PRIMARY_NAVY,
                fg=WHITE if active else SIDEBAR_TEXT,
            )

    def _toggle_advanced_mode(self, save: bool = True) -> None:
        enabled = bool(self.advanced_mode_var.get())
        if save:
            self.registry.data["advanced_mode"] = enabled
            self.registry.save()
        if enabled:
            self.advanced_nav_frame.pack(fill="x", pady=(4, 12))
            self._update_role_navigation()
        else:
            self.advanced_nav_frame.pack_forget()

    def _update_role_navigation(self) -> None:
        chat_allowed = self.has_permission("chat.use") if self.current_user else False
        self.nav_buttons["assistant"].configure(state="normal" if chat_allowed else "disabled")
        self.quick_ask_entry.configure(state="normal" if chat_allowed else "disabled")
        self.quick_ask_button.configure(state="normal" if chat_allowed else "disabled")
        if self.advanced_mode_var.get():
            for button, permission in self.advanced_nav_buttons:
                allowed = True
                if permission and self.current_user:
                    allowed = self._navigation_permission_allowed(permission)
                    if permission == "reports.view":
                        allowed = allowed or self.has_permission("reports.export")
                    if permission == "exceptions.view":
                        allowed = allowed or self.has_permission("exceptions.manage")
                if allowed:
                    button.pack(fill="x", pady=1)
                else:
                    button.pack_forget()
        self._update_sidebar_user()
        self._update_work_cards_visibility()
        self._update_more_cards_visibility()
        self.update_permission_control_states()
        self._update_simple_settings_control_states()
        if self.pipeline and hasattr(self, "backup_tree"):
            self.refresh_security()

    def _navigation_permission_allowed(self, permission: str | None) -> bool:
        if not permission:
            return bool(self.current_user)
        if permission == "security.access":
            return bool(self.current_user) and any(
                self.has_permission(candidate)
                for candidate in ("users.manage", "backups.create", "backups.restore", "audit.view")
            )
        return bool(self.current_user and self.has_permission(permission))

    # ---------- simplified home ----------
    def _build_dashboard(self) -> None:
        self.dashboard_view = DashboardView(
            self.dashboard_tab,
            navigate=self._dashboard_navigate,
            refresh=self._refresh_dashboard_now,
            date_changed=self._dashboard_date_changed,
            open_filters=self._open_dashboard_filters,
            ask_costpilot=self._ask_costpilot_about_dashboard,
        )
        self.dashboard_view.set_empty(
            "Select or add a restaurant workspace to connect Overview to its operational data."
        )

    def _build_work_hub(self) -> None:
        ttk.Label(self.work_hub_tab, text="Work", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.work_hub_tab,
            text="Drop incoming files into one folder. The background automation identifies, imports, and organizes them for you.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 14))
        self.work_cards_container = ttk.Frame(self.work_hub_tab)
        self.work_cards_container.pack(fill="both", expand=True)
        self.work_card_widgets: list[tuple[ttk.Frame, str | None]] = []
        self.work_card_vars: dict[str, tk.StringVar] = {}
        cards = [
            ("receiving", "Receive deliveries", "Clean deliveries verify automatically; open only shortages, damage, substitutions, or exceptions.", "receiving.verify", lambda: self.show_page(self.receiving_tab, "work")),
            ("inventory", "Count inventory", "Start a phone count or complete month-end inventory.", "inventory.count", self._open_inventory_workflow),
            ("orders", "Review weekly order", "Only products that may need ordering are shown.", "orders.edit", lambda: self.show_page(self.orders_tab, "work")),
            ("uploads", "Add restaurant files", "Drop invoices, sales, counts, recipes, costs, calendars, and supported integration files into one folder.", "invoices.upload", self.open_auto_upload_folder),
            ("reviews", "Resolve exceptions with CostPilot", "CostPilot explains invoice and receiving problems, applies confirmed single or batch fixes, and leaves only genuinely manual cases open.", "reviews.center", lambda: self.show_page(self.review_tab, "work")),
            ("waste", "Log waste", "Record spoilage, mistakes, returns, or equipment losses.", "waste.log", lambda: self._open_phase2(2)),
        ]
        for index, (key, title, description, permission, command) in enumerate(cards):
            card = ttk.Frame(self.work_cards_container, style="Card.TFrame", padding=16)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=5, pady=5)
            ttk.Label(card, text=title, style="TaskTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=description, style="Muted.TLabel", wraplength=390).pack(anchor="w", pady=(4, 10))
            count_var = tk.StringVar(value="")
            self.work_card_vars[key] = count_var
            ttk.Label(card, textvariable=count_var).pack(anchor="w", pady=(0, 10))
            ttk.Button(card, text="Open", style="Primary.TButton", command=command).pack(anchor="w")
            self.work_card_widgets.append((card, permission))
        self.work_cards_container.columnconfigure(0, weight=1)
        self.work_cards_container.columnconfigure(1, weight=1)
        for row in range(3):
            self.work_cards_container.rowconfigure(row, weight=1)

    def _build_insights_hub(self) -> None:
        ttk.Label(self.insights_hub_tab, text="Insights", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.insights_hub_tab,
            text="Business answers, not database tables. Open the detail only when you need to investigate.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 14))
        self.insight_summary_var = tk.StringVar(value="Select a restaurant to view performance.")
        summary = ttk.Frame(self.insights_hub_tab, style="Card.TFrame", padding=16)
        summary.pack(fill="x", pady=(0, 12))
        ttk.Label(summary, text="Owner summary", style="TaskTitle.TLabel").pack(anchor="w")
        ttk.Label(summary, textvariable=self.insight_summary_var, wraplength=880, justify="left").pack(anchor="w", pady=(6, 0))

        grid = ttk.Frame(self.insights_hub_tab)
        grid.pack(fill="both", expand=True)
        cards = [
            ("MarginMemory", "See captured manager decisions, the conditions behind them, and outcomes waiting to be measured.", self.open_margin_memory),
            ("Sales, costs and annual report", "Review trends and export manager reports.", lambda: self.show_page(self.data_tab, "insights")),
            ("Menu profitability", "See true food cost, contribution and pricing recommendations.", lambda: self._open_phase3(4)),
            ("Forecasts and events", "Review weather, events, forecast accuracy and sales-driven ordering.", lambda: self._open_phase3(2)),
            ("Locations and savings", "Compare locations and quantify time and money saved.", lambda: self._open_phase3(0)),
        ]
        for index, (title, description, command) in enumerate(cards):
            card = ttk.Frame(grid, style="Card.TFrame", padding=16)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=5, pady=5)
            ttk.Label(card, text=title, style="TaskTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=description, style="Muted.TLabel", wraplength=390).pack(anchor="w", pady=(4, 12))
            ttk.Button(card, text="Open", command=command).pack(anchor="w")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)
        grid.rowconfigure(2, weight=1)

    def _build_margin_memory(self) -> None:
        ttk.Label(self.margin_memory_tab, text="MarginMemory", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.margin_memory_tab,
            text="Your restaurant's decision ledger. Phase 1 captures what changed and the conditions that existed; outcome scoring arrives after the evidence window closes.",
            style="Muted.TLabel", wraplength=980,
        ).pack(anchor="w", pady=(2, 10))

        self.margin_memory_summary_var = tk.StringVar(value="Select a restaurant to view decision memory.")
        ttk.Label(
            self.margin_memory_tab, textvariable=self.margin_memory_summary_var,
            style="TaskTitle.TLabel", wraplength=980,
        ).pack(anchor="w", pady=(0, 10))

        filters = ttk.Frame(self.margin_memory_tab)
        filters.pack(fill="x", pady=(0, 8))
        self.margin_memory_status_var = tk.StringVar(value="All")
        self.margin_memory_type_var = tk.StringVar(value="All")
        self.margin_memory_manager_var = tk.StringVar(value="All")
        for label, variable in (
            ("Status", self.margin_memory_status_var),
            ("Decision type", self.margin_memory_type_var),
            ("Manager", self.margin_memory_manager_var),
        ):
            ttk.Label(filters, text=label + ":").pack(side="left", padx=(0, 4))
            combo = ttk.Combobox(filters, textvariable=variable, state="readonly", width=22, values=["All"])
            combo.pack(side="left", padx=(0, 10))
            combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_margin_memory())
            if label == "Status": self.margin_memory_status_combo = combo
            elif label == "Decision type": self.margin_memory_type_combo = combo
            else: self.margin_memory_manager_combo = combo
        ttk.Button(filters, text="Refresh", command=self.refresh_margin_memory).pack(side="right", padx=3)
        ttk.Button(filters, text="Export Ledger", command=self.export_margin_memory).pack(side="right", padx=3)

        pane = ttk.Panedwindow(self.margin_memory_tab, orient="vertical")
        pane.pack(fill="both", expand=True)
        list_frame = ttk.LabelFrame(pane, text="Captured decisions", padding=6)
        detail_frame = ttk.LabelFrame(pane, text="Decision and context details", padding=6)
        pane.add(list_frame, weight=2)
        pane.add(detail_frame, weight=1)

        columns = ("time", "type", "subject", "manager", "reason", "override", "status")
        self.margin_memory_tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        widths = {"time":145,"type":150,"subject":250,"manager":120,"reason":160,"override":105,"status":130}
        for col in columns:
            self.margin_memory_tree.heading(col, text=col.replace("_", " ").title())
            self.margin_memory_tree.column(col, width=widths[col], anchor="w")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.margin_memory_tree.yview)
        self.margin_memory_tree.configure(yscrollcommand=scroll.set)
        self.margin_memory_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.margin_memory_tree.bind("<<TreeviewSelect>>", lambda _e: self.show_selected_margin_memory())
        self.margin_memory_tree.bind("<Double-1>", lambda _e: self.show_selected_margin_memory())

        self.margin_memory_detail = tk.Text(detail_frame, height=12, wrap="word", state="disabled")
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.margin_memory_detail.yview)
        self.margin_memory_detail.configure(yscrollcommand=detail_scroll.set)
        self.margin_memory_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

    def open_margin_memory(self) -> None:
        if not self.require_permission("margin_memory.view"):
            return
        self.show_page(self.margin_memory_tab, "insights")
        self.refresh_margin_memory()

    def refresh_margin_memory(self) -> None:
        if not hasattr(self, "margin_memory_tree"):
            return
        for item in self.margin_memory_tree.get_children():
            self.margin_memory_tree.delete(item)
        self.margin_memory_rows = {}
        if not self.pipeline or not self.current_user or not self.has_permission("margin_memory.view"):
            self.margin_memory_summary_var.set("MarginMemory is unavailable for the current session.")
            return
        try:
            options = self.pipeline.margin_memory_filter_options()
            self.margin_memory_status_combo.configure(values=["All"] + options["statuses"])
            self.margin_memory_type_combo.configure(values=["All"] + options["decision_types"])
            self.margin_memory_manager_combo.configure(values=["All"] + options["managers"])
            rows = self.pipeline.list_margin_memory_decisions(
                status=self.margin_memory_status_var.get(),
                decision_type=self.margin_memory_type_var.get(),
                manager=self.margin_memory_manager_var.get(),
            )
            from margin_memory import REASON_LABELS
            for row in rows:
                data = dict(row)
                iid = data["decision_id"]
                self.margin_memory_rows[iid] = data
                override = data.get("override_percent") or data.get("override_amount") or ""
                if data.get("override_percent"):
                    override = f"{data['override_percent']}%"
                self.margin_memory_tree.insert("", "end", iid=iid, values=(
                    data.get("decision_time", ""), data.get("decision_type", ""),
                    data.get("subject_name") or data.get("subject_id", ""),
                    data.get("decision_maker", ""),
                    REASON_LABELS.get(data.get("reason_code"), data.get("reason_code", "")),
                    override, data.get("status", ""),
                ))
            summary = self.pipeline.margin_memory_summary()
            pending = sum(
                count for status, count in summary["by_status"].items()
                if status in {"Pending Approval", "Pending Outcome", "Ready to Evaluate"}
            )
            self.margin_memory_summary_var.set(
                f"{summary['total']} decision(s) captured · {pending} awaiting approval or outcome · "
                f"{summary['evaluated']} evaluated. Adjusted recommendations are logged per restaurant."
            )
            if rows:
                first = rows[0]["decision_id"]
                self.margin_memory_tree.selection_set(first)
                self.show_selected_margin_memory()
            else:
                self._set_margin_memory_detail(
                    "No decisions match the current filters. Material order overrides, transfers, receiving discrepancies, and invoice corrections will appear here automatically."
                )
        except Exception as exc:
            self.margin_memory_summary_var.set(f"MarginMemory refresh warning: {exc}")
            self._set_margin_memory_detail(str(exc))

    def _set_margin_memory_detail(self, text: str) -> None:
        self.margin_memory_detail.configure(state="normal")
        self.margin_memory_detail.delete("1.0", "end")
        self.margin_memory_detail.insert("1.0", text)
        self.margin_memory_detail.configure(state="disabled")

    def show_selected_margin_memory(self) -> None:
        if not self.pipeline:
            return
        selected = self.margin_memory_tree.selection()
        if not selected:
            return
        try:
            record = self.pipeline.get_margin_memory_decision(selected[0])
            decision = record["decision"]
            context = record.get("context") or {}
            outcome = record.get("outcome")
            recommended = json.loads(decision.get("recommended_action_json") or "{}")
            actual = json.loads(decision.get("actual_action_json") or "{}")
            context_raw = json.loads(context.get("context_json") or "{}") if context else {}
            lines = [
                f"Decision: {decision.get('decision_type')}",
                f"Subject: {decision.get('subject_name') or decision.get('subject_id')}",
                f"Manager: {decision.get('decision_maker')} ({decision.get('decision_maker_role')})",
                f"Reason: {decision.get('reason_code')}",
                f"Note: {decision.get('manager_note') or 'None'}",
                f"Status: {decision.get('status')}",
                f"Evaluation window: {decision.get('evaluation_start_date') or 'Not set'} to {decision.get('evaluation_end_date') or 'Not set'}",
                "", "SYSTEM RECOMMENDATION", json.dumps(recommended, indent=2),
                "", "MANAGER ACTION", json.dumps(actual, indent=2),
                "", "DECISION-TIME CONTEXT", json.dumps(context_raw, indent=2),
                "", "OUTCOME",
                json.dumps(outcome, indent=2) if outcome else "Not evaluated yet. Phase 1 preserves the decision and context without inventing an outcome.",
            ]
            self._set_margin_memory_detail("\n".join(lines))
        except Exception as exc:
            self._set_margin_memory_detail(str(exc))

    def export_margin_memory(self) -> None:
        if not self.require_permission("margin_memory.view") or not self.pipeline:
            return
        try:
            path = self.pipeline.export_margin_memory_decisions()
            if messagebox.askyesno("MarginMemory exported", "Open the exported decision ledger now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("MarginMemory export failed", str(exc))

    def _build_more_hub(self) -> None:
        ttk.Label(self.more_hub_tab, text="More", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.more_hub_tab,
            text="Setup and occasional tools. Managers should not need this section during normal daily work.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 14))
        self.more_cards_container = ttk.Frame(self.more_hub_tab)
        self.more_cards_container.pack(fill="both", expand=True)
        self.more_card_widgets: list[tuple[ttk.Frame, str | None]] = []
        cards = [
            ("Restaurant setup", "Restaurant identity, targets, automation and assistant branding.", "settings.view", lambda: self.show_page(self.simple_settings_tab, "more")),
            ("Products and recipes", "Product conversions, prices, menu recipes and POS imports.", "items.edit", lambda: self.show_page(self.items_tab, "more")),
            ("Locations and transfers", "Register locations and move inventory between restaurants.", "portfolio.view", lambda: self._open_phase3(0)),
            ("Automatic uploads & integrations", "See exact workbook and row errors, retry resolved dependencies, or open the one-folder inbox.", "settings.view", lambda: self.show_page(self.auto_upload_tab, "more")),
            ("Users, backups and audit", "Permissions, verified backups, restore and change history.", "security.access", lambda: self.show_page(self.security_tab, "more")),
            ("Advanced tools", "Full specialist screens and diagnostics.", "settings.view", self._enable_advanced_from_card),
        ]
        for index, (title, description, permission, command) in enumerate(cards):
            card = ttk.Frame(self.more_cards_container, style="Card.TFrame", padding=16)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=5, pady=5)
            ttk.Label(card, text=title, style="TaskTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=description, style="Muted.TLabel", wraplength=390).pack(anchor="w", pady=(4, 12))
            ttk.Button(card, text="Open", command=command).pack(anchor="w")
            self.more_card_widgets.append((card, permission))
        self.more_cards_container.columnconfigure(0, weight=1)
        self.more_cards_container.columnconfigure(1, weight=1)
        for row in range(3):
            self.more_cards_container.rowconfigure(row, weight=1)

    def _build_simple_settings(self) -> None:
        ttk.Label(self.simple_settings_tab, text="Restaurant Setup", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.simple_settings_tab,
            text="The settings most managers and owners may actually need. Technical engine controls remain in Advanced Mode.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 14))
        form = ttk.Frame(self.simple_settings_tab, style="Card.TFrame", padding=18)
        form.pack(fill="x")
        self.simple_setting_vars = {
            "restaurant_name": tk.StringVar(),
            "restaurant_group": tk.StringVar(),
            "address": tk.StringVar(),
            "assistant_display_name": tk.StringVar(value="CostPilot"),
            "target_menu_food_cost_percent": tk.StringVar(),
            "margin_memory_materiality_threshold_percent": tk.StringVar(value="10"),
            "margin_memory_enabled": tk.BooleanVar(value=True),
            "automatic_backups_enabled": tk.BooleanVar(value=True),
            "auto_generate_weekly_order_draft": tk.BooleanVar(value=True),
            "auto_upload_enabled": tk.BooleanVar(value=True),
            "auto_recover_invoice_headers": tk.BooleanVar(value=True),
            "auto_approve_recovered_invoice_headers": tk.BooleanVar(value=True),
            "auto_verify_clean_receiving": tk.BooleanVar(value=True),
            "costpilot_review_auto_explain": tk.BooleanVar(value=True),
            "require_login": tk.BooleanVar(value=True),
        }
        rows = [
            ("Restaurant name", "restaurant_name"),
            ("Restaurant group", "restaurant_group"),
            ("Address", "address"),
            ("Assistant name", "assistant_display_name"),
            ("Target food cost %", "target_menu_food_cost_percent"),
            ("MarginMemory override threshold %", "margin_memory_materiality_threshold_percent"),
        ]
        self.simple_setting_edit_widgets: list[ttk.Widget] = []
        for row, (label, key) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 14))
            entry = ttk.Entry(form, textvariable=self.simple_setting_vars[key], width=54)
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            self.simple_setting_edit_widgets.append(entry)
        checks = [
            ("Enable MarginMemory decision capture", "margin_memory_enabled"),
            ("Create verified automatic backups", "automatic_backups_enabled"),
            ("Prepare one weekly order draft automatically", "auto_generate_weekly_order_draft"),
            ("Automatically process files dropped into the Desktop upload folder", "auto_upload_enabled"),
            ("Recover missing invoice dates and numbers from raw extraction", "auto_recover_invoice_headers"),
            ("Auto-approve recovered invoices that pass all validation", "auto_approve_recovered_invoice_headers"),
            ("Auto-verify clean approved deliveries as received in full", "auto_verify_clean_receiving"),
            ("Have CostPilot explain the first review case automatically", "costpilot_review_auto_explain"),
            ("Require users to sign in", "require_login"),
        ]
        start = len(rows)
        for offset, (label, key) in enumerate(checks):
            check = ttk.Checkbutton(form, text=label, variable=self.simple_setting_vars[key])
            check.grid(row=start + offset, column=0, columnspan=2, sticky="w", pady=4)
            self.simple_setting_edit_widgets.append(check)
        self.save_simple_settings_button = ttk.Button(
            form, text="Save Setup", style="Primary.TButton", command=self.save_simple_settings
        )
        self.save_simple_settings_button.grid(row=start + len(checks), column=0, sticky="w", pady=(16, 0))
        advanced_buttons = ttk.Frame(form)
        advanced_buttons.grid(row=start + len(checks), column=1, sticky="e", pady=(16, 0))
        ttk.Button(advanced_buttons, text="Open Upload Folder", command=self.open_auto_upload_folder).pack(side="left", padx=(0, 6))
        ttk.Button(advanced_buttons, text="About & Privacy", command=self.show_about).pack(side="left", padx=(0, 6))
        ttk.Button(advanced_buttons, text="Open Advanced Settings", command=lambda: self.show_page(self.settings_tab, "more")).pack(side="left")
        form.columnconfigure(1, weight=1)

    def _update_simple_settings_control_states(self) -> None:
        allowed = self.has_permission("settings.manage")
        for widget in getattr(self, "simple_setting_edit_widgets", []):
            widget.state(["!disabled"] if allowed else ["disabled"])
        button = getattr(self, "save_simple_settings_button", None)
        if button is not None:
            button.state(["!disabled"] if allowed else ["disabled"])

    # ---------- CostPilot ----------
    def _assistant_name(self) -> str:
        if self.workspace:
            try:
                return str(self.workspace.load_settings().get("assistant_display_name") or "CostPilot").strip() or "CostPilot"
            except Exception:
                pass
        return "CostPilot"

    def _build_chat(self) -> None:
        top = ttk.Frame(self.chat_tab)
        top.pack(fill="x", pady=(0, 8))
        self.assistant_title_var = tk.StringVar(value="CostPilot")
        ttk.Label(top, textvariable=self.assistant_title_var, style="PageTitle.TLabel").pack(side="left")
        more = ttk.Menubutton(top, text="Conversation ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="New conversation", command=self.new_chat_session)
        menu.add_command(label="Open latest evidence packet", command=self.open_latest_chat_context)
        menu.add_separator()
        menu.add_command(label="Test assistant connection", command=self.test_manager_chat_model)
        more.configure(menu=menu)
        more.pack(side="right")

        self.chat_status_var = tk.StringVar(value="Ask about orders, inventory, invoices, sales, costs, forecasts, or menu profitability.")
        ttk.Label(self.chat_tab, textvariable=self.chat_status_var, style="Muted.TLabel", wraplength=900).pack(anchor="w", pady=(0, 8))

        suggestions = ttk.Frame(self.chat_tab)
        suggestions.pack(fill="x", pady=(0, 8))
        prompts = [
            "What needs my attention today?",
            "Why are these items on the weekly order?",
            "Which costs or prices changed the most?",
            "What is missing before month close?",
        ]
        for index, prompt in enumerate(prompts):
            ttk.Button(suggestions, text=prompt, command=lambda text=prompt: self.ask_suggested_question(text)).grid(
                row=0, column=index, sticky="ew", padx=3
            )
            suggestions.columnconfigure(index, weight=1)

        transcript_frame = ttk.Frame(self.chat_tab, style="Card.TFrame", padding=8)
        transcript_frame.pack(fill="both", expand=True)
        self.chat_transcript = tk.Text(transcript_frame, wrap="word", state="disabled", height=20, borderwidth=0)
        transcript_scroll = ttk.Scrollbar(transcript_frame, orient="vertical", command=self.chat_transcript.yview)
        self.chat_transcript.configure(yscrollcommand=transcript_scroll.set)
        self.chat_transcript.pack(side="left", fill="both", expand=True)
        transcript_scroll.pack(side="right", fill="y")
        self.chat_transcript.tag_configure("user", foreground="#17324D", font=("Segoe UI", 10, "bold"))
        self.chat_transcript.tag_configure("assistant", foreground="#1F6F78")
        self.chat_transcript.tag_configure("system", foreground="#667085", font=("Segoe UI", 9, "italic"))

        source_frame = ttk.LabelFrame(self.chat_tab, text="Evidence behind the latest answer", padding=6)
        source_frame.pack(fill="x", pady=(8, 0))
        source_columns = ("evidence_id", "label", "type", "record_id")
        self.chat_sources_tree = ttk.Treeview(source_frame, columns=source_columns, show="headings", height=3, selectmode="browse")
        for col, width in {"evidence_id": 170, "label": 430, "type": 105, "record_id": 170}.items():
            self.chat_sources_tree.heading(col, text=col.replace("_", " ").title())
            self.chat_sources_tree.column(col, width=width, anchor="w")
        self.chat_sources_tree.pack(side="left", fill="x", expand=True)
        ttk.Button(source_frame, text="Open evidence", command=self.open_selected_chat_source).pack(side="right", padx=(8, 0))
        self.chat_sources_tree.bind("<Double-1>", lambda _e: self.open_selected_chat_source())

        entry_frame = ttk.Frame(self.chat_tab)
        entry_frame.pack(fill="x", pady=(8, 0))
        self.chat_input = tk.Text(entry_frame, height=3, wrap="word")
        self.chat_input.pack(side="left", fill="x", expand=True)
        self.chat_input.bind("<Control-Return>", self._chat_ctrl_enter)
        self.chat_send_button = ttk.Button(entry_frame, text="Ask", style="Primary.TButton", command=self.send_chat_question)
        self.chat_send_button.pack(side="right", padx=(8, 0), fill="y")
        ttk.Label(
            self.chat_tab,
            text="General CostPilot chat is read-only. In CostPilot Review Center, authorized managers can approve, reject, or resolve selected cases only after an explicit confirmation. Counts and vendor transmissions remain protected.",
            style="Muted.TLabel", wraplength=930,
        ).pack(anchor="w", pady=(6, 0))

    def quick_ask(self) -> None:
        question = self.quick_ask_var.get().strip()
        if not question:
            return
        if not self.has_permission("chat.use"):
            messagebox.showwarning("CostPilot", "Your role does not have access to the assistant.")
            return
        self.show_page(self.chat_tab, "assistant")
        self.chat_input.delete("1.0", "end")
        self.chat_input.insert("1.0", question)
        self.quick_ask_var.set("")
        self.send_chat_question()

    # ---------- simplified weekly order ----------
    def _build_orders(self) -> None:
        top = ttk.Frame(self.orders_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Weekly Order", style="PageTitle.TLabel").pack(side="left")
        self.order_filter_var = tk.StringVar(value="Needs ordering")
        ttk.Combobox(top, textvariable=self.order_filter_var, values=("Needs ordering", "All items"), state="readonly", width=16).pack(side="right", padx=(8, 0))
        self.order_filter_var.trace_add("write", lambda *_: self.refresh_orders())

        summary = ttk.Frame(self.orders_tab, style="Card.TFrame", padding=14)
        summary.pack(fill="x", pady=(0, 10))
        self.order_summary_var = tk.StringVar(value="No order draft has been prepared.")
        ttk.Label(summary, textvariable=self.order_summary_var, style="TaskTitle.TLabel", wraplength=730).pack(side="left", fill="x", expand=True)
        ttk.Button(summary, text="Approve Reviewed Order", style="Primary.TButton", command=self.approve_order_batch).pack(side="right")

        columns = ("item", "vendor", "stock", "suggested", "manager_qty", "purchase_unit", "cost")
        table_frame = ttk.Frame(self.orders_tab)
        table_frame.pack(fill="both", expand=True)
        self.orders_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "item": "Item", "vendor": "Vendor", "stock": "Estimated stock",
            "suggested": "Suggested", "manager_qty": "Order", "purchase_unit": "Unit", "cost": "Cost",
        }
        widths = {"item": 300, "vendor": 190, "stock": 125, "suggested": 95, "manager_qty": 95, "purchase_unit": 85, "cost": 100}
        for col in columns:
            self.orders_tree.heading(col, text=headings[col])
            self.orders_tree.column(col, width=widths[col], anchor="w")
        order_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.orders_tree.yview)
        self.orders_tree.configure(yscrollcommand=order_scroll.set)
        self.orders_tree.pack(side="left", fill="both", expand=True)
        order_scroll.pack(side="right", fill="y")
        self.orders_tree.bind("<Double-1>", lambda _e: self.edit_selected_order())
        self.orders_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_order_explanation())

        footer = ttk.Frame(self.orders_tab)
        footer.pack(fill="x", pady=(8, 0))
        self.order_batch_var = tk.StringVar(value="")
        self.order_explanation_var = tk.StringVar(value="Double-click an item to change the order quantity.")
        ttk.Label(footer, textvariable=self.order_explanation_var, style="Muted.TLabel", wraplength=760).pack(side="left", fill="x", expand=True)
        more = ttk.Menubutton(footer, text="More actions ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="Recalculate draft", command=self.generate_order_predictions)
        menu.add_command(label="Edit selected quantity", command=self.edit_selected_order)
        menu.add_command(label="Export order sheet", command=self.export_order_sheet)
        menu.add_command(label="Create vendor purchase orders", command=self.generate_vendor_purchase_orders)
        more.configure(menu=menu)
        more.pack(side="right")

    def refresh_orders(self) -> None:
        if not hasattr(self, "orders_tree"):
            return
        for item in self.orders_tree.get_children():
            self.orders_tree.delete(item)
        self.order_rows_by_id = {}
        if not self.pipeline:
            self.order_summary_var.set("Select a restaurant to view the order draft.")
            return
        batch = self.pipeline.latest_order_batch()
        if not batch:
            self.order_summary_var.set("No order draft is ready. CostPilot can prepare one when purchasing history is available.")
            return
        rows = [dict(row) for row in self.pipeline.list_order_predictions(batch["batch_id"])]
        show_all = self.order_filter_var.get() == "All items"
        visible = []
        total = Decimal("0")
        for row in rows:
            suggested = Decimal(str(row.get("suggested_order_quantity") or 0))
            manager_qty = Decimal(str(row.get("manager_order_quantity") or 0))
            if not show_all and max(suggested, manager_qty) <= 0:
                continue
            visible.append(row)
            total += Decimal(str(row.get("estimated_order_cost") or 0))
            iid = str(row["prediction_id"])
            self.order_rows_by_id[iid] = row
            on_hand = Decimal(str(row.get("estimated_on_hand") or 0))
            stock_text = "Out" if on_hand <= 0 else ("Low" if on_hand < Decimal(str(row.get("par_quantity_count_units") or 0)) else f"{on_hand:,.2f}")
            self.orders_tree.insert("", "end", iid=iid, values=(
                row.get("item_name", ""), row.get("vendor_name", ""), stock_text,
                f"{suggested:,.2f}", f"{manager_qty:,.2f}", row.get("purchase_unit", ""),
                f"${float(row.get('estimated_order_cost') or 0):,.2f}",
            ))
        self.order_batch_var.set(f"Batch {batch['batch_id']} | {batch['status']}")
        qualifier = "products need review" if visible else "products need ordering"
        if len(visible) == 1:
            label = "product needs review"
        else:
            label = qualifier
        self.order_summary_var.set(
            f"{len(visible)} {label} · Estimated order total ${total:,.2f} · Manager approval required"
        )
        self.order_explanation_var.set("Double-click an item to change the order quantity. Select an item to see why it was recommended.")

    def _update_order_explanation(self) -> None:
        selected = self.orders_tree.selection()
        if not selected:
            return
        row = self.order_rows_by_id.get(selected[0])
        if not row:
            return
        self.order_explanation_var.set(
            f"Why this amount: estimated stock {row.get('estimated_on_hand') or 0} {row.get('count_unit') or ''}; "
            f"average weekly use {row.get('average_weekly_usage') or 0}; par {row.get('par_quantity_count_units') or 0}; "
            f"lead time {row.get('lead_time_days') or 0} days; safety stock {row.get('safety_stock_days') or 0} days."
        )

    # ---------- task grouping ----------
    def refresh_exceptions_health(self) -> None:
        super().refresh_exceptions_health()
        if not hasattr(self, "attention_tree"):
            return
        for item in self.attention_tree.get_children():
            self.attention_tree.delete(item)
        rows = list(getattr(self, "exception_rows", {}).values())
        grouped = self._group_exceptions(rows)
        self.attention_group_map = {}
        for index, group in enumerate(grouped):
            iid = f"group-{index}"
            self.attention_group_map[iid] = group["rows"]
            count = len(group["rows"])
            title = group["title"] if count == 1 else f"{group['title']} ({count})"
            self.attention_tree.insert("", "end", iid=iid, values=(
                group["severity"], group["category"], title, group["action"],
            ))
        self._render_home_task_cards(grouped)

    def _group_exceptions(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            category = str(row.get("category") or "Other")
            title = str(row.get("title") or "Needs review")
            action = str(row.get("recommended_action") or "Open the record and review it.")
            normalized = title.lower()
            if category == "Receiving" and "not verified" in normalized:
                title = "Deliveries waiting for verification"
                action = "Open Receiving and verify delivered quantities or mark historical records."
            elif category in {"Inventory Count", "Inventory"} and ("overdue" in normalized or "count" in normalized):
                title = "Inventory count needs completion"
                action = "Start or import the current physical inventory count."
            elif "recipe" in normalized or category == "Recipes":
                title = "Menu items need recipe setup"
                action = "Complete recipes for menu items used in profitability analysis."
            elif "backup" in normalized:
                title = "Backup needs attention"
                action = "Create or verify a restaurant backup."
            elif "sales" in normalized or category in {"POS", "Sales"}:
                title = "Sales data needs attention"
                action = "Import or review the latest sales report."
            key = (category, title, action)
            buckets[key].append(row)
        groups = []
        for (category, title, action), members in buckets.items():
            severity = min((str(row.get("severity") or "Info") for row in members), key=lambda x: SEVERITY_RANK.get(x, 9))
            groups.append({"category": category, "title": title, "action": action, "severity": severity, "rows": members})
        groups.sort(key=lambda g: (SEVERITY_RANK.get(g["severity"], 9), -len(g["rows"]), g["category"], g["title"]))
        return groups

    def _render_home_task_cards(self, groups: list[dict[str, Any]]) -> None:
        if not hasattr(self, "home_tasks_container"):
            return
        for child in self.home_tasks_container.winfo_children():
            child.destroy()
        tasks = self._manager_tasks(groups)
        self.home_task_rows = tasks
        if not tasks:
            card = ttk.Frame(self.home_tasks_container, style="Card.TFrame", padding=16)
            card.pack(fill="x")
            ttk.Label(card, text="You are caught up", style="TaskTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text="No urgent manager actions are currently waiting.", style="Success.TLabel").pack(anchor="w", pady=(4, 0))
            return
        for index, task in enumerate(tasks[:4]):
            card = ttk.Frame(self.home_tasks_container, style="Card.TFrame", padding=14)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=5, pady=5)
            style = "CriticalText.TLabel" if task["severity"] == "Critical" else ("WarningText.TLabel" if task["severity"] == "Warning" else "Muted.TLabel")
            ttk.Label(card, text=task["severity"], style=style).pack(anchor="w")
            ttk.Label(card, text=task["title"], style="TaskTitle.TLabel", wraplength=380).pack(anchor="w", pady=(3, 2))
            ttk.Label(card, text=task["description"], style="Muted.TLabel", wraplength=380).pack(anchor="w", pady=(0, 10))
            ttk.Button(card, text=task["button"], style="Primary.TButton", command=task["command"]).pack(anchor="w")
        self.home_tasks_container.columnconfigure(0, weight=1)
        self.home_tasks_container.columnconfigure(1, weight=1)

    def _manager_tasks(self, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tasks = []
        summary = self.pipeline.dashboard_summary() if self.pipeline else {}
        if int(summary.get("deliveries_unverified") or 0) and self.has_permission("receiving.verify"):
            count = int(summary.get("deliveries_unverified") or 0)
            tasks.append({"severity": "Warning", "title": f"Verify {count} delivery record(s)", "description": "Confirm what arrived and record shortages, damage, substitutions, or historical deliveries.", "button": "Open Receiving", "command": lambda: self.show_page(self.receiving_tab, "work")})
        if self.has_permission("reviews.center"):
            try:
                review_summary = self.pipeline.costpilot_review_summary() if self.pipeline else {}
            except Exception:
                review_summary = {"open": int(summary.get("needs_review") or 0), "critical": 0, "invoice_cases": int(summary.get("needs_review") or 0), "receiving_cases": 0}
            if int(review_summary.get("open") or 0):
                count = int(review_summary.get("open") or 0)
                critical = int(review_summary.get("critical") or 0)
                tasks.append({
                    "severity": "Critical" if critical else "Warning",
                    "title": f"Resolve {count} CostPilot review case(s)",
                    "description": (
                        f"{int(review_summary.get('invoice_cases') or 0)} invoice case(s) and "
                        f"{int(review_summary.get('receiving_cases') or 0)} receiving case(s). "
                        "CostPilot explains each issue and can apply confirmed single or batch actions."
                    ),
                    "button": "Open CostPilot Review",
                    "command": lambda: self.show_page(self.review_tab, "work"),
                })
        if int(summary.get("items_to_order") or 0) and self.has_permission("orders.edit"):
            count = int(summary.get("items_to_order") or 0)
            tasks.append({"severity": "Warning", "title": f"Review the weekly order for {count} item(s)", "description": "Suggested quantities are ready. Only unusual or required items are shown.", "button": "Review Order", "command": lambda: self.show_page(self.orders_tab, "work")})
        ready_to_close = int(summary.get("ready_to_close_months") or 0)
        if ready_to_close and self.has_permission("inventory.count"):
            tasks.append({
                "severity": "Info",
                "title": f"Review and close {ready_to_close} imported inventory month(s)",
                "description": "Beginning and ending counts are complete and their usage calculations are already available as previews.",
                "button": "Review Inventory",
                "command": self._open_inventory_workflow,
            })
        elif int(summary.get("closed_months") or 0) == 0 and self.has_permission("inventory.count"):
            tasks.append({"severity": "Info", "title": "Complete the first month-end inventory count", "description": "A physical count improves usage, food-cost, inventory and ordering estimates.", "button": "Count Inventory", "command": self._open_inventory_workflow})
        item_reviews = int(summary.get("item_reviews") or 0)
        if item_reviews and self.has_permission("items.edit"):
            tasks.append({
                "severity": "Warning",
                "title": f"Configure {item_reviews} new product(s)",
                "description": "Confirm count units and purchase-unit conversions before ordering or inventory analysis.",
                "button": "Open Products",
                "command": lambda: self.show_page(self.items_tab, "more"),
            })
        existing = {(task["title"], task["button"]) for task in tasks}
        for group in groups:
            if len(tasks) >= 6:
                break
            title = group["title"] if len(group["rows"]) == 1 else f"{group['title']} ({len(group['rows'])})"
            marker = (title, "Open")
            if marker in existing:
                continue
            tasks.append({
                "severity": group["severity"], "title": title,
                "description": group["action"], "button": "Open",
                "command": lambda g=group: self._open_exception_group(g),
            })
        return tasks

    def _selected_exception(self, from_dashboard: bool = False) -> dict[str, Any] | None:
        if from_dashboard:
            selected = self.attention_tree.selection()
            if not selected:
                return None
            rows = self.attention_group_map.get(selected[0]) or []
            return rows[0] if rows else None
        return super()._selected_exception(from_dashboard=False)

    def open_selected_exception_source(self, from_dashboard: bool = False) -> None:
        if from_dashboard:
            selected = self.attention_tree.selection()
            if not selected:
                messagebox.showinfo("Tasks", "Select a task first.")
                return
            rows = self.attention_group_map.get(selected[0]) or []
            if rows:
                self._open_exception_group({"category": rows[0].get("category", ""), "rows": rows})
            return
        super().open_selected_exception_source(from_dashboard=False)

    def _open_exception_group(self, group: dict[str, Any]) -> None:
        category = str(group.get("category") or "").lower()
        if "receiv" in category:
            self.show_page(self.receiving_tab, "work")
        elif "inventory" in category or "count" in category:
            self.show_page(self.inventory_tab, "work")
        elif "invoice" in category or "review" in category:
            self.show_page(self.review_tab, "work")
        elif "price" in category or "item" in category:
            self.show_page(self.items_tab, "more")
        elif "pos" in category or "recipe" in category or "waste" in category:
            self._open_phase2(0 if "pos" in category or "recipe" in category else 2)
        elif "backup" in category or "security" in category:
            self.show_page(self.security_tab, "more")
        elif "forecast" in category or "weather" in category:
            self._open_phase3(2)
        else:
            self.show_page(self.exceptions_tab, "work")

    # ---------- settings and helpers ----------
    def select_workspace(self, path: Path) -> None:
        super().select_workspace(path)
        if self.pipeline and self.workspace:
            self.dashboard_service = DashboardService(self.pipeline)
            self.dashboard_vendor_filter = ""
            self.dashboard_category_filter = ""
            self.dashboard_custom_start = ""
            self.dashboard_custom_end = ""
            self._update_role_navigation()
            self.refresh_simple_settings()
            self.refresh_dashboard()
            self.show_page(self.dashboard_tab, "home")

    def refresh_all(self) -> None:
        if self.dashboard_service:
            self.dashboard_service.invalidate()
        super().refresh_all()
        self.refresh_simple_settings()
        self.refresh_margin_memory()
        self._update_role_navigation()

    def _refresh_dashboard_legacy(self) -> None:
        if not self.pipeline or not self.workspace:
            self.dashboard_restaurant.configure(text="Select or add a restaurant workspace.")
            for var in self.metric_vars.values():
                var.set("-")
            return
        settings = self.workspace.load_settings()
        summary = self.pipeline.dashboard_summary()
        name = settings.get("restaurant_name") or "Restaurant"
        first_name = self.current_user.display_name.split()[0] if self.current_user and self.current_user.display_name else "Manager"
        self.home_greeting_var.set(f"Good {self._daypart()}, {first_name}")
        self.dashboard_restaurant.configure(text=name)
        upload = self.current_auto_upload_status()
        if upload.get("enabled"):
            pending = int(upload.get("pending") or 0)
            review = int(upload.get("needs_review") or 0)
            failed = int(upload.get("failed") or 0)
            self.home_upload_var.set(
                f"Automatic Upload is watching the Desktop folder · {pending} waiting · {review} review · {failed} failed"
            )
        else:
            self.home_upload_var.set("Automatic Upload is turned off in Restaurant Setup.")
        sales = Decimal(str(summary.get("year_sales") or 0))
        purchases = Decimal(str(summary.get("year_purchases") or 0))
        cost_pct = (purchases / sales * 100) if sales > 0 else Decimal("0")
        tasks = len(self._manager_tasks(self._group_exceptions(list(getattr(self, "exception_rows", {}).values()))))
        self.metric_vars["year_sales"].set(f"${sales:,.2f}")
        self.metric_vars["product_cost_percent"].set(f"{cost_pct:,.1f}%")
        self.metric_vars["estimated_inventory_value"].set(f"${float(summary.get('estimated_inventory_value') or 0):,.2f}")
        self.metric_vars["open_tasks"].set(str(tasks))
        self.home_cost_note_var.set("Product cost uses purchases until completed physical counts provide inventory-adjusted cost.")
        score = float(summary.get("data_quality_score") or 0)
        grade = str(summary.get("data_quality_grade") or "")
        self.home_health_var.set(f"Data health {score:.0f}% · {grade}")
        checks = []
        checks.append("✓ All invoices processed" if int(summary.get("needs_review") or 0) == 0 else f"• {int(summary.get('needs_review') or 0)} invoice(s) need review")
        checks.append("✓ No critical exceptions" if int(summary.get("critical_exceptions") or 0) == 0 else f"• {int(summary.get('critical_exceptions') or 0)} critical exception(s)")
        checks.append("✓ Weekly order is clear" if int(summary.get("items_to_order") or 0) == 0 else f"• {int(summary.get('items_to_order') or 0)} item(s) may need ordering")
        self.everything_else_var.set("   ".join(checks))
        self.insight_summary_var.set(
            f"Year sales ${sales:,.2f}. Purchases ${purchases:,.2f}. Estimated contribution "
            f"${float(summary.get('year_estimated_contribution') or 0):,.2f}. Estimated inventory "
            f"${float(summary.get('estimated_inventory_value') or 0):,.2f}. Forecast accuracy "
            f"{float(summary.get('forecast_accuracy') or 0):,.1f}% across available scored forecasts."
        )
        self._update_work_card_counts(summary)
        assistant = self._assistant_name()
        self.assistant_title_var.set(f"{assistant} · Restaurant Operations Assistant")
        self.quick_ask_button.configure(text=f"Ask {assistant}")
        self.nav_buttons["assistant"].configure(text=assistant)
        self.status_var.set(f"Ready · {name}")

    def refresh_dashboard(self) -> None:
        if not self.dashboard_view:
            return
        if not self.pipeline or not self.workspace:
            self.dashboard_model = {}
            self.dashboard_view.set_empty(
                "Select or add a restaurant workspace to connect Overview to its operational data."
            )
            return
        try:
            if not self.dashboard_service or self.dashboard_service.pipeline is not self.pipeline:
                self.dashboard_service = DashboardService(self.pipeline)
            date_range = self.dashboard_view.date_range_var.get() or "Last 7 Days"
            self.dashboard_model = self.dashboard_service.get_dashboard_summary(
                date_range,
                vendor=self.dashboard_vendor_filter,
                category=self.dashboard_category_filter,
                custom_start=self.dashboard_custom_start or None,
                custom_end=self.dashboard_custom_end or None,
            )
            first_name = (
                self.current_user.display_name.split()[0]
                if self.current_user and self.current_user.display_name
                else "Manager"
            )
            self.dashboard_view.render(
                self.dashboard_model,
                first_name=first_name,
                multi_location=len(self.registry.restaurants) > 1,
            )
            self._dashboard_retry_count = 0
            settings = self.workspace.load_settings()
            name = settings.get("restaurant_name") or self.workspace.root.name
            summary = self.pipeline.dashboard_summary()
            if hasattr(self, "insight_summary_var"):
                self.insight_summary_var.set(
                    f"Current-year sales ${float(summary.get('year_sales') or 0):,.2f}. "
                    f"Purchases ${float(summary.get('year_purchases') or 0):,.2f}. "
                    f"Estimated contribution ${float(summary.get('year_estimated_contribution') or 0):,.2f}."
                )
            self._update_work_card_counts(summary)
            assistant = self._assistant_name()
            if hasattr(self, "assistant_title_var"):
                self.assistant_title_var.set(f"{assistant} · Restaurant Operations Assistant")
            self.quick_ask_button.configure(text=f"Ask {assistant}")
            self.nav_buttons["assistant"].configure(text=f"✦  {assistant}")
            self.status_var.set(f"Ready · {name}")
            self._update_sidebar_user()
        except Exception as exc:
            self.status_var.set(f"Dashboard refresh warning: {exc}")
            self.dashboard_view.set_empty(
                "Overview data could not be loaded. Existing workflows remain available."
            )
            self.log(f"Dashboard refresh warning: {exc}")
            # Auto Upload can finish a database write between the service query
            # and Tk rendering during a large first-run batch. Retry a bounded
            # number of times so a transient lock cannot leave Overview stuck
            # in its fallback state after imports have completed.
            attempts = int(getattr(self, "_dashboard_retry_count", 0)) + 1
            self._dashboard_retry_count = attempts
            expected_pipeline = self.pipeline
            if attempts <= 3:
                self.root.after(
                    650,
                    lambda expected=expected_pipeline: (
                        self.refresh_dashboard() if self.pipeline is expected else None
                    ),
                )

    def _refresh_dashboard_now(self) -> None:
        if self.dashboard_service:
            self.dashboard_service.invalidate()
        self.refresh_dashboard()

    def _dashboard_date_changed(self, selection: str) -> None:
        if selection == "Custom Range":
            start = simpledialog.askstring(
                "Custom dashboard range",
                "Start date (YYYY-MM-DD):",
                parent=self.root,
                initialvalue=self.dashboard_custom_start or date.today().replace(day=1).isoformat(),
            )
            if not start:
                self.dashboard_view.date_range_var.set("Last 7 Days")
                return
            end = simpledialog.askstring(
                "Custom dashboard range",
                "End date (YYYY-MM-DD):",
                parent=self.root,
                initialvalue=self.dashboard_custom_end or date.today().isoformat(),
            )
            if not end:
                self.dashboard_view.date_range_var.set("Last 7 Days")
                return
            try:
                start_date = date.fromisoformat(start.strip())
                end_date = date.fromisoformat(end.strip())
                if start_date > end_date:
                    raise ValueError("Start date must be on or before the end date.")
            except ValueError as exc:
                messagebox.showerror("Custom range", f"Enter valid ISO dates.\n\n{exc}")
                self.dashboard_view.date_range_var.set("Last 7 Days")
                return
            self.dashboard_custom_start = start_date.isoformat()
            self.dashboard_custom_end = end_date.isoformat()
        self._refresh_dashboard_now()

    def _open_dashboard_filters(self) -> None:
        if not self.dashboard_service:
            messagebox.showinfo("Filters", "Select a restaurant first.")
            return
        options = self.dashboard_service.get_filter_options()
        has_vendor = bool(options["vendors"])
        has_category = bool(options["categories"])
        if not has_vendor and not has_category:
            messagebox.showinfo(
                "Filters",
                "No additional vendor or category filters are relevant to the available data.",
            )
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Overview Filters")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=FROST_WHITE)
        form = ttk.Frame(dialog, padding=18)
        form.pack(fill="both", expand=True)
        vendor_var = tk.StringVar(value=self.dashboard_vendor_filter or "All")
        category_var = tk.StringVar(value=self.dashboard_category_filter or "All")
        row = 0
        if has_vendor:
            ttk.Label(form, text="Vendor").grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            ttk.Combobox(
                form,
                textvariable=vendor_var,
                values=["All"] + options["vendors"],
                state="readonly",
                width=30,
            ).grid(row=row, column=1, sticky="ew", pady=6)
            row += 1
        if has_category:
            ttk.Label(form, text="Category").grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            ttk.Combobox(
                form,
                textvariable=category_var,
                values=["All"] + options["categories"],
                state="readonly",
                width=30,
            ).grid(row=row, column=1, sticky="ew", pady=6)
            row += 1

        def apply_filters() -> None:
            self.dashboard_vendor_filter = "" if vendor_var.get() == "All" else vendor_var.get()
            self.dashboard_category_filter = "" if category_var.get() == "All" else category_var.get()
            dialog.destroy()
            self._refresh_dashboard_now()

        ttk.Button(form, text="Clear", command=lambda: (vendor_var.set("All"), category_var.set("All"))).grid(
            row=row, column=0, sticky="w", pady=(14, 0)
        )
        ttk.Button(form, text="Apply", command=apply_filters, style="Primary.TButton").grid(
            row=row, column=1, sticky="e", pady=(14, 0)
        )
        dialog.grab_set()
        dialog.wait_visibility()
        dialog.focus_set()

    def _dashboard_navigate(self, action: str, item: dict[str, Any] | None = None) -> None:
        item = item or {}
        permission = item.get("permission") or {
            "sales": "reports.view",
            "reports": "reports.view",
            "inventory": "inventory.count",
            "receiving": "receiving.verify",
            "orders": "orders.edit",
            "exceptions": "exceptions.view",
            "review": "reviews.center",
            "margin_memory": "margin_memory.view",
            "invoice_intake": "invoices.upload",
            "settings": "settings.view",
            "items": "items.edit",
        }.get(action)
        allowed = True
        if permission:
            allowed = bool(self.current_user and self.has_permission(str(permission)))
            if permission == "reports.view":
                allowed = allowed or bool(self.current_user and self.has_permission("reports.export"))
            elif permission == "exceptions.view":
                allowed = allowed or bool(self.current_user and self.has_permission("exceptions.manage"))
        if not allowed:
            messagebox.showwarning(
                "Permission denied",
                "Your role does not have access to that workflow.",
            )
            return
        if action == "review":
            self.show_page(self.review_tab, "work")
            case_id = str(item.get("source_id") or "")
            if case_id and hasattr(self, "review_tree") and self.review_tree.exists(case_id):
                self.review_tree.selection_set(case_id)
                self.review_tree.see(case_id)
            return
        if action == "source":
            self.open_record_source(
                item.get("source_type"),
                item.get("source_id"),
                item.get("payload") or {},
            )
            return
        if action in {"sales", "reports"}:
            self.show_page(self.data_tab, "insights")
        elif action in {"inventory", "work"}:
            self.show_page(self.inventory_tab if action == "inventory" else self.work_hub_tab, "work")
        elif action == "receiving":
            self.show_page(self.receiving_tab, "work")
        elif action == "orders":
            self.show_page(self.orders_tab, "work")
        elif action == "items":
            self.show_page(self.items_tab, "more")
        elif action == "exceptions":
            self.show_page(self.exceptions_tab, "work")
        elif action == "margin_memory":
            self.open_margin_memory()
        elif action == "sales_import":
            self.show_page(self.data_tab, "insights")
        elif action == "invoice_intake":
            self.show_page(self.intake_tab, "work")
        elif action == "settings":
            self.show_page(self.simple_settings_tab, "more")
        else:
            self.show_page(self.work_hub_tab, "work")

    def _ask_costpilot_about_dashboard(self) -> None:
        if not self.has_permission("chat.use"):
            messagebox.showwarning("CostPilot", "Your role does not have access to the assistant.")
            return
        context = self.dashboard_model.get("costpilot_context", {})
        date_range = context.get("date_range", {})
        self.quick_ask_var.set(
            "Review the Overview for "
            f"{date_range.get('start', '')} through {date_range.get('end', '')}. "
            "Explain the most important KPI changes, exceptions, cost movements, inventory warnings, "
            "and any relevant MarginMemory evidence. Keep the advice read-only and identify the "
            "existing controlled workflow for any recommended action."
        )
        self.quick_ask()

    def _update_sidebar_user(self) -> None:
        if not hasattr(self, "sidebar_user_name_var"):
            return
        if self.current_user:
            self.sidebar_user_name_var.set(self.current_user.display_name or self.current_user.username)
            self.sidebar_user_role_var.set(self.current_user.role)
        else:
            self.sidebar_user_name_var.set("Local mode" if self.workspace else "Not signed in")
            self.sidebar_user_role_var.set("")

    def refresh_simple_settings(self) -> None:
        if not hasattr(self, "simple_setting_vars"):
            return
        if not self.workspace:
            for var in self.simple_setting_vars.values():
                if isinstance(var, tk.BooleanVar):
                    var.set(False)
                else:
                    var.set("")
            return
        settings = self.workspace.load_settings()
        for key, var in self.simple_setting_vars.items():
            var.set(settings.get(key, "CostPilot" if key == "assistant_display_name" else ""))

    def save_simple_settings(self) -> None:
        if not self.require_permission("settings.manage"):
            return
        if not self.workspace:
            return
        settings = self.workspace.load_settings()
        for key, var in self.simple_setting_vars.items():
            value: Any = var.get()
            if key in {"target_menu_food_cost_percent", "margin_memory_materiality_threshold_percent"}:
                try:
                    value = float(str(value).strip())
                except ValueError:
                    messagebox.showerror("Restaurant setup", f"{key.replace('_', ' ').title()} must be a number.")
                    return
                if key == "margin_memory_materiality_threshold_percent" and not 0 <= value <= 1000:
                    messagebox.showerror("Restaurant setup", "MarginMemory threshold must be between 0 and 1000 percent.")
                    return
            if key == "assistant_display_name":
                value = str(value).strip() or "CostPilot"
            settings[key] = value
        self.workspace.save_settings(settings)
        if self.pipeline:
            self.pipeline.reload_settings()
        try:
            from auto_upload import ensure_auto_upload_folder
            ensure_auto_upload_folder(self.workspace, settings.get("restaurant_name", self.workspace.root.name))
            self.auto_upload_coordinator.scan_now()
        except Exception as exc:
            self.log(f"Automatic upload folder warning: {exc}")
        if self.pipeline:
            self.pipeline.controls.audit("settings.manager_update", "settings", "manager", "Updated manager-facing restaurant setup")
        self.refresh_all()
        messagebox.showinfo("Restaurant setup", "Setup saved.")

    def show_about(self) -> None:
        assistant = self._assistant_name()
        messagebox.showinfo(
            f"About {assistant}",
            f"{assistant} is the restaurant-facing name for the application's automation assistant.\n\n"
            "Invoice validation, arithmetic, inventory calculations, order quantities, audit history, and approvals are handled by deterministic application rules. "
            "Natural-language explanations and difficult document interpretation may use the configured external language-model service.\n\n"
            "No order, invoice approval, inventory count, accounting post, or vendor transmission is completed through chat without an authorized manager action. "
            "Review the configured provider's privacy and data-handling terms before a live deployment.",
            parent=self.root,
        )

    def _update_work_cards_visibility(self) -> None:
        if not hasattr(self, "work_card_widgets"):
            return
        for card, permission in self.work_card_widgets:
            allowed = self._navigation_permission_allowed(permission)
            if allowed:
                card.grid()
            else:
                card.grid_remove()

    def _update_more_cards_visibility(self) -> None:
        if not hasattr(self, "more_card_widgets"):
            return
        for card, permission in self.more_card_widgets:
            allowed = self._navigation_permission_allowed(permission)
            if allowed:
                card.grid()
            else:
                card.grid_remove()

    def _update_work_card_counts(self, summary: dict[str, Any]) -> None:
        if not hasattr(self, "work_card_vars"):
            return
        self.work_card_vars["receiving"].set(f"{int(summary.get('deliveries_unverified') or 0)} delivery record(s) waiting")
        closed = int(summary.get("closed_months") or 0)
        ready = int(summary.get("ready_to_close_months") or 0)
        self.work_card_vars["inventory"].set(
            f"{closed} month(s) closed · {ready} ready to review"
            if ready else f"{closed} month(s) closed this year"
        )
        self.work_card_vars["orders"].set(f"{int(summary.get('items_to_order') or 0)} item(s) may need ordering")
        upload = self.current_auto_upload_status()
        self.work_card_vars["uploads"].set(
            f"{int(upload.get('pending') or 0)} file(s) waiting · {int(upload.get('needs_review') or 0)} need review"
        )
        review_count = int(summary.get("needs_review") or 0)
        item_reviews = int(summary.get("item_reviews") or 0)
        self.work_card_vars["reviews"].set(
            f"{review_count} invoice(s) · {item_reviews} product(s) need review"
        )
        self.work_card_vars["waste"].set(f"${float(summary.get('month_waste_cost') or 0):,.2f} logged this month")

    def _open_inventory_workflow(self) -> None:
        if hasattr(self, "phase2_notebook") and self.has_permission("mobile_counts.manage"):
            self.show_page(self.phase2_tab, "work")
            self.phase2_notebook.select(1)
        else:
            self.show_page(self.inventory_tab, "work")

    def _open_phase2(self, index: int) -> None:
        self.show_page(self.phase2_tab, "more" if index in {0, 3, 4} else "work")
        self.phase2_notebook.select(index)

    def _open_phase3(self, index: int) -> None:
        self.show_page(self.phase3_tab, "insights" if index in {0, 2, 4} else "more")
        self.phase3_notebook.select(index)

    def _enable_advanced_from_card(self) -> None:
        self.advanced_mode_var.set(True)
        self._toggle_advanced_mode()
        messagebox.showinfo("Advanced Mode", "Specialist screens are now available in the left navigation.")

    def _daypart(self) -> str:
        from datetime import datetime
        hour = datetime.now().hour
        if hour < 12:
            return "morning"
        if hour < 17:
            return "afternoon"
        return "evening"

    def current_gui_state(self) -> dict[str, Any]:
        state = super().current_gui_state()
        if self.dashboard_model:
            state["overview"] = self.dashboard_model.get("costpilot_context", {})
        return state

    def _scheduled_refresh(self) -> None:
        try:
            if self.pipeline and not self.processing and not self.chat_busy:
                self.refresh_all()
        finally:
            self.root.after(120000, self._scheduled_refresh)

    def generate_order_predictions(self) -> None:
        super().generate_order_predictions()
        if self.pipeline:
            self.show_page(self.orders_tab, "work")


def main() -> int:
    root = tk.Tk()
    app = ManagerFirstRestaurantCostControllerGUI(root)

    def close_app() -> None:
        try:
            app.auto_upload_coordinator.stop()
            if app.pipeline:
                app.pipeline.phase2.stop_mobile_count_server()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_app)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
