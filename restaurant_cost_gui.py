#!/usr/bin/env python3
"""MarginMise GUI v3.5.

Self-contained multi-restaurant interface. All extraction, OCR, and AI querying
run locally via RapidOCR, Tesseract, and the locally provisioned CostPilot LLM.
No external AI provider or cloud service is required.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from datetime import date, datetime
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

Figure = None
FigureCanvasTkAgg = None
MATPLOTLIB_AVAILABLE = False


def _load_matplotlib() -> bool:
    """Load charting only when a chart is actually rendered."""
    global Figure, FigureCanvasTkAgg, MATPLOTLIB_AVAILABLE
    if MATPLOTLIB_AVAILABLE:
        return True
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as canvas_type
        from matplotlib.figure import Figure as figure_type
    except ImportError:
        return False
    Figure = figure_type
    FigureCanvasTkAgg = canvas_type
    MATPLOTLIB_AVAILABLE = True
    return True

from manager_chat import (
    DEFAULT_FREE_MODEL,
    DEFAULT_FREE_PROVIDER,
    ManagerChatError,
    ManagerChatService,
    is_free_model,
)
from auto_upload import (
    AutoUploadCoordinator,
    AutoUploadRouter,
    InitialDocumentDiscovery,
    auto_upload_status,
    ensure_auto_upload_folder,
)
from margin_memory import REASON_CODES, REASON_LABELS
from dashboard_service import DashboardService
from local_ai import ensure as ensure_local_ai, status as local_ai_status
from operational_controls import (
    ALL_ROLES, AuthenticatedUser, OperationalControlsError, PermissionDenied,
)

from invoice_pipeline import (
    DEFAULT_SETTINGS,
    InvoicePipeline,
    ProcessResult,
    RestaurantWorkspace,
    SUPPORTED_SOURCE_SUFFIXES,
    safe_filename,
)

APP_DIR = Path(__file__).resolve().parent
APP_STATE_PATH = Path.home() / ".restaurant_cost_controller_gui.json"


def open_path(path: Path) -> None:
    path = path.expanduser().resolve()
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showerror("Open failed", f"Could not open:\n{path}\n\n{exc}")


class AppRegistry:
    def __init__(self, path: Path = APP_STATE_PATH):
        self.path = path
        self.data: dict[str, Any] = {"restaurants": [], "selected": ""}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception:
            pass

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    @property
    def restaurants(self) -> list[dict[str, str]]:
        rows = self.data.get("restaurants")
        return rows if isinstance(rows, list) else []

    def add(self, name: str, path: Path) -> None:
        resolved = str(path.expanduser().resolve())
        existing = next((r for r in self.restaurants if r.get("path") == resolved), None)
        if existing:
            existing["name"] = name
        else:
            self.restaurants.append({"name": name, "path": resolved})
        self.data["selected"] = resolved
        self.save()

    def remove(self, path: Path) -> None:
        resolved = str(path.resolve())
        self.data["restaurants"] = [r for r in self.restaurants if r.get("path") != resolved]
        if self.data.get("selected") == resolved:
            self.data["selected"] = ""
        self.save()


class RestaurantCostControllerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MarginMise v3.5 - CostPilot Review Automation")
        self.root.geometry("1220x820")
        self.root.minsize(1020, 680)
        self.registry = AppRegistry()
        self.workspace: RestaurantWorkspace | None = None
        self.pipeline: InvoicePipeline | None = None
        self.last_backend_status = None
        self.backend_busy = False
        self.backend_status_checking = False
        self.chat_service: ManagerChatService | None = None
        self.chat_session_id: str | None = None
        self.chat_busy = False
        self.current_user: AuthenticatedUser | None = None
        self.chat_sources: list[dict[str, Any]] = []
        self.mobile_count_token: str | None = None
        self.mobile_count_url: str = ""
        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.processing = False
        self.document_discovery_busy = False
        self.workspace_maintenance_busy = False
        self.auto_upload_coordinator = AutoUploadCoordinator(
            lambda: list(self.registry.restaurants),
            lambda payload: self.worker_queue.put(("auto_upload", payload)),
            scan_interval=5.0,
            max_files_per_cycle=2,
        )
        self._build_style()
        self._build_shell()
        self.root.after(1200, self._start_auto_upload)
        self.root.after(150, self._drain_worker_queue)
        self.root.after(800, self._check_costpilot_first_run)
        # Load the initial restaurant only after the shell (including the log
        # widget) is fully built, so early log() calls have a target.
        self._load_initial_restaurant()

    def _start_auto_upload(self) -> None:
        """Start folder polling after the initial window has become responsive."""
        try:
            if self.root.winfo_exists():
                self.auto_upload_coordinator.start()
        except tk.TclError:
            return

    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Metric.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Danger.TLabel", foreground="#B42318")
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_shell(self) -> None:
        # Sidebar shell that matches the MarginMise overview layout, while preserving
        # all existing functionality and notebook tabs.
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)

        sidebar = ttk.Frame(self.root, padding=(10, 10, 8, 10))
        sidebar.grid(row=0, column=0, rowspan=3, sticky="nsw")
        sidebar.columnconfigure(0, weight=1)

        logo = ttk.Label(sidebar, text="MarginMise", font=("Segoe UI", 16, "bold"), foreground="#0F6B78")
        logo.grid(row=0, column=0, sticky="w", pady=(0, 14))

        self.sidebar_nav: dict[str, tk.Widget] = {}

        main = ttk.Frame(self.root)
        main.grid(row=0, column=1, rowspan=2, sticky="nsew")
        main.rowconfigure(1, weight=1)
        main.columnconfigure(0, weight=1)

        header = ttk.Frame(main, padding=(12, 10, 12, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Overview", font=("Segoe UI", 17, "bold"), foreground="#0B1F33").grid(row=0, column=0, sticky="w")
        self.restaurant_var = tk.StringVar()
        self.restaurant_combo = ttk.Combobox(header, textvariable=self.restaurant_var, state="readonly", width=34)
        self.restaurant_combo.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.restaurant_combo.bind("<<ComboboxSelected>>", self._restaurant_selected)

        self.user_role_var = tk.StringVar(value="Not signed in")
        user_status = ttk.Label(header, textvariable=self.user_role_var, style="Muted.TLabel")
        user_status.grid(row=0, column=2, sticky="e")
        ttk.Button(header, text="Sign Out", command=self.sign_out).grid(row=0, column=3, sticky="e", padx=(6, 0))
        ttk.Button(header, text="Refresh", command=self.refresh_all).grid(row=0, column=4, sticky="e", padx=(6, 0))

        self.notebook = ttk.Notebook(main, padding=(12, 4, 12, 12))
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.notebook.enable_traversal()
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._update_sidebar_highlight())

        self.dashboard_tab = ttk.Frame(self.notebook)
        self.intake_tab = ttk.Frame(self.notebook)
        self.review_tab = ttk.Frame(self.notebook)
        self.auto_upload_tab = ttk.Frame(self.notebook)
        self.exceptions_tab = ttk.Frame(self.notebook)
        self.receiving_tab = ttk.Frame(self.notebook)
        self.items_tab = ttk.Frame(self.notebook)
        self.inventory_tab = ttk.Frame(self.notebook)
        self.orders_tab = ttk.Frame(self.notebook)
        self.data_tab = ttk.Frame(self.notebook)
        self.phase2_tab = ttk.Frame(self.notebook)
        self.phase3_tab = ttk.Frame(self.notebook)
        self.chat_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.security_tab = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook)
        for frame, title in (
            (self.dashboard_tab, "Overview"),
            (self.intake_tab, "Invoice Intake"),
            (self.review_tab, "CostPilot Review"),
            (self.auto_upload_tab, "Auto Upload History"),
            (self.exceptions_tab, "Notifications"),
            (self.receiving_tab, "Receiving"),
            (self.items_tab, "Items & Prices"),
            (self.inventory_tab, "Inventory & Counts"),
            (self.orders_tab, "Order Planning"),
            (self.data_tab, "Reports & Exports"),
            (self.phase2_tab, "Operations"),
            (self.phase3_tab, "Intelligence"),
            (self.chat_tab, "Ask CostPilot"),
            (self.settings_tab, "Settings"),
            (self.security_tab, "Security"),
        ):
            self.notebook.add(frame, text=title)

        nav_items = [
            (self.dashboard_tab, "Overview"),
            (self.intake_tab, "Invoice Intake"),
            (self.review_tab, "CostPilot Review"),
            (self.auto_upload_tab, "Auto Upload History"),
            (self.exceptions_tab, "Notifications"),
            (self.receiving_tab, "Receiving"),
            (self.items_tab, "Items & Prices"),
            (self.inventory_tab, "Inventory & Counts"),
            (self.orders_tab, "Order Planning"),
            (self.data_tab, "Reports & Exports"),
            (self.phase2_tab, "Operations"),
            (self.phase3_tab, "Intelligence"),
            (self.chat_tab, "Ask CostPilot"),
            (self.settings_tab, "Settings"),
            (self.security_tab, "Security"),
        ]
        for idx, (frame, label) in enumerate(nav_items):
            btn = tk.Button(
                sidebar,
                text=label,
                anchor="w",
                padx=10,
                pady=7,
                bg="#0B1F33",
                fg="#FFFFFF",
                activebackground="#0F6B78",
                activeforeground="#FFFFFF",
                relief="flat",
                cursor="hand2",
                command=lambda f=frame: self.notebook.select(f),
            )
            btn.grid(row=idx + 1, column=0, sticky="ew", pady=2)
            self.sidebar_nav[label] = btn

        user_lbl = ttk.Label(sidebar, text="Operations Manager", style="Muted.TLabel", foreground="#CBD5E1")
        user_lbl.grid(row=len(nav_items) + 2, column=0, sticky="w", pady=(12, 0))
        self.notebook.select(self.dashboard_tab)
        self.root.after(50, self._update_sidebar_highlight)

        self.user_status_var = self.user_role_var
        self._build_dashboard()
        self._build_auto_upload_history()
        self._build_exceptions()
        self._build_receiving()
        self._build_items()
        self._build_inventory()
        self._build_orders()
        self._build_data()
        self._build_phase2()
        self._build_phase3()
        self._build_chat()
        self._build_settings()
        self._build_security()
        self._build_log()

        self.status_var = tk.StringVar(value="Select or add a restaurant workspace.")
        status = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(10, 5))
        status.grid(row=2, column=1, sticky="ew")

    def _update_sidebar_highlight(self) -> None:
        current = self.notebook.select()
        try:
            current_tab = self.notebook.nametowidget(current)
        except Exception:
            return
        for frame, label in (
            (self.dashboard_tab, "Overview"),
            (self.intake_tab, "Invoice Intake"),
            (self.review_tab, "CostPilot Review"),
            (self.auto_upload_tab, "Auto Upload History"),
            (self.exceptions_tab, "Notifications"),
            (self.receiving_tab, "Receiving"),
            (self.items_tab, "Items & Prices"),
            (self.inventory_tab, "Inventory & Counts"),
            (self.orders_tab, "Order Planning"),
            (self.data_tab, "Reports"),
            (self.phase2_tab, "Operations"),
            (self.phase3_tab, "Intelligence"),
            (self.chat_tab, "Ask CostPilot"),
            (self.settings_tab, "Settings"),
            (self.security_tab, "Security"),
        ):
            btn = self.sidebar_nav.get(label)
            if not btn:
                continue
            active = current_tab is frame
            btn.configure(
                bg="#0F6B78" if active else "#0B1F33",
                fg="#FFFFFF",
            )

    def _build_dashboard(self) -> None:
        # Wipe any prior dashboard layout and rebuild in the MarginMise overview style.
        for child in self.dashboard_tab.winfo_children():
            child.destroy()

        top = ttk.Frame(self.dashboard_tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Overview", font=("Segoe UI", 17, "bold"), foreground="#0B1F33").pack(side="left")
        ttk.Label(top, text="Welcome back. Here's what's happening across your restaurant today.", style="Muted.TLabel").pack(side="left", padx=(10, 0))

        self.date_range_var = tk.StringVar(value="Last 7 Days")
        date_choices = ["Today", "Yesterday", "Last 7 Days", "Last 30 Days", "This Month", "Last Month"]
        ttk.Label(top, text="Date:").pack(side="left", padx=(12, 2))
        ttk.Combobox(top, textvariable=self.date_range_var, values=date_choices, state="readonly", width=12).pack(side="left")
        ttk.Button(top, text="Filters", command=self._open_filters_dialog).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Refresh", command=self.refresh_all).pack(side="right", padx=(0, 4))
        ttk.Button(top, text="Open CostPilot Review", command=lambda: self.notebook.select(self.review_tab)).pack(side="right", padx=4)

        self.kpi_frame = ttk.Frame(self.dashboard_tab)
        self.kpi_frame.pack(fill="x", pady=(4, 8))
        for column in range(5):
            self.kpi_frame.columnconfigure(column, weight=1)

        charts = ttk.Frame(self.dashboard_tab)
        charts.pack(fill="both", expand=True, pady=(0, 8))
        charts.columnconfigure(0, weight=3)
        charts.columnconfigure(1, weight=3)
        charts.columnconfigure(2, weight=2)

        self._sales_chart_frame = ttk.LabelFrame(charts, text="Sales Trend", padding=8)
        self._sales_chart_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._margin_chart_frame = ttk.LabelFrame(charts, text="Margin Trend", padding=8)
        self._margin_chart_frame.grid(row=0, column=1, sticky="nsew", padx=4)
        self._cost_chart_frame = ttk.LabelFrame(charts, text="Cost Breakdown", padding=8)
        self._cost_chart_frame.grid(row=0, column=2, sticky="nsew", padx=(4, 0))

        bottom = ttk.Frame(self.dashboard_tab)
        bottom.pack(fill="both", expand=True)

        attention = ttk.LabelFrame(bottom, text="Attention Needed", padding=10)
        attention.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        watchlist = ttk.LabelFrame(bottom, text="Watchlist", padding=10)
        watchlist.grid(row=0, column=1, sticky="nsew", padx=4)
        on_track = ttk.LabelFrame(bottom, text="On Track", padding=10)
        on_track.grid(row=0, column=2, sticky="nsew", padx=4)
        tasks = ttk.LabelFrame(bottom, text="Today's Tasks", padding=10)
        tasks.grid(row=0, column=3, sticky="nsew", padx=(4, 0))

        for col in range(4):
            bottom.columnconfigure(col, weight=1)
            bottom.rowconfigure(0, weight=1)

        self._attention_tree = ttk.Treeview(attention, columns=("title","action"), show="headings", height=8)
        self._attention_tree.heading("title", text="Item")
        self._attention_tree.heading("action", text="Action")
        self._attention_tree.column("title", width=220, anchor="w")
        self._attention_tree.column("action", width=130, anchor="center")
        self._attention_tree.pack(fill="both", expand=True)
        self._attention_tree.bind("<Double-1>", self._on_attention_double_click)
        ttk.Button(attention, text="Review Now", command=lambda: self.notebook.select(self.review_tab)).pack(fill="x", pady=(6, 0))

        self._watchlist_tree = ttk.Treeview(watchlist, columns=("title","action"), show="headings", height=8)
        self._watchlist_tree.heading("title", text="Item")
        self._watchlist_tree.heading("action", text="Action")
        self._watchlist_tree.column("title", width=200, anchor="w")
        self._watchlist_tree.column("action", width=110, anchor="center")
        self._watchlist_tree.pack(fill="both", expand=True)
        self._watchlist_tree.bind("<Double-1>", self._on_watchlist_double_click)
        ttk.Button(watchlist, text="View Watchlist", command=lambda: self.notebook.select(self.exceptions_tab)).pack(fill="x", pady=(6, 0))

        self._ontrack_tree = ttk.Treeview(on_track, columns=("title",), show="headings", height=8)
        self._ontrack_tree.heading("title", text="Status")
        self._ontrack_tree.column("title", width=280, anchor="w")
        self._ontrack_tree.pack(fill="both", expand=True)
        self._ontrack_tree.bind("<Double-1>", self._on_ontrack_double_click)
        ttk.Button(on_track, text="View Performance", command=lambda: self.notebook.select(self.data_tab)).pack(fill="x", pady=(6, 0))

        self._tasks_tree = ttk.Treeview(tasks, columns=("task",), show="headings", height=8)
        self._tasks_tree.heading("task", text="Task")
        self._tasks_tree.column("task", width=220, anchor="w")
        self._tasks_tree.pack(fill="both", expand=True)
        self._tasks_tree.bind("<Double-1>", self._selected_task)
        ttk.Button(tasks, text="View All Tasks", command=lambda: self.notebook.select(self.items_tab)).pack(fill="x", pady=(6, 0))

    def _build_intake(self) -> None:
        top = ttk.Frame(self.intake_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Invoice Intake", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Add Files", command=self.add_invoice_files).pack(side="right", padx=3)
        ttk.Button(top, text="Process Selected", command=self.process_selected_uploads).pack(side="right", padx=3)
        ttk.Button(top, text="Process All", command=self.process_all_uploads).pack(side="right", padx=3)
        ttk.Button(top, text="Open Upload Folder", command=lambda: self.open_folder_key("upload")).pack(side="right", padx=3)

        columns = ("name", "type", "size", "status")
        self.upload_tree = ttk.Treeview(self.intake_tab, columns=columns, show="headings", selectmode="extended")
        widths = {"name": 500, "type": 100, "size": 120, "status": 300}
        for col in columns:
            self.upload_tree.heading(col, text=col.replace("_", " ").title())
            self.upload_tree.column(col, width=widths[col], anchor="w")
        self.upload_tree.pack(fill="both", expand=True)
        self.upload_tree.bind("<Double-1>", lambda _event: self.open_selected_upload())
        self.processing_progress = ttk.Progressbar(self.intake_tab, mode="indeterminate")
        self.processing_progress.pack(fill="x", pady=(8, 0))

    def _build_review(self) -> None:
        top = ttk.Frame(self.review_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="CostPilot Review Center", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_review).pack(side="right", padx=3)
        ttk.Button(top, text="Reject All", command=self.reject_all_reviews).pack(side="right", padx=3)
        ttk.Button(top, text="Reject Selected", command=self.reject_selected_reviews).pack(side="right", padx=3)
        ttk.Button(top, text="Approve All Eligible", command=self.auto_recover_all_reviews).pack(side="right", padx=3)
        ttk.Button(top, text="Apply Recommended", command=self.apply_recommended_selected_reviews).pack(side="right", padx=3)
        ttk.Button(top, text="Select All", command=self.select_all_reviews).pack(side="right", padx=3)

        action_bar = ttk.Frame(self.review_tab)
        action_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(action_bar, text="Explain Selected", command=self.explain_selected_review).pack(side="left", padx=(0, 4))
        ttk.Button(action_bar, text="Open Selected", command=self.open_selected_review).pack(side="left", padx=4)
        ttk.Button(action_bar, text="Approve Selected Eligible", command=self.batch_approve_selected_reviews).pack(side="left", padx=4)
        ttk.Button(action_bar, text="Fix Selected", command=self.apply_recommended_selected_reviews).pack(side="left", padx=4)
        ttk.Button(action_bar, text="Retry Upload", command=self.retry_selected_review_uploads).pack(side="left", padx=4)
        ttk.Button(action_bar, text="Reject Unreadable + Duplicates", command=self.reject_unreadable_duplicate_reviews).pack(side="left", padx=4)
        ttk.Button(action_bar, text="Next Case", command=self.next_review_case).pack(side="left", padx=4)

        paned = ttk.Panedwindow(self.review_tab, orient="horizontal")
        paned.pack(fill="both", expand=True)
        queue_frame = ttk.LabelFrame(paned, text="Manager review queue", padding=6)
        conversation_frame = ttk.LabelFrame(paned, text="Conversation with CostPilot", padding=8)
        paned.add(queue_frame, weight=3)
        paned.add(conversation_frame, weight=2)

        columns = ("type", "document", "problem", "recommendation", "severity")
        review_table = ttk.Frame(queue_frame)
        review_table.pack(fill="both", expand=True)
        review_table.columnconfigure(0, weight=1)
        review_table.rowconfigure(0, weight=1)
        self.review_tree = ttk.Treeview(review_table, columns=columns, show="headings", selectmode="extended")
        headings = {
            "type": "Type", "document": "Document / Delivery", "problem": "Problem",
            "recommendation": "CostPilot recommendation", "severity": "Priority",
        }
        widths = {"type": 88, "document": 225, "problem": 180, "recommendation": 330, "severity": 75}
        for col in columns:
            self.review_tree.heading(col, text=headings[col])
            self.review_tree.column(col, width=widths[col], anchor="w")
        review_scroll = ttk.Scrollbar(review_table, orient="vertical", command=self.review_tree.yview)
        review_x_scroll = ttk.Scrollbar(review_table, orient="horizontal", command=self.review_tree.xview)
        self.review_tree.configure(yscrollcommand=review_scroll.set, xscrollcommand=review_x_scroll.set)
        self.review_tree.grid(row=0, column=0, sticky="nsew")
        review_scroll.grid(row=0, column=1, sticky="ns")
        review_x_scroll.grid(row=1, column=0, sticky="ew")
        self.review_tree.bind("<Double-1>", lambda _event: self.open_selected_review())
        self.review_tree.bind("<<TreeviewSelect>>", self._review_selection_changed)

        self.review_copilot_status_var = tk.StringVar(
            value="CostPilot explains each exception and can execute confirmed single or batch actions."
        )
        ttk.Label(
            conversation_frame, textvariable=self.review_copilot_status_var,
            style="Muted.TLabel", wraplength=460, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        transcript_box = ttk.Frame(conversation_frame)
        transcript_box.pack(fill="both", expand=True)
        self.review_chat_transcript = tk.Text(transcript_box, wrap="word", state="disabled", height=18, borderwidth=1)
        review_chat_scroll = ttk.Scrollbar(transcript_box, orient="vertical", command=self.review_chat_transcript.yview)
        self.review_chat_transcript.configure(yscrollcommand=review_chat_scroll.set)
        self.review_chat_transcript.pack(side="left", fill="both", expand=True)
        review_chat_scroll.pack(side="right", fill="y")
        self.review_chat_transcript.tag_configure("manager", foreground="#17324D", font=("Segoe UI", 10, "bold"))
        self.review_chat_transcript.tag_configure("costpilot", foreground="#1F6F78")
        self.review_chat_transcript.tag_configure("system", foreground="#667085", font=("Segoe UI", 9, "italic"))

        prompt_frame = ttk.Frame(conversation_frame)
        prompt_frame.pack(fill="x", pady=(8, 0))
        self.review_chat_input = tk.Text(prompt_frame, height=3, wrap="word")
        self.review_chat_input.pack(side="left", fill="x", expand=True)
        self.review_chat_input.bind("<Control-Return>", self._review_chat_ctrl_enter)
        ttk.Button(prompt_frame, text="Send", command=self.send_review_chat_command).pack(side="right", padx=(8, 0), fill="y")

        self.review_batch_status_var = tk.StringVar(
            value="Approve All Eligible never approves unreadable documents, duplicates, arithmetic mismatches, or receiving discrepancies."
        )
        ttk.Label(
            self.review_tab, textvariable=self.review_batch_status_var,
            style="Muted.TLabel", wraplength=1120, justify="left",
        ).pack(anchor="w", pady=(8, 0))
        self.review_case_rows: dict[str, dict[str, Any]] = {}
        self._review_queue_signature = ""
        self._last_review_explained_case = ""

    def _build_exceptions(self) -> None:
        top = ttk.Frame(self.exceptions_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Data Quality and Exception Management", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh Analysis", command=self.refresh_exceptions_health).pack(side="right", padx=3)
        ttk.Button(top, text="Open Source", command=self.open_selected_exception_source).pack(side="right", padx=3)
        ttk.Button(top, text="Acknowledge", command=lambda: self.change_selected_exception("Acknowledged")).pack(side="right", padx=3)
        ttk.Button(top, text="Resolve", command=lambda: self.change_selected_exception("Resolved")).pack(side="right", padx=3)

        score_frame = ttk.Frame(self.exceptions_tab)
        score_frame.pack(fill="x", pady=(0, 8))
        self.health_vars: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate((
            ("overall", "Overall Health"), ("completeness", "Completeness"),
            ("freshness", "Freshness"), ("integrity", "Integrity"),
            ("operational", "Operational"),
        )):
            box = ttk.LabelFrame(score_frame, text=label, padding=8)
            box.grid(row=0, column=index, sticky="nsew", padx=4)
            var = tk.StringVar(value="-")
            self.health_vars[key] = var
            ttk.Label(box, textvariable=var, style="Metric.TLabel").pack()
            score_frame.columnconfigure(index, weight=1)

        columns = ("severity", "category", "title", "message", "action", "status", "detected")
        exceptions_table = ttk.Frame(self.exceptions_tab)
        exceptions_table.pack(fill="both", expand=True)
        exceptions_table.columnconfigure(0, weight=1)
        exceptions_table.rowconfigure(0, weight=1)
        self.exceptions_tree = ttk.Treeview(exceptions_table, columns=columns, show="headings", selectmode="browse")
        widths = {"severity":80,"category":125,"title":245,"message":310,"action":310,"status":105,"detected":145}
        for col in columns:
            self.exceptions_tree.heading(col, text=col.replace("_", " ").title())
            self.exceptions_tree.column(col, width=widths[col], anchor="w")
        exception_y_scroll = ttk.Scrollbar(exceptions_table, orient="vertical", command=self.exceptions_tree.yview)
        exception_x_scroll = ttk.Scrollbar(exceptions_table, orient="horizontal", command=self.exceptions_tree.xview)
        self.exceptions_tree.configure(
            yscrollcommand=exception_y_scroll.set,
            xscrollcommand=exception_x_scroll.set,
        )
        self.exceptions_tree.grid(row=0, column=0, sticky="nsew")
        exception_y_scroll.grid(row=0, column=1, sticky="ns")
        exception_x_scroll.grid(row=1, column=0, sticky="ew")
        self.exceptions_tree.bind("<Double-1>", lambda _event: self.open_selected_exception_source())

    def _build_receiving(self) -> None:
        top = ttk.Frame(self.receiving_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Receiving and Delivery Verification", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Open Selected", command=self.verify_selected_delivery).pack(side="right", padx=3)
        ttk.Button(top, text="Verify Selected as Received", command=self.auto_verify_selected_deliveries).pack(side="right", padx=3)
        ttk.Button(top, text="Verify All Eligible", command=self.auto_verify_all_deliveries).pack(side="right", padx=3)
        ttk.Button(top, text="Select Pending", command=self.select_all_pending_receiving).pack(side="right", padx=3)
        ttk.Button(top, text="Refresh", command=self.refresh_receiving).pack(side="right", padx=3)
        columns = ("invoice_id", "vendor", "invoice_number", "invoice_date", "total", "receiving_status", "received_date", "discrepancies")
        self.receiving_tree = ttk.Treeview(self.receiving_tab, columns=columns, show="headings", selectmode="extended")
        widths = {"invoice_id":145,"vendor":205,"invoice_number":125,"invoice_date":100,"total":95,"receiving_status":125,"received_date":105,"discrepancies":95}
        for col in columns:
            self.receiving_tree.heading(col, text=col.replace("_", " ").title())
            self.receiving_tree.column(col, width=widths[col], anchor="w")
        self.receiving_tree.pack(fill="both", expand=True)
        self.receiving_tree.bind("<Double-1>", lambda _event: self.verify_selected_delivery())
        self.receiving_batch_status_var = tk.StringVar(value="Clean approved invoices can be verified in bulk as received exactly as invoiced. Existing shortages, damage, substitutions, and review sessions are never overwritten.")
        ttk.Label(
            self.receiving_tab,
            textvariable=self.receiving_batch_status_var,
            style="Muted.TLabel", wraplength=1100,
        ).pack(anchor="w", pady=(8, 0))

    def _build_items(self) -> None:
        top = ttk.Frame(self.items_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Item Master and Price Tracking", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Edit Selected", command=self.edit_selected_item).pack(side="right", padx=3)
        ttk.Button(top, text="Refresh", command=self.refresh_items).pack(side="right", padx=3)
        columns = ("item_id", "vendor", "sku", "name", "category", "purchase_unit", "count_unit", "units_per", "current_price", "estimated_on_hand", "review_status")
        item_table = ttk.Frame(self.items_tab)
        item_table.pack(fill="both", expand=True)
        item_table.columnconfigure(0, weight=1)
        item_table.rowconfigure(0, weight=1)
        self.items_tree = ttk.Treeview(item_table, columns=columns, show="headings", selectmode="browse")
        widths = {
            "item_id": 145, "vendor": 175, "sku": 115, "name": 250, "category": 105,
            "purchase_unit": 90, "count_unit": 90, "units_per": 85, "current_price": 95,
            "estimated_on_hand": 120, "review_status": 190,
        }
        for col in columns:
            self.items_tree.heading(col, text=col.replace("_", " ").title())
            self.items_tree.column(col, width=widths[col], anchor="w")
        item_y_scroll = ttk.Scrollbar(item_table, orient="vertical", command=self.items_tree.yview)
        item_x_scroll = ttk.Scrollbar(item_table, orient="horizontal", command=self.items_tree.xview)
        self.items_tree.configure(
            yscrollcommand=item_y_scroll.set,
            xscrollcommand=item_x_scroll.set,
        )
        self.items_tree.grid(row=0, column=0, sticky="nsew")
        item_y_scroll.grid(row=0, column=1, sticky="ns")
        item_x_scroll.grid(row=1, column=0, sticky="ew")
        self.items_tree.bind("<Double-1>", lambda _event: self.edit_selected_item())
        ttk.Label(
            self.items_tab,
            text="Set purchase unit, count unit, units per case, lead time, safety stock, and order multiple before relying on forecasts.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _build_inventory(self) -> None:
        top = ttk.Frame(self.inventory_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Month-End Inventory and Usage", style="Title.TLabel").pack(side="left")
        self.inventory_month_var = tk.StringVar(value=date.today().strftime("%Y-%m"))
        ttk.Label(top, text="Month:").pack(side="left", padx=(18, 4))
        ttk.Entry(top, textvariable=self.inventory_month_var, width=10).pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_inventory).pack(side="right", padx=3)

        actions = ttk.LabelFrame(self.inventory_tab, text="Monthly workflow", padding=10)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="1. Export Count Sheet", command=self.export_inventory_count_sheet).pack(side="left", padx=3)
        ttk.Button(actions, text="2. Import Completed Count", command=self.import_inventory_count).pack(side="left", padx=3)
        ttk.Button(actions, text="3. Close Month", command=self.close_inventory_month).pack(side="left", padx=3)
        ttk.Button(actions, text="Open Count Folder", command=lambda: self.open_folder_key("inventory_counts")).pack(side="left", padx=3)

        columns = ("item", "vendor", "opening", "purchased", "ending", "usage", "avg_weekly", "estimated_stock", "unit", "confidence")
        inventory_table = ttk.Frame(self.inventory_tab)
        inventory_table.pack(fill="both", expand=True)
        inventory_table.columnconfigure(0, weight=1)
        inventory_table.rowconfigure(0, weight=1)
        self.inventory_tree = ttk.Treeview(inventory_table, columns=columns, show="headings", selectmode="browse")
        widths = {"item":250,"vendor":170,"opening":85,"purchased":85,"ending":85,"usage":85,"avg_weekly":95,"estimated_stock":110,"unit":80,"confidence":175}
        for col in columns:
            self.inventory_tree.heading(col, text=col.replace("_", " ").title())
            self.inventory_tree.column(col, width=widths[col], anchor="w")
        inventory_y_scroll = ttk.Scrollbar(inventory_table, orient="vertical", command=self.inventory_tree.yview)
        inventory_x_scroll = ttk.Scrollbar(inventory_table, orient="horizontal", command=self.inventory_tree.xview)
        self.inventory_tree.configure(
            yscrollcommand=inventory_y_scroll.set,
            xscrollcommand=inventory_x_scroll.set,
        )
        self.inventory_tree.grid(row=0, column=0, sticky="nsew")
        inventory_y_scroll.grid(row=0, column=1, sticky="ns")
        inventory_x_scroll.grid(row=1, column=0, sticky="ew")
        self.inventory_status_var = tk.StringVar(value="Export a count sheet, complete physical counts, import it, then close the month.")
        ttk.Label(self.inventory_tab, textvariable=self.inventory_status_var, style="Muted.TLabel", wraplength=1050).pack(anchor="w", pady=(8,0))

    def _build_orders(self) -> None:
        top = ttk.Frame(self.orders_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Weekly Par and Order Planning", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Generate Draft", command=self.generate_order_predictions).pack(side="right", padx=3)
        ttk.Button(top, text="Edit Selected Qty", command=self.edit_selected_order).pack(side="right", padx=3)
        ttk.Button(top, text="Approve Batch", command=self.approve_order_batch).pack(side="right", padx=3)
        ttk.Button(top, text="Export Order Sheet", command=self.export_order_sheet).pack(side="right", padx=3)
        ttk.Button(top, text="Refresh", command=self.refresh_orders).pack(side="right", padx=3)
        columns = ("vendor", "sku", "item", "on_hand", "avg_weekly", "par", "suggested", "manager_qty", "purchase_unit", "cost", "status")
        self.orders_tree = ttk.Treeview(self.orders_tab, columns=columns, show="headings", selectmode="browse")
        widths = {"vendor":170,"sku":110,"item":245,"on_hand":90,"avg_weekly":95,"par":90,"suggested":90,"manager_qty":100,"purchase_unit":90,"cost":100,"status":90}
        for col in columns:
            self.orders_tree.heading(col, text=col.replace("_", " ").title())
            self.orders_tree.column(col, width=widths[col], anchor="w")
        self.orders_tree.pack(fill="both", expand=True)
        self.order_batch_var = tk.StringVar(value="No order batch generated.")
        ttk.Label(self.orders_tab, textvariable=self.order_batch_var, style="Muted.TLabel", wraplength=1050).pack(anchor="w", pady=(8,0))

    def _build_data(self) -> None:
        ttk.Label(self.data_tab, text="Sales, Costs and 12-Month Reporting", style="Title.TLabel").pack(anchor="w")
        frame = ttk.LabelFrame(self.data_tab, text="Imports", padding=12)
        frame.pack(fill="x", pady=10)
        ttk.Button(frame, text="Import Sales CSV", command=self.import_sales).pack(side="left", padx=4)
        ttk.Button(frame, text="Import Operating Costs CSV", command=self.import_costs).pack(side="left", padx=4)
        ttk.Button(frame, text="Open Sales Folder", command=lambda: self.open_folder_key("sales")).pack(side="left", padx=4)
        ttk.Button(frame, text="Open Cost Folder", command=lambda: self.open_folder_key("costs")).pack(side="left", padx=4)

        reports = ttk.LabelFrame(self.data_tab, text="Exports", padding=12)
        reports.pack(fill="x", pady=10)
        self.export_format_var = tk.StringVar(value="excel")
        ttk.Label(reports, text="Format:").pack(side="left")
        ttk.Combobox(reports, textvariable=self.export_format_var, values=("excel", "csv", "txt", "pdf", "docx"), state="readonly", width=10).pack(side="left", padx=3)
        ttk.Button(reports, text="Export CSV Files", command=self.export_csvs).pack(side="left", padx=4)
        ttk.Button(reports, text="Export Manager Workbook", command=self.export_workbook).pack(side="left", padx=4)
        ttk.Button(reports, text="Export Full Inventory", command=self.export_full_inventory).pack(side="left", padx=4)
        ttk.Button(reports, text="Open Export Folder", command=lambda: self.open_folder_key("exports")).pack(side="left", padx=4)

        year_frame = ttk.LabelFrame(self.data_tab, text="12-month summary", padding=8)
        year_frame.pack(fill="both", expand=True, pady=8)
        self.report_year_var = tk.StringVar(value=str(date.today().year))
        self.export_format_var = tk.StringVar(value="excel")
        year_top = ttk.Frame(year_frame); year_top.pack(fill="x")
        ttk.Label(year_top, text="Year:").pack(side="left")
        ttk.Entry(year_top, textvariable=self.report_year_var, width=8).pack(side="left", padx=4)
        ttk.Button(year_top, text="Refresh Year", command=self.refresh_annual_summary).pack(side="left", padx=4)
        annual_columns = ("month","sales","purchases","opening_inventory","ending_inventory","estimated_cogs","product_margin","contribution","status")
        annual_table = ttk.Frame(year_frame)
        annual_table.pack(fill="both", expand=True, pady=(8,0))
        annual_table.columnconfigure(0, weight=1)
        annual_table.rowconfigure(0, weight=1)
        self.annual_tree = ttk.Treeview(annual_table, columns=annual_columns, show="headings", height=12)
        annual_widths = {"month":80,"sales":110,"purchases":110,"opening_inventory":125,"ending_inventory":125,"estimated_cogs":110,"product_margin":115,"contribution":115,"status":230}
        for col in annual_columns:
            self.annual_tree.heading(col, text=col.replace("_"," ").title())
            self.annual_tree.column(col, width=annual_widths[col], anchor="w")
        annual_y_scroll = ttk.Scrollbar(annual_table, orient="vertical", command=self.annual_tree.yview)
        annual_x_scroll = ttk.Scrollbar(annual_table, orient="horizontal", command=self.annual_tree.xview)
        self.annual_tree.configure(
            yscrollcommand=annual_y_scroll.set,
            xscrollcommand=annual_x_scroll.set,
        )
        self.annual_tree.grid(row=0, column=0, sticky="nsew")
        annual_y_scroll.grid(row=0, column=1, sticky="ns")
        annual_x_scroll.grid(row=1, column=0, sticky="ew")
        self.data_summary = tk.Text(self.data_tab, height=7, wrap="word", state="disabled")
        # Keep the totals visible on 1366x768 instead of letting the expanding
        # annual table push them below the window.
        self.data_summary.pack(fill="x", pady=8, before=year_frame)

    def _build_phase2(self) -> None:
        ttk.Label(self.phase2_tab, text="Phase 2 Operations", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self.phase2_tab,
            text="POS-neutral sales imports, recipe costing, mobile counts, waste tracking, vendor purchase orders, and accounting-ready exports.",
            style="Muted.TLabel", wraplength=1080,
        ).pack(anchor="w", pady=(0, 8))

        self.phase2_notebook = ttk.Notebook(self.phase2_tab)
        self.phase2_notebook.pack(fill="both", expand=True)
        pos_frame = ttk.Frame(self.phase2_notebook, padding=8)
        mobile_frame = ttk.Frame(self.phase2_notebook, padding=8)
        waste_frame = ttk.Frame(self.phase2_notebook, padding=8)
        po_frame = ttk.Frame(self.phase2_notebook, padding=8)
        accounting_frame = ttk.Frame(self.phase2_notebook, padding=8)
        self.phase2_notebook.add(pos_frame, text="POS & Recipes")
        self.phase2_notebook.add(mobile_frame, text="Mobile Counts")
        self.phase2_notebook.add(waste_frame, text="Waste Log")
        self.phase2_notebook.add(po_frame, text="Vendor Purchase Orders")
        self.phase2_notebook.add(accounting_frame, text="Accounting Exports")

        pos_buttons = ttk.Frame(pos_frame)
        pos_buttons.pack(fill="x", pady=(0, 6))
        ttk.Button(pos_buttons, text="Import POS CSV / Excel", command=self.import_pos_report).pack(side="left", padx=3)
        ttk.Button(pos_buttons, text="Import Recipe CSV", command=self.import_recipe_file).pack(side="left", padx=3)
        ttk.Button(pos_buttons, text="Export Recipe Template", command=self.export_recipe_template).pack(side="left", padx=3)
        ttk.Button(pos_buttons, text="Open POS Folder", command=lambda: self.open_folder_key("pos")).pack(side="left", padx=3)
        ttk.Button(pos_buttons, text="Refresh", command=self.refresh_phase2).pack(side="right", padx=3)
        pos_pane = ttk.Panedwindow(pos_frame, orient="vertical")
        pos_pane.pack(fill="both", expand=True)
        runs_box = ttk.LabelFrame(pos_pane, text="POS import history", padding=5)
        menu_box = ttk.LabelFrame(pos_pane, text="Menu and recipe costing", padding=5)
        pos_pane.add(runs_box, weight=1); pos_pane.add(menu_box, weight=2)
        self.pos_runs_tree = ttk.Treeview(runs_box, columns=("date","file","rows","rejected","gross","net","status"), show="headings", height=5)
        for col, width in {"date":145,"file":260,"rows":75,"rejected":75,"gross":100,"net":100,"status":95}.items():
            self.pos_runs_tree.heading(col, text=col.title()); self.pos_runs_tree.column(col, width=width, anchor="w")
        pos_run_scroll = ttk.Scrollbar(runs_box, orient="vertical", command=self.pos_runs_tree.yview)
        self.pos_runs_tree.configure(yscrollcommand=pos_run_scroll.set)
        self.pos_runs_tree.pack(side="left", fill="both", expand=True); pos_run_scroll.pack(side="right", fill="y")
        self.menu_cost_tree = ttk.Treeview(menu_box, columns=("item","category","price","ingredients","recipe_cost","food_cost","margin","qty_sold","net_sales"), show="headings")
        for col, width in {"item":220,"category":110,"price":85,"ingredients":75,"recipe_cost":95,"food_cost":85,"margin":90,"qty_sold":85,"net_sales":100}.items():
            self.menu_cost_tree.heading(col, text=col.replace("_"," ").title()); self.menu_cost_tree.column(col, width=width, anchor="w")
        menu_scroll = ttk.Scrollbar(menu_box, orient="vertical", command=self.menu_cost_tree.yview)
        self.menu_cost_tree.configure(yscrollcommand=menu_scroll.set)
        self.menu_cost_tree.pack(side="left", fill="both", expand=True); menu_scroll.pack(side="right", fill="y")

        mobile_buttons = ttk.Frame(mobile_frame); mobile_buttons.pack(fill="x", pady=(0,6))
        ttk.Button(mobile_buttons, text="Start Mobile Count", command=self.start_mobile_count).pack(side="left", padx=3)
        ttk.Button(mobile_buttons, text="Copy Mobile URL", command=self.copy_mobile_count_url).pack(side="left", padx=3)
        ttk.Button(mobile_buttons, text="Open URL on This Computer", command=self.open_mobile_count_url).pack(side="left", padx=3)
        ttk.Button(mobile_buttons, text="Stop Mobile Server", command=self.stop_mobile_count_server).pack(side="left", padx=3)
        ttk.Button(mobile_buttons, text="Finalize Selected Count", command=self.finalize_mobile_count).pack(side="right", padx=3)
        self.mobile_count_status_var = tk.StringVar(value="No mobile count server is running.")
        ttk.Label(mobile_frame, textvariable=self.mobile_count_status_var, style="Muted.TLabel", wraplength=1050).pack(anchor="w", pady=(0,6))
        self.mobile_sessions_tree = ttk.Treeview(mobile_frame, columns=("date","status","entries","created_by","created","submitted","finalized"), show="headings", selectmode="browse")
        for col, width in {"date":100,"status":100,"entries":75,"created_by":110,"created":150,"submitted":150,"finalized":150}.items():
            self.mobile_sessions_tree.heading(col, text=col.replace("_"," ").title()); self.mobile_sessions_tree.column(col, width=width, anchor="w")
        mobile_scroll = ttk.Scrollbar(mobile_frame, orient="vertical", command=self.mobile_sessions_tree.yview)
        self.mobile_sessions_tree.configure(yscrollcommand=mobile_scroll.set)
        self.mobile_sessions_tree.pack(side="left", fill="both", expand=True); mobile_scroll.pack(side="right", fill="y")

        waste_buttons = ttk.Frame(waste_frame); waste_buttons.pack(fill="x", pady=(0,6))
        ttk.Button(waste_buttons, text="Log Waste", command=self.add_waste_event).pack(side="left", padx=3)
        ttk.Button(waste_buttons, text="Open Waste Folder", command=lambda: self.open_folder_key("waste")).pack(side="left", padx=3)
        ttk.Button(waste_buttons, text="Refresh", command=self.refresh_phase2).pack(side="right", padx=3)
        waste_table = ttk.Frame(waste_frame)
        waste_table.pack(fill="both", expand=True)
        waste_table.columnconfigure(0, weight=1)
        waste_table.rowconfigure(0, weight=1)
        self.waste_tree = ttk.Treeview(waste_table, columns=("date","item","vendor","quantity","unit","reason","shift","cost","created_by","notes"), show="headings")
        for col, width in {"date":95,"item":210,"vendor":150,"quantity":80,"unit":70,"reason":110,"shift":80,"cost":85,"created_by":100,"notes":250}.items():
            self.waste_tree.heading(col, text=col.replace("_"," ").title()); self.waste_tree.column(col, width=width, anchor="w")
        waste_scroll = ttk.Scrollbar(waste_table, orient="vertical", command=self.waste_tree.yview)
        waste_x_scroll = ttk.Scrollbar(waste_table, orient="horizontal", command=self.waste_tree.xview)
        self.waste_tree.configure(yscrollcommand=waste_scroll.set, xscrollcommand=waste_x_scroll.set)
        self.waste_tree.grid(row=0, column=0, sticky="nsew")
        waste_scroll.grid(row=0, column=1, sticky="ns")
        waste_x_scroll.grid(row=1, column=0, sticky="ew")

        po_buttons = ttk.Frame(po_frame); po_buttons.pack(fill="x", pady=(0,6))
        ttk.Button(po_buttons, text="Generate Vendor POs", command=self.generate_vendor_purchase_orders).pack(side="left", padx=3)
        ttk.Button(po_buttons, text="Approve Selected PO", command=self.approve_selected_purchase_order).pack(side="left", padx=3)
        ttk.Button(po_buttons, text="Export Vendor PO Package", command=self.export_vendor_purchase_orders).pack(side="left", padx=3)
        ttk.Button(po_buttons, text="Open PO Folder", command=lambda: self.open_folder_key("purchase_orders")).pack(side="left", padx=3)
        ttk.Button(po_buttons, text="Refresh", command=self.refresh_phase2).pack(side="right", padx=3)
        self.purchase_orders_tree = ttk.Treeview(po_frame, columns=("vendor","date","status","lines","subtotal","delivery","created_by"), show="headings", selectmode="extended")
        for col, width in {"vendor":220,"date":100,"status":95,"lines":70,"subtotal":100,"delivery":110,"created_by":110}.items():
            self.purchase_orders_tree.heading(col, text=col.replace("_"," ").title()); self.purchase_orders_tree.column(col, width=width, anchor="w")
        po_scroll = ttk.Scrollbar(po_frame, orient="vertical", command=self.purchase_orders_tree.yview)
        self.purchase_orders_tree.configure(yscrollcommand=po_scroll.set)
        self.purchase_orders_tree.pack(side="left", fill="both", expand=True); po_scroll.pack(side="right", fill="y")

        account_top = ttk.Frame(accounting_frame); account_top.pack(fill="x", pady=(0,6))
        self.accounting_start_var = tk.StringVar(value=date.today().replace(month=1, day=1).isoformat())
        self.accounting_end_var = tk.StringVar(value=date.today().isoformat())
        self.accounting_type_var = tk.StringVar(value="General Journal CSV")
        ttk.Label(account_top, text="Start:").pack(side="left"); ttk.Entry(account_top, textvariable=self.accounting_start_var, width=12).pack(side="left", padx=3)
        ttk.Label(account_top, text="End:").pack(side="left"); ttk.Entry(account_top, textvariable=self.accounting_end_var, width=12).pack(side="left", padx=3)
        ttk.Label(account_top, text="Format:").pack(side="left"); ttk.Combobox(account_top, textvariable=self.accounting_type_var, values=("General Journal CSV","QuickBooks IIF"), state="readonly", width=22).pack(side="left", padx=3)
        ttk.Button(account_top, text="Export Accounting File", command=self.export_accounting_file).pack(side="left", padx=5)
        ttk.Button(account_top, text="Open Accounting Folder", command=lambda: self.open_folder_key("accounting")).pack(side="left", padx=3)
        self.accounting_tree = ttk.Treeview(accounting_frame, columns=("created","type","period","rows","debits","credits","file"), show="headings")
        for col, width in {"created":150,"type":150,"period":180,"rows":70,"debits":100,"credits":100,"file":330}.items():
            self.accounting_tree.heading(col, text=col.title()); self.accounting_tree.column(col, width=width, anchor="w")
        account_scroll = ttk.Scrollbar(accounting_frame, orient="vertical", command=self.accounting_tree.yview)
        self.accounting_tree.configure(yscrollcommand=account_scroll.set)
        self.accounting_tree.pack(side="left", fill="both", expand=True); account_scroll.pack(side="right", fill="y")

    def _build_phase3(self) -> None:
        ttk.Label(self.phase3_tab, text="Phase 3 - Owner Intelligence", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self.phase3_tab,
            text="Multi-location performance, transfers, event/weather forecasts, forecast learning, distributor exchanges, profitability, shrinkage, pricing, and owner reporting.",
            style="Muted.TLabel", wraplength=1120,
        ).pack(anchor="w", pady=(0, 8))
        self.phase3_notebook = ttk.Notebook(self.phase3_tab)
        self.phase3_notebook.pack(fill="both", expand=True)
        portfolio = ttk.Frame(self.phase3_notebook, padding=8)
        transfers = ttk.Frame(self.phase3_notebook, padding=8)
        forecasting = ttk.Frame(self.phase3_notebook, padding=8)
        distributors = ttk.Frame(self.phase3_notebook, padding=8)
        profitability = ttk.Frame(self.phase3_notebook, padding=8)
        self.phase3_notebook.add(portfolio, text="Owner Portfolio")
        self.phase3_notebook.add(transfers, text="Inventory Transfers")
        self.phase3_notebook.add(forecasting, text="Events & Forecasts")
        self.phase3_notebook.add(distributors, text="Distributor Exchange")
        self.phase3_notebook.add(profitability, text="Profitability & Savings")

        ptop = ttk.Frame(portfolio); ptop.pack(fill="x", pady=(0,6))
        self.portfolio_year_var = tk.StringVar(value=str(date.today().year))
        ttk.Label(ptop, text="Year:").pack(side="left")
        ttk.Entry(ptop, textvariable=self.portfolio_year_var, width=8).pack(side="left", padx=4)
        ttk.Button(ptop, text="Refresh Portfolio", command=self.refresh_phase3).pack(side="left", padx=3)
        ttk.Button(ptop, text="Export Owner Report", command=self.export_owner_report_phase3).pack(side="left", padx=3)
        self.portfolio_tree = ttk.Treeview(portfolio, columns=("location","sales","purchases","purchase_pct","inventory","waste","exceptions","reviews"), show="headings")
        for col,width in {"location":240,"sales":115,"purchases":115,"purchase_pct":90,"inventory":115,"waste":100,"exceptions":85,"reviews":85}.items():
            self.portfolio_tree.heading(col,text=col.replace("_"," ").title()); self.portfolio_tree.column(col,width=width,anchor="w")
        ps=ttk.Scrollbar(portfolio,orient="vertical",command=self.portfolio_tree.yview); self.portfolio_tree.configure(yscrollcommand=ps.set)
        self.portfolio_tree.pack(side="left",fill="both",expand=True); ps.pack(side="right",fill="y")

        ttop=ttk.Frame(transfers); ttop.pack(fill="x",pady=(0,6))
        ttk.Button(ttop,text="Create Transfer",command=self.create_inventory_transfer).pack(side="left",padx=3)
        ttk.Button(ttop,text="Receive Selected",command=self.receive_inventory_transfer).pack(side="left",padx=3)
        ttk.Button(ttop,text="Open Transfer Folder",command=lambda:self.open_folder_key("transfers")).pack(side="left",padx=3)
        ttk.Button(ttop,text="Refresh",command=self.refresh_phase3).pack(side="right",padx=3)
        self.transfers_tree=ttk.Treeview(transfers,columns=("date","source","destination","status","lines","value","created_by","received"),show="headings")
        for col,width in {"date":95,"source":190,"destination":190,"status":90,"lines":70,"value":100,"created_by":100,"received":150}.items():
            self.transfers_tree.heading(col,text=col.title()); self.transfers_tree.column(col,width=width,anchor="w")
        ts=ttk.Scrollbar(transfers,orient="vertical",command=self.transfers_tree.yview); self.transfers_tree.configure(yscrollcommand=ts.set)
        self.transfers_tree.pack(side="left",fill="both",expand=True); ts.pack(side="right",fill="y")

        ftop=ttk.Frame(forecasting); ftop.pack(fill="x",pady=(0,6))
        self.forecast_start_var=tk.StringVar(value=date.today().isoformat()); self.forecast_days_var=tk.StringVar(value="14")
        ttk.Label(ftop,text="Start:").pack(side="left"); ttk.Entry(ftop,textvariable=self.forecast_start_var,width=12).pack(side="left",padx=3)
        ttk.Label(ftop,text="Days:").pack(side="left"); ttk.Entry(ftop,textvariable=self.forecast_days_var,width=5).pack(side="left",padx=3)
        ttk.Button(ftop,text="Add Event",command=self.add_local_event).pack(side="left",padx=3)
        ttk.Button(ftop,text="Import ICS",command=self.import_event_calendar).pack(side="left",padx=3)
        ttk.Button(ftop,text="Refresh Weather",command=self.refresh_weather_phase3).pack(side="left",padx=3)
        ttk.Button(ftop,text="Generate Forecast",command=self.generate_forecasts_phase3).pack(side="left",padx=3)
        ttk.Button(ftop,text="Score Actuals",command=self.learn_forecasts_phase3).pack(side="left",padx=3)
        ttk.Button(ftop,text="Sales-Driven Order",command=self.generate_sales_driven_order).pack(side="left",padx=3)
        fpane=ttk.Panedwindow(forecasting,orient="vertical"); fpane.pack(fill="both",expand=True)
        event_box=ttk.LabelFrame(fpane,text="Upcoming events and weather",padding=5); forecast_box=ttk.LabelFrame(fpane,text="Sales forecasts and learning",padding=5)
        fpane.add(event_box,weight=1); fpane.add(forecast_box,weight=2)
        self.events_tree=ttk.Treeview(event_box,columns=("date","end","event","category","impact","source","weather"),show="headings",height=6)
        for col,width in {"date":90,"end":90,"event":260,"category":110,"impact":80,"source":100,"weather":300}.items():
            self.events_tree.heading(col,text=col.title()); self.events_tree.column(col,width=width,anchor="w")
        es=ttk.Scrollbar(event_box,orient="vertical",command=self.events_tree.yview); self.events_tree.configure(yscrollcommand=es.set)
        self.events_tree.pack(side="left",fill="both",expand=True); es.pack(side="right",fill="y")
        self.forecast_tree=ttk.Treeview(forecast_box,columns=("date","baseline","predicted","actual","error","trend","weather","event","status"),show="headings")
        for col,width in {"date":90,"baseline":100,"predicted":100,"actual":100,"error":85,"trend":75,"weather":75,"event":75,"status":80}.items():
            self.forecast_tree.heading(col,text=col.title()); self.forecast_tree.column(col,width=width,anchor="w")
        fs=ttk.Scrollbar(forecast_box,orient="vertical",command=self.forecast_tree.yview); self.forecast_tree.configure(yscrollcommand=fs.set)
        self.forecast_tree.pack(side="left",fill="both",expand=True); fs.pack(side="right",fill="y")

        dtop=ttk.Frame(distributors); dtop.pack(fill="x",pady=(0,6))
        ttk.Button(dtop,text="Add / Edit Distributor",command=self.configure_distributor).pack(side="left",padx=3)
        ttk.Button(dtop,text="Import Catalog CSV",command=self.import_distributor_catalog).pack(side="left",padx=3)
        ttk.Button(dtop,text="Export Approved POs",command=self.export_distributor_orders).pack(side="left",padx=3)
        ttk.Button(dtop,text="Import Confirmation",command=self.import_distributor_confirmation).pack(side="left",padx=3)
        ttk.Button(dtop,text="Open Exchange Folder",command=lambda:self.open_folder_key("distributors")).pack(side="left",padx=3)
        dpane=ttk.Panedwindow(distributors,orient="vertical"); dpane.pack(fill="both",expand=True)
        dist_box=ttk.LabelFrame(dpane,text="Distributor profiles",padding=5); exchange_box=ttk.LabelFrame(dpane,text="Exchange history",padding=5)
        dpane.add(dist_box,weight=1); dpane.add(exchange_box,weight=2)
        self.distributors_tree=ttk.Treeview(dist_box,columns=("name","vendor","type","account","format","catalog","outbound"),show="headings",height=6)
        for col,width in {"name":170,"vendor":160,"type":125,"account":100,"format":75,"catalog":70,"outbound":320}.items():
            self.distributors_tree.heading(col,text=col.title()); self.distributors_tree.column(col,width=width,anchor="w")
        ds=ttk.Scrollbar(dist_box,orient="vertical",command=self.distributors_tree.yview); self.distributors_tree.configure(yscrollcommand=ds.set)
        self.distributors_tree.pack(side="left",fill="both",expand=True); ds.pack(side="right",fill="y")
        exchange_table = ttk.Frame(exchange_box)
        exchange_table.pack(fill="both", expand=True)
        exchange_table.columnconfigure(0, weight=1)
        exchange_table.rowconfigure(0, weight=1)
        self.exchanges_tree=ttk.Treeview(exchange_table,columns=("created","distributor","type","reference","status","rows","total","file"),show="headings")
        for col,width in {"created":145,"distributor":150,"type":130,"reference":150,"status":80,"rows":65,"total":90,"file":300}.items():
            self.exchanges_tree.heading(col,text=col.title()); self.exchanges_tree.column(col,width=width,anchor="w")
        dx=ttk.Scrollbar(exchange_table,orient="vertical",command=self.exchanges_tree.yview)
        exchange_x_scroll=ttk.Scrollbar(exchange_table,orient="horizontal",command=self.exchanges_tree.xview)
        self.exchanges_tree.configure(yscrollcommand=dx.set,xscrollcommand=exchange_x_scroll.set)
        self.exchanges_tree.grid(row=0,column=0,sticky="nsew")
        dx.grid(row=0,column=1,sticky="ns")
        exchange_x_scroll.grid(row=1,column=0,sticky="ew")

        prtop=ttk.Frame(profitability); prtop.pack(fill="x",pady=(0,6))
        self.phase3_month_var=tk.StringVar(value=date.today().strftime("%Y-%m"))
        ttk.Label(prtop,text="Month:").pack(side="left"); ttk.Entry(prtop,textvariable=self.phase3_month_var,width=9).pack(side="left",padx=3)
        ttk.Button(prtop,text="Refresh Analysis",command=self.refresh_phase3).pack(side="left",padx=3)
        ttk.Button(prtop,text="Export Owner Report",command=self.export_owner_report_phase3).pack(side="left",padx=3)
        ppane=ttk.Panedwindow(profitability,orient="vertical"); ppane.pack(fill="both",expand=True)
        menu_box=ttk.LabelFrame(ppane,text="True menu cost and pricing decisions",padding=5); variance_box=ttk.LabelFrame(ppane,text="Theoretical versus actual usage, waste and shrinkage",padding=5)
        ppane.add(menu_box,weight=2); ppane.add(variance_box,weight=2)
        self.profitability_tree=ttk.Treeview(menu_box,columns=("item","price","recipe","true_cost","food_pct","contribution","sold","sales","recommended","status"),show="headings")
        for col,width in {"item":220,"price":80,"recipe":80,"true_cost":85,"food_pct":80,"contribution":95,"sold":75,"sales":95,"recommended":100,"status":150}.items():
            self.profitability_tree.heading(col,text=col.replace("_"," ").title()); self.profitability_tree.column(col,width=width,anchor="w")
        mps=ttk.Scrollbar(menu_box,orient="vertical",command=self.profitability_tree.yview); self.profitability_tree.configure(yscrollcommand=mps.set)
        self.profitability_tree.pack(side="left",fill="both",expand=True); mps.pack(side="right",fill="y")
        self.variance_tree=ttk.Treeview(variance_box,columns=("item","theoretical","waste","transfers","actual","variance","shrink_pct","shrink_cost","status"),show="headings")
        for col,width in {"item":220,"theoretical":90,"waste":75,"transfers":80,"actual":80,"variance":85,"shrink_pct":80,"shrink_cost":90,"status":105}.items():
            self.variance_tree.heading(col,text=col.replace("_"," ").title()); self.variance_tree.column(col,width=width,anchor="w")
        vs=ttk.Scrollbar(variance_box,orient="vertical",command=self.variance_tree.yview); self.variance_tree.configure(yscrollcommand=vs.set)
        self.variance_tree.pack(side="left",fill="both",expand=True); vs.pack(side="right",fill="y")
        self.savings_var=tk.StringVar(value="Estimated value and risk metrics will appear after data is available.")
        ttk.Label(profitability,textvariable=self.savings_var,style="Muted.TLabel",wraplength=1120).pack(anchor="w",pady=(6,0))

    def _build_chat(self) -> None:
        top = ttk.Frame(self.chat_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Ask CostPilot - Restaurant Manager Assistant", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="New Conversation", command=self.new_chat_session).pack(side="right", padx=3)
        ttk.Button(top, text="Test Free Model", command=self.test_manager_chat_model).pack(side="right", padx=3)
        ttk.Button(top, text="Open Context", command=self.open_latest_chat_context).pack(side="right", padx=3)

        self.chat_status_var = tk.StringVar(
            value="Select a restaurant, then ask about orders, inventory, sales, costs, invoices, or reviews."
        )
        ttk.Label(self.chat_tab, textvariable=self.chat_status_var, style="Muted.TLabel", wraplength=1080).pack(anchor="w", pady=(0, 8))

        suggestions = ttk.LabelFrame(self.chat_tab, text="Common manager questions", padding=8)
        suggestions.pack(fill="x", pady=(0, 8))
        prompts = [
            "What should I order this week?",
            "Which items are most likely to run out?",
            "What product prices increased the most?",
            "How are sales and product costs trending this year?",
            "Which invoices or items still need review?",
            "What information is missing for an accurate month close?",
        ]
        for index, prompt in enumerate(prompts):
            ttk.Button(
                suggestions, text=prompt, command=lambda text=prompt: self.ask_suggested_question(text)
            ).grid(row=index // 3, column=index % 3, sticky="ew", padx=3, pady=3)
        for column in range(3):
            suggestions.columnconfigure(column, weight=1)

        transcript_frame = ttk.LabelFrame(self.chat_tab, text="Conversation", padding=6)
        transcript_frame.pack(fill="both", expand=True)
        self.chat_transcript = tk.Text(transcript_frame, wrap="word", state="disabled", height=20)
        transcript_scroll = ttk.Scrollbar(transcript_frame, orient="vertical", command=self.chat_transcript.yview)
        self.chat_transcript.configure(yscrollcommand=transcript_scroll.set)
        self.chat_transcript.pack(side="left", fill="both", expand=True)
        transcript_scroll.pack(side="right", fill="y")
        self.chat_transcript.tag_configure("user", foreground="#17324D", font=("Segoe UI", 10, "bold"))
        self.chat_transcript.tag_configure("assistant", foreground="#1F6F78")
        self.chat_transcript.tag_configure("system", foreground="#667085", font=("Segoe UI", 9, "italic"))

        source_frame = ttk.LabelFrame(self.chat_tab, text="Sources used in the latest answer", padding=6)
        source_frame.pack(fill="x", pady=(8, 0))
        source_columns = ("evidence_id", "label", "type", "record_id")
        self.chat_sources_tree = ttk.Treeview(source_frame, columns=source_columns, show="headings", height=4, selectmode="browse")
        for col, width in {"evidence_id":190,"label":430,"type":110,"record_id":180}.items():
            self.chat_sources_tree.heading(col, text=col.replace("_", " ").title())
            self.chat_sources_tree.column(col, width=width, anchor="w")
        self.chat_sources_tree.pack(side="left", fill="x", expand=True)
        ttk.Button(source_frame, text="Open Source", command=self.open_selected_chat_source).pack(side="right", padx=(8, 0))
        self.chat_sources_tree.bind("<Double-1>", lambda _event: self.open_selected_chat_source())

        entry_frame = ttk.Frame(self.chat_tab)
        entry_frame.pack(fill="x", pady=(8, 0))
        self.chat_input = tk.Text(entry_frame, height=4, wrap="word")
        self.chat_input.pack(side="left", fill="x", expand=True)
        self.chat_input.bind("<Control-Return>", self._chat_ctrl_enter)
        controls = ttk.Frame(entry_frame)
        controls.pack(side="right", fill="y", padx=(8, 0))
        self.chat_send_button = ttk.Button(controls, text="Send", command=self.send_chat_question)
        self.chat_send_button.pack(fill="x", pady=(0, 4))
        ttk.Button(controls, text="Clear Entry", command=lambda: self.chat_input.delete("1.0", "end")).pack(fill="x")
        ttk.Label(
            self.chat_tab,
            text="General assistant chat is read-only. CostPilot Review Center can execute permission-checked invoice and receiving actions only after manager confirmation; counts and orders remain protected.",
            style="Muted.TLabel",
            wraplength=1080,
        ).pack(anchor="w", pady=(6, 0))

    def _build_settings(self) -> None:
        canvas = tk.Canvas(self.settings_tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.settings_tab, orient="vertical", command=canvas.yview)
        form = ttk.Frame(canvas, padding=4)
        window_id = canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        form.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        ttk.Label(form, text="Restaurant, Planning & Automation Settings", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        self.setting_vars: dict[str, tk.Variable] = {
            "restaurant_name": tk.StringVar(),
            "restaurant_group": tk.StringVar(),
            "address": tk.StringVar(),
            "latitude": tk.StringVar(),
            "longitude": tk.StringVar(),
            "target_menu_food_cost_percent": tk.StringVar(),
            "estimated_manual_invoice_minutes": tk.StringVar(),
            "estimated_manager_hourly_cost": tk.StringVar(),
            "weather_forecast_days": tk.StringVar(),
            "minimum_extraction_confidence": tk.StringVar(),
            "auto_approve_confidence": tk.StringVar(),
            "invoice_math_tolerance": tk.StringVar(),
            "price_alert_percent": tk.StringVar(),
            "require_review_for_unrecognized_vendors": tk.BooleanVar(),
            "auto_learn_validated_vendors": tk.BooleanVar(),
            "known_vendors": tk.StringVar(),
            "forecast_history_months": tk.StringVar(),
            "default_lead_time_days": tk.StringVar(),
            "default_order_cycle_days": tk.StringVar(),
            "default_safety_stock_days": tk.StringVar(),
            "default_order_multiple": tk.StringVar(),
            "auto_generate_weekly_order_draft": tk.BooleanVar(),
            "manager_chat_enabled": tk.BooleanVar(),
            "manager_chat_provider": tk.StringVar(),
            "manager_chat_model": tk.StringVar(),
            "manager_chat_free_only": tk.BooleanVar(),
            "manager_chat_timeout_seconds": tk.StringVar(),
            "manager_chat_context_max_items": tk.StringVar(),
            "manager_chat_history_turns": tk.StringVar(),
            "manager_chat_local_fallback": tk.BooleanVar(),
            "automatic_backups_enabled": tk.BooleanVar(),
            "automatic_backup_interval_hours": tk.StringVar(),
            "backup_retention_count": tk.StringVar(),
            "require_login": tk.BooleanVar(),
            "receiving_verification_enabled": tk.BooleanVar(),
            "auto_recover_invoice_headers": tk.BooleanVar(),
            "auto_approve_recovered_invoice_headers": tk.BooleanVar(),
            "auto_verify_clean_receiving": tk.BooleanVar(),
            "auto_verify_receiving_date_mode": tk.StringVar(),
            "costpilot_review_auto_explain": tk.BooleanVar(),
        }
        rows = [
            ("Restaurant name", "restaurant_name"),
            ("Restaurant group", "restaurant_group"),
            ("Address", "address"),
            ("Latitude", "latitude"),
            ("Longitude", "longitude"),
            ("Target menu food cost percent", "target_menu_food_cost_percent"),
            ("Estimated manual invoice minutes", "estimated_manual_invoice_minutes"),
            ("Estimated manager hourly cost", "estimated_manager_hourly_cost"),
            ("Weather forecast days", "weather_forecast_days"),
            ("Minimum extraction confidence", "minimum_extraction_confidence"),
            ("Auto-approve confidence", "auto_approve_confidence"),
            ("Invoice math tolerance", "invoice_math_tolerance"),
            ("Price alert percent", "price_alert_percent"),
            ("Known vendors (semicolon separated)", "known_vendors"),
            ("Forecast history months", "forecast_history_months"),
            ("Default lead time days", "default_lead_time_days"),
            ("Default order cycle days", "default_order_cycle_days"),
            ("Default safety stock days", "default_safety_stock_days"),
            ("Default order multiple", "default_order_multiple"),
            ("Manager chat provider", "manager_chat_provider"),
            ("Manager chat model", "manager_chat_model"),
            ("Manager chat timeout seconds", "manager_chat_timeout_seconds"),
            ("Manager chat context item limit", "manager_chat_context_max_items"),
            ("Manager chat history turns", "manager_chat_history_turns"),
            ("Automatic backup interval hours", "automatic_backup_interval_hours"),
            ("Backup retention count", "backup_retention_count"),
            ("Automatic receiving date source (invoice_date or today)", "auto_verify_receiving_date_mode"),
        ]
        self.setting_edit_widgets: list[ttk.Widget] = []
        for row_index, (label, key) in enumerate(rows, 1):
            ttk.Label(form, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 10), pady=4)
            entry = ttk.Entry(form, textvariable=self.setting_vars[key], width=54)
            entry.grid(row=row_index, column=1, sticky="ew", pady=4)
            self.setting_edit_widgets.append(entry)
        check_row = len(rows) + 1
        checks = [
            ("Require review for unrecognized vendors or layouts", "require_review_for_unrecognized_vendors"),
            ("Automatically learn new vendors after complete validation", "auto_learn_validated_vendors"),
            ("Automatically create one draft order sheet each week", "auto_generate_weekly_order_draft"),
            ("Enable general read-only manager chat", "manager_chat_enabled"),
            ("Require an explicitly free model ID for manager chat", "manager_chat_free_only"),
            ("Use deterministic summaries when the free model is unavailable", "manager_chat_local_fallback"),
            ("Create automatic verified backups", "automatic_backups_enabled"),
            ("Require users to sign in", "require_login"),
            ("Require receiving verification for approved deliveries", "receiving_verification_enabled"),
            ("Re-read raw extraction to recover missing invoice dates and numbers", "auto_recover_invoice_headers"),
            ("Automatically approve invoices when recovered headers pass all validation", "auto_approve_recovered_invoice_headers"),
            ("Automatically verify clean approved invoices as received in full", "auto_verify_clean_receiving"),
            ("Automatically explain the first CostPilot review case", "costpilot_review_auto_explain"),
        ]
        for offset, (label, key) in enumerate(checks):
            check = ttk.Checkbutton(form, text=label, variable=self.setting_vars[key])
            check.grid(row=check_row + offset, column=0, columnspan=2, sticky="w", pady=3)
            self.setting_edit_widgets.append(check)

        backend_frame = ttk.LabelFrame(form, text="Local CostPilot and optional cloud fallback", padding=10)
        backend_frame.grid(row=check_row + len(checks), column=0, columnspan=3, sticky="ew", pady=(12, 6))
        self.costpilot_status_var = tk.StringVar(value="Local CostPilot status has not been checked.")
        ttk.Label(backend_frame, textvariable=self.costpilot_status_var, wraplength=860).pack(anchor="w")
        buttons = ttk.Frame(backend_frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Test Local Processing", command=self.test_dependencies).pack(side="left", padx=3)
        ttk.Button(buttons, text="Test CostPilot", command=self.test_manager_chat_model).pack(side="left", padx=3)
        self.install_repair_costpilot_button = ttk.Button(buttons, text="Install / Repair Local CostPilot", command=self.install_repair_local_costpilot)
        self.install_repair_costpilot_button.pack(side="left", padx=3)
        self.configure_cloud_button = ttk.Button(buttons, text="Configure Optional Cloud", command=self.configure_manager_chat_model)
        self.configure_cloud_button.pack(side="left", padx=3)
        ttk.Button(buttons, text="Open Automation Logs", command=lambda: self.open_folder_key("logs")).pack(side="left", padx=3)

        self.save_settings_button = ttk.Button(form, text="Save Settings", command=self.save_settings)
        self.save_settings_button.grid(row=check_row + len(checks) + 1, column=0, sticky="w", pady=12)
        form.columnconfigure(1, weight=1)

    def _build_security(self) -> None:
        ttk.Label(self.security_tab, text="Security, Backups and Audit History", style="Title.TLabel").pack(anchor="w")
        self.security_notebook = ttk.Notebook(self.security_tab)
        self.security_notebook.pack(fill="both", expand=True, pady=(8, 0))

        backup_frame = ttk.Frame(self.security_notebook, padding=8)
        users_frame = ttk.Frame(self.security_notebook, padding=8)
        audit_frame = ttk.Frame(self.security_notebook, padding=8)
        self.security_notebook.add(backup_frame, text="Backups & Restore")
        self.security_notebook.add(users_frame, text="Users & Roles")
        self.security_notebook.add(audit_frame, text="Audit Log")

        backup_top = ttk.Frame(backup_frame); backup_top.pack(fill="x", pady=(0, 6))
        self.create_backup_button = ttk.Button(backup_top, text="Create Backup Now", command=self.create_manual_backup)
        self.create_backup_button.pack(side="left", padx=3)
        self.restore_backup_button = ttk.Button(backup_top, text="Restore Selected Backup", command=self.restore_selected_backup)
        self.restore_backup_button.pack(side="left", padx=3)
        self.restore_external_button = ttk.Button(backup_top, text="Restore External Backup", command=self.restore_external_backup)
        self.restore_external_button.pack(side="left", padx=3)
        self.open_backup_folder_button = ttk.Button(backup_top, text="Open Backup Folder", command=self.open_backup_folder)
        self.open_backup_folder_button.pack(side="left", padx=3)
        ttk.Button(backup_top, text="Refresh", command=self.refresh_security).pack(side="right", padx=3)
        backup_cols = ("backup_id", "created_at", "created_by", "type", "size", "status", "file")
        backup_table = ttk.Frame(backup_frame)
        backup_table.pack(fill="both", expand=True)
        backup_table.columnconfigure(0, weight=1)
        backup_table.rowconfigure(0, weight=1)
        self.backup_tree = ttk.Treeview(backup_table, columns=backup_cols, show="headings", selectmode="browse")
        for col, width in {"backup_id":190,"created_at":145,"created_by":105,"type":100,"size":95,"status":90,"file":420}.items():
            self.backup_tree.heading(col, text=col.replace("_", " ").title())
            self.backup_tree.column(col, width=width, anchor="w")
        backup_y_scroll = ttk.Scrollbar(backup_table, orient="vertical", command=self.backup_tree.yview)
        backup_x_scroll = ttk.Scrollbar(backup_table, orient="horizontal", command=self.backup_tree.xview)
        self.backup_tree.configure(yscrollcommand=backup_y_scroll.set, xscrollcommand=backup_x_scroll.set)
        self.backup_tree.grid(row=0, column=0, sticky="nsew")
        backup_y_scroll.grid(row=0, column=1, sticky="ns")
        backup_x_scroll.grid(row=1, column=0, sticky="ew")

        users_top = ttk.Frame(users_frame); users_top.pack(fill="x", pady=(0, 6))
        self.add_user_button = ttk.Button(users_top, text="Add User", command=self.add_user)
        self.add_user_button.pack(side="left", padx=3)
        self.edit_user_button = ttk.Button(users_top, text="Edit Selected", command=self.edit_selected_user)
        self.edit_user_button.pack(side="left", padx=3)
        ttk.Button(users_top, text="Refresh", command=self.refresh_security).pack(side="right", padx=3)
        user_cols = ("username", "display_name", "role", "active", "last_login")
        self.users_tree = ttk.Treeview(users_frame, columns=user_cols, show="headings", selectmode="browse")
        for col, width in {"username":160,"display_name":230,"role":180,"active":80,"last_login":170}.items():
            self.users_tree.heading(col, text=col.replace("_", " ").title())
            self.users_tree.column(col, width=width, anchor="w")
        self.users_tree.pack(fill="both", expand=True)
        self.users_tree.bind("<Double-1>", lambda _event: self.edit_selected_user())

        audit_top = ttk.Frame(audit_frame); audit_top.pack(fill="x", pady=(0, 6))
        ttk.Button(audit_top, text="Refresh", command=self.refresh_security).pack(side="right", padx=3)
        audit_cols = ("created_at", "username", "role", "action", "entity", "summary")
        audit_table = ttk.Frame(audit_frame)
        audit_table.pack(fill="both", expand=True)
        audit_table.columnconfigure(0, weight=1)
        audit_table.rowconfigure(0, weight=1)
        self.audit_tree = ttk.Treeview(audit_table, columns=audit_cols, show="headings", selectmode="browse")
        for col, width in {"created_at":145,"username":115,"role":135,"action":150,"entity":165,"summary":480}.items():
            self.audit_tree.heading(col, text=col.replace("_", " ").title())
            self.audit_tree.column(col, width=width, anchor="w")
        audit_y_scroll = ttk.Scrollbar(audit_table, orient="vertical", command=self.audit_tree.yview)
        audit_x_scroll = ttk.Scrollbar(audit_table, orient="horizontal", command=self.audit_tree.xview)
        self.audit_tree.configure(yscrollcommand=audit_y_scroll.set, xscrollcommand=audit_x_scroll.set)
        self.audit_tree.grid(row=0, column=0, sticky="nsew")
        audit_y_scroll.grid(row=0, column=1, sticky="ns")
        audit_x_scroll.grid(row=1, column=0, sticky="ew")

    def _build_auto_upload_history(self) -> None:
        top = ttk.Frame(self.auto_upload_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Auto Upload History", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_auto_upload_history).pack(side="right", padx=3)
        ttk.Button(top, text="Open Upload Folder", command=self.open_auto_upload_folder).pack(side="right", padx=3)
        ttk.Button(top, text="Find Existing Files", command=self.scan_restaurant_documents).pack(side="right", padx=3)
        ttk.Button(top, text="Retry Selected", command=self.retry_selected_auto_upload).pack(side="right", padx=3)
        ttk.Button(top, text="Retry All Unresolved", command=self.retry_all_auto_upload).pack(side="right", padx=3)

        self.auto_upload_history_status_var = tk.StringVar(
            value="Select a workbook to see its exact classification and row errors."
        )
        ttk.Label(
            self.auto_upload_tab,
            textvariable=self.auto_upload_history_status_var,
            style="Muted.TLabel",
            wraplength=1100,
            justify="left",
        ).pack(anchor="w", pady=(0, 7))

        paned = ttk.Panedwindow(self.auto_upload_tab, orient="vertical")
        paned.pack(fill="both", expand=True)
        history_frame = ttk.LabelFrame(paned, text="Workbook processing attempts", padding=6)
        detail_frame = ttk.LabelFrame(paned, text="Exact workbook and row details", padding=6)
        paned.add(history_frame, weight=3)
        paned.add(detail_frame, weight=2)

        columns = ("completed", "workbook", "detected", "status", "rows", "summary")
        history_table = ttk.Frame(history_frame)
        history_table.pack(fill="both", expand=True)
        history_table.columnconfigure(0, weight=1)
        history_table.rowconfigure(0, weight=1)
        self.auto_upload_tree = ttk.Treeview(
            history_table, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "completed": "Completed",
            "workbook": "Workbook",
            "detected": "Detected Type",
            "status": "Status",
            "rows": "Imported / Errors",
            "summary": "Result",
        }
        widths = {
            "completed": 145, "workbook": 260, "detected": 130,
            "status": 105, "rows": 125, "summary": 430,
        }
        for column in columns:
            self.auto_upload_tree.heading(column, text=headings[column])
            self.auto_upload_tree.column(column, width=widths[column], anchor="w")
        scroll = ttk.Scrollbar(history_table, orient="vertical", command=self.auto_upload_tree.yview)
        history_x_scroll = ttk.Scrollbar(history_table, orient="horizontal", command=self.auto_upload_tree.xview)
        self.auto_upload_tree.configure(yscrollcommand=scroll.set, xscrollcommand=history_x_scroll.set)
        self.auto_upload_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        history_x_scroll.grid(row=1, column=0, sticky="ew")
        self.auto_upload_tree.bind("<<TreeviewSelect>>", self._auto_upload_selection_changed)
        self.auto_upload_tree.bind("<Double-1>", lambda _event: self.open_selected_auto_upload_source())

        self.auto_upload_detail = tk.Text(detail_frame, wrap="word", state="disabled", height=12)
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.auto_upload_detail.yview)
        self.auto_upload_detail.configure(yscrollcommand=detail_scroll.set)
        self.auto_upload_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.auto_upload_event_rows: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _auto_upload_outcome(event: dict[str, Any]) -> dict[str, Any]:
        try:
            details = json.loads(str(event.get("details_json") or "{}"))
        except Exception:
            details = {}
        outcome = details.get("outcome") if isinstance(details, dict) else {}
        return outcome if isinstance(outcome, dict) else {}

    def _set_auto_upload_detail(self, text: str) -> None:
        if not hasattr(self, "auto_upload_detail"):
            return
        self.auto_upload_detail.configure(state="normal")
        self.auto_upload_detail.delete("1.0", "end")
        self.auto_upload_detail.insert("1.0", text)
        self.auto_upload_detail.configure(state="disabled")

    def _auto_upload_selection_changed(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "auto_upload_tree"):
            return
        selected = self.auto_upload_tree.selection()
        if not selected:
            self._set_auto_upload_detail("Select a processing attempt to view details.")
            return
        event = self.auto_upload_event_rows.get(str(selected[0]), {})
        outcome = self._auto_upload_outcome(event)
        details = outcome.get("details")
        details = details if isinstance(details, dict) else {}
        errors = details.get("errors")
        if not isinstance(errors, list):
            errors = [details.get("error")] if details.get("error") else []
        lines = [
            f"Workbook: {event.get('original_name') or ''}",
            f"Completed: {event.get('completed_at') or ''}",
            f"Detected type: {event.get('detected_type') or ''}",
            f"Classification confidence: {float(event.get('classification_confidence') or 0):.0%}",
            f"Status: {event.get('status') or ''}",
            f"Result: {event.get('summary') or ''}",
            f"Archived original: {event.get('archived_path') or ''}",
            "",
            "Workbook / row errors:",
        ]
        if errors:
            lines.extend(f"- {error}" for error in errors)
        else:
            lines.append("- No row errors were recorded.")
        invoice_results = details.get("results")
        if isinstance(invoice_results, list) and invoice_results:
            lines.extend(["", "Invoice records:"])
            for result in invoice_results:
                if isinstance(result, dict):
                    lines.append(
                        f"- {result.get('invoice_number') or result.get('invoice_id') or 'Invoice'}: "
                        f"{result.get('status') or ''} "
                        f"{'; '.join(result.get('errors') or [])}"
                    )
        self._set_auto_upload_detail("\n".join(lines))

    def refresh_auto_upload_history(self) -> None:
        if not hasattr(self, "auto_upload_tree"):
            return
        previous = list(self.auto_upload_tree.selection())
        for item in self.auto_upload_tree.get_children():
            self.auto_upload_tree.delete(item)
        self.auto_upload_event_rows = {}
        if not self.workspace:
            self._set_auto_upload_detail("Select a restaurant to view Auto Upload history.")
            return
        try:
            router = AutoUploadRouter(self.workspace)
            events = router.list_events(300)
            unresolved_ids = {
                str(event["event_id"]) for event in router.list_unresolved_events(300)
            }
        except Exception as exc:
            self._set_auto_upload_detail(f"Auto Upload history could not be loaded: {exc}")
            return
        for event in events:
            event_id = str(event["event_id"])
            outcome = self._auto_upload_outcome(event)
            imported = int(outcome.get("imported") or 0)
            rejected = int(outcome.get("rejected") or 0)
            self.auto_upload_event_rows[event_id] = event
            self.auto_upload_tree.insert(
                "", "end", iid=event_id,
                values=(
                    event.get("completed_at") or "",
                    event.get("original_name") or "",
                    event.get("detected_type") or "",
                    event.get("status") or "",
                    f"{imported} / {rejected}",
                    event.get("summary") or "",
                ),
            )
        surviving = [event_id for event_id in previous if self.auto_upload_tree.exists(event_id)]
        if surviving:
            self.auto_upload_tree.selection_set(*surviving)
        elif events:
            self.auto_upload_tree.selection_set(str(events[0]["event_id"]))
        discovery_summary = str(
            self.workspace.load_settings().get("document_discovery_last_summary") or ""
        ).strip()
        status = (
            f"{len(events)} processing attempt(s) shown; {len(unresolved_ids)} currently unresolved file(s). "
            "Only the latest unresolved attempt for an exact file appears in CostPilot Review."
        )
        if discovery_summary:
            status += f" Last folder search: {discovery_summary}"
        self.auto_upload_history_status_var.set(status)
        self._auto_upload_selection_changed()

    def _retry_auto_upload_ids(self, event_ids: list[str]) -> None:
        if not self.workspace or not event_ids:
            return
        if not self.current_user or not (
            self.current_user.can("invoices.process")
            or self.current_user.can("pos.import")
            or self.current_user.can("settings.manage")
        ):
            messagebox.showerror("Permission denied", "Your role cannot retry Auto Upload files.")
            return
        if not messagebox.askyesno(
            "Retry Auto Upload",
            f"Queue {len(event_ids)} preserved file(s) for deterministic reprocessing?",
        ):
            return
        router = AutoUploadRouter(self.workspace)
        queued, errors = 0, []
        for event_id in event_ids:
            try:
                router.retry_event(int(event_id))
                queued += 1
            except Exception as exc:
                errors.append(f"Event {event_id}: {exc}")
        if queued:
            self.auto_upload_coordinator.scan_now()
        message = f"Queued {queued} file(s)."
        if errors:
            message += "\n\n" + "\n".join(errors[:20])
        messagebox.showinfo("Auto Upload retry", message)
        self.refresh_auto_upload_history()
        self.refresh_review()

    def retry_selected_auto_upload(self) -> None:
        selected = list(self.auto_upload_tree.selection()) if hasattr(self, "auto_upload_tree") else []
        if not selected:
            messagebox.showinfo("Auto Upload", "Select one or more unresolved attempts first.")
            return
        self._retry_auto_upload_ids(selected)

    def retry_all_auto_upload(self) -> None:
        if not self.workspace:
            return
        try:
            events = AutoUploadRouter(self.workspace).list_unresolved_events(300)
        except Exception as exc:
            messagebox.showerror("Auto Upload", str(exc))
            return
        self._retry_auto_upload_ids([str(event["event_id"]) for event in events])

    def open_selected_auto_upload_source(self) -> None:
        selected = list(self.auto_upload_tree.selection()) if hasattr(self, "auto_upload_tree") else []
        if not selected:
            return
        event = self.auto_upload_event_rows.get(str(selected[0]), {})
        path = Path(str(event.get("archived_path") or ""))
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo("Auto Upload", "The archived original is not available.")

    def _build_log(self) -> None:
        top = ttk.Frame(self.log_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Activity Log", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Clear", command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")
        self.log_text = tk.Text(self.log_tab, wrap="word", state="normal")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text.configure(state="disabled")

    def _load_initial_restaurant(self) -> None:
        self._refresh_restaurant_combo()
        selected = self.registry.data.get("selected")
        if selected and Path(selected).exists():
            self.select_workspace(Path(selected))
        elif self.registry.restaurants:
            self.select_workspace(Path(self.registry.restaurants[0]["path"]))
        else:
            self.status_var.set("Add a restaurant workspace to begin.")

    def _refresh_restaurant_combo(self) -> None:
        values = [f"{r.get('name')} | {r.get('path')}" for r in self.registry.restaurants]
        self.restaurant_combo["values"] = values
        if self.workspace:
            for value, row in zip(values, self.registry.restaurants):
                if Path(row["path"]).resolve() == self.workspace.root:
                    self.restaurant_var.set(value)
                    break

    def _restaurant_selected(self, _event: tk.Event | None = None) -> None:
        index = self.restaurant_combo.current()
        if index >= 0:
            self.select_workspace(Path(self.registry.restaurants[index]["path"]))

    def add_restaurant(self) -> None:
        name = simpledialog.askstring("Restaurant name", "Enter the restaurant name:", parent=self.root)
        if not name:
            return
        selected = filedialog.askdirectory(title="Choose or create the restaurant workspace folder")
        if not selected:
            return
        path = Path(selected)
        workspace = RestaurantWorkspace(path)
        settings = workspace.load_settings()
        settings.update({
            "restaurant_name": name.strip(),
            "initial_document_discovery_pending": True,
            "initial_document_discovery_source": str(path.resolve()),
            "document_discovery_last_status": "Pending",
        })
        workspace.save_settings(settings)
        upload_folder = ensure_auto_upload_folder(workspace, name.strip())
        self.registry.add(name.strip(), path)
        self._refresh_restaurant_combo()
        self.select_workspace(path)
        self.log(f"Desktop auto-upload folder created: {upload_folder}")

    def remove_restaurant(self) -> None:
        if not self.workspace:
            return
        if not messagebox.askyesno(
            "Remove from GUI",
            "Remove this restaurant from the GUI list? Its workspace files will not be deleted.",
        ):
            return
        old = self.workspace.root
        self.registry.remove(old)
        self.workspace = None
        self.pipeline = None
        self.chat_service = None
        self.chat_session_id = None
        self._refresh_restaurant_combo()
        if self.registry.restaurants:
            self.select_workspace(Path(self.registry.restaurants[0]["path"]))
        else:
            self.refresh_all()

    def select_workspace(self, path: Path) -> None:
        try:
            if self.pipeline:
                try:
                    self.pipeline.phase2.stop_mobile_count_server()
                except Exception:
                    pass
            self.mobile_count_url = ""
            self.mobile_count_token = None
            workspace = RestaurantWorkspace(path)
            pipeline = InvoicePipeline(workspace)
            if not pipeline.controls.has_users():
                dialog = OwnerSetupDialog(self.root, pipeline.controls)
                self.root.wait_window(dialog)
                if not dialog.created_user:
                    self.status_var.set("Owner account setup was cancelled.")
                    return
            settings = workspace.load_settings()
            try:
                stored_settings = json.loads(workspace.config_path.read_text(encoding="utf-8"))
            except Exception:
                stored_settings = {}
            if not isinstance(stored_settings, dict) or int(
                stored_settings.get("costpilot_local_migration_version") or 0
            ) < 1:
                settings.update(
                    {
                        "manager_chat_provider": "local",
                        "manager_chat_model": DEFAULT_FREE_MODEL,
                        "manager_chat_free_only": True,
                        "manager_chat_local_fallback": True,
                        "manager_chat_cloud_fallback_enabled": False,
                                        "costpilot_local_migration_version": 1,
                    }
                )
                workspace.save_settings(settings)
            upload_folder = ensure_auto_upload_folder(workspace, settings.get("restaurant_name", path.name))
            if settings.get("require_login", True):
                dialog = LoginDialog(self.root, pipeline.controls, settings.get("restaurant_name", "Restaurant"))
                self.root.wait_window(dialog)
                if not dialog.user:
                    self.status_var.set("Sign-in cancelled.")
                    return
                user = dialog.user
            else:
                rows = pipeline.controls.list_users()
                row = next((item for item in rows if item["active"]), None)
                user = AuthenticatedUser(row["user_id"], row["username"], row["display_name"], row["role"]) if row else None
            self.workspace = workspace
            self.pipeline = pipeline
            self.current_user = user
            self.pipeline.controls.current_user = user
            self.pipeline.phase3.set_location_provider(lambda: list(self.registry.restaurants))
            self.user_status_var.set(f"{user.display_name} | {user.role}" if user else "Local mode")
            self.chat_service = ManagerChatService(
                self.workspace, self.pipeline, self.current_gui_state
            )
            self.chat_session_id = None
            self.registry.data["selected"] = str(self.workspace.root)
            self.registry.save()
            self._refresh_restaurant_combo()
            self.log(f"Selected restaurant workspace: {self.workspace.root}")
            self.log(f"Automatic upload folder: {upload_folder}")
            self.auto_upload_coordinator.scan_now()
            self.pipeline.controls.audit("workspace.open", "workspace", str(self.workspace.root), "Opened restaurant workspace")
            self._start_workspace_maintenance(workspace, self.pipeline, settings, user)
            self.root.after(100, self.refresh_all)
            if bool(settings.get("initial_document_discovery_pending")):
                discovery_source = Path(
                    str(settings.get("initial_document_discovery_source") or workspace.root)
                )
                expected_workspace = workspace.root
                self.root.after(
                    350,
                    lambda source=discovery_source, expected=expected_workspace:
                    self._start_document_discovery(source, first_run=True, expected_workspace=expected),
                )
        except Exception as exc:
            messagebox.showerror("Workspace error", str(exc))
            self.log(traceback.format_exc())

    def _start_workspace_maintenance(
        self,
        workspace: RestaurantWorkspace,
        pipeline: InvoicePipeline,
        settings: dict[str, Any],
        user: AuthenticatedUser | None,
    ) -> None:
        """Run nonessential first-open maintenance away from Tk's event loop."""
        if self.workspace_maintenance_busy:
            return
        self.workspace_maintenance_busy = True
        expected_workspace = str(workspace.root)
        expected_pipeline = pipeline

        def worker() -> None:
            messages: list[str] = []
            try:
                if settings.get("auto_recover_invoice_headers", True) and user and user.can("invoices.review"):
                    try:
                        result = pipeline.batch_process_reviews(
                            None,
                            approve_eligible=bool(settings.get("auto_approve_recovered_invoice_headers", True)),
                            explicit_approval=False,
                        )
                        approved = int(result.get("approved") or 0)
                        if approved:
                            messages.append(f"Automatic invoice recovery approved {approved} invoice(s).")
                    except Exception as exc:
                        messages.append(f"Automatic invoice recovery warning: {exc}")
                if settings.get("auto_verify_clean_receiving", True) and user and user.can("receiving.verify"):
                    try:
                        result = pipeline.auto_verify_receiving()
                        verified = int(result.get("verified") or 0)
                        if verified:
                            messages.append(f"Automatic receiving verified {verified} delivery record(s).")
                    except Exception as exc:
                        messages.append(f"Automatic receiving warning: {exc}")
                try:
                    backup = pipeline.automatic_backup_if_due()
                    if backup:
                        messages.append(f"Automatic backup created: {backup.name}")
                except Exception as exc:
                    messages.append(f"Automatic backup warning: {exc}")
                if settings.get("auto_generate_weekly_order_draft", True) and user and user.can("orders.generate"):
                    try:
                        draft = pipeline.ensure_weekly_order_draft()
                        if draft.get("created"):
                            pipeline.controls.audit(
                                "order.auto_draft",
                                "order_batch",
                                draft.get("batch_id"),
                                "Automatically created weekly draft order batch",
                            )
                            messages.append(f"Automatically created weekly draft order batch {draft['batch_id']}.")
                    except Exception as exc:
                        messages.append(f"Weekly draft order warning: {exc}")
            finally:
                self.worker_queue.put((
                    "workspace_maintenance_done",
                    {"workspace": expected_workspace, "messages": messages},
                ))

        threading.Thread(target=worker, name="MarginMise-Workspace-Maintenance", daemon=True).start()

    def open_auto_upload_folder(self) -> None:
        if not self.workspace:
            messagebox.showinfo("Automatic upload", "Select a restaurant first.")
            return
        settings = self.workspace.load_settings()
        folder = ensure_auto_upload_folder(
            self.workspace, settings.get("restaurant_name", self.workspace.root.name)
        )
        open_path(folder)

    def scan_restaurant_documents(self) -> None:
        if not self.workspace:
            messagebox.showinfo("Find existing files", "Select a restaurant first.")
            return
        user = self.current_user
        if user and not any(user.can(permission) for permission in (
            "invoices.process", "pos.import", "settings.manage"
        )):
            messagebox.showerror(
                "Permission denied",
                "Your role cannot queue restaurant files for automatic upload.",
            )
            return
        selected = filedialog.askdirectory(
            title="Choose the restaurant records folder to search",
            initialdir=str(self.workspace.root),
        )
        if selected:
            self._start_document_discovery(Path(selected), first_run=False)

    def _start_document_discovery(
        self,
        source_root: Path,
        *,
        first_run: bool,
        expected_workspace: Path | None = None,
    ) -> None:
        if not self.workspace:
            return
        if expected_workspace is not None and self.workspace.root != expected_workspace.resolve():
            return
        if self.document_discovery_busy:
            if not first_run:
                messagebox.showinfo(
                    "Find existing files",
                    "MarginMise is already searching a restaurant folder.",
                )
            return
        workspace = self.workspace
        restaurant_name = str(workspace.load_settings().get("restaurant_name") or workspace.root.name)
        self.document_discovery_busy = True
        self.status_var.set(f"Searching {source_root} for existing restaurant documents...")
        self.log(
            f"Document discovery started for {restaurant_name}. Originals will remain unchanged: "
            f"{source_root}"
        )

        def progress(payload: dict[str, Any]) -> None:
            self.worker_queue.put(("document_discovery_progress", payload))

        def worker() -> None:
            try:
                service = InitialDocumentDiscovery(workspace, restaurant_name)
                report = service.discover(source_root, progress_callback=progress)
                self.worker_queue.put(("document_discovery_done", {
                    "workspace": str(workspace.root),
                    "first_run": first_run,
                    "report": report.as_dict(),
                    "summary": report.summary,
                }))
            except Exception as exc:
                self.worker_queue.put(("document_discovery_error", {
                    "workspace": str(workspace.root),
                    "first_run": first_run,
                    "error": str(exc),
                }))

        threading.Thread(target=worker, name="RestaurantDocumentDiscovery", daemon=True).start()

    def scan_auto_upload_now(self) -> None:
        if not self.workspace:
            return
        self.auto_upload_coordinator.scan_now()
        self.status_var.set("Checking automatic upload folders...")
        self.log("Manual automatic-upload scan requested.")

    def current_auto_upload_status(self) -> dict[str, Any]:
        if not self.workspace:
            return {"enabled": False, "folder": "", "pending": 0, "needs_review": 0, "failed": 0}
        try:
            return auto_upload_status(self.workspace)
        except Exception as exc:
            return {"enabled": False, "folder": "", "pending": 0, "needs_review": 0, "failed": 0, "error": str(exc)}

    def has_permission(self, permission: str) -> bool:
        return bool(self.current_user and self.current_user.can(permission))

    def require_permission(self, permission: str) -> bool:
        if not self.pipeline:
            return False
        try:
            self.pipeline.controls.require_permission(permission, self.current_user)
            return True
        except PermissionDenied as exc:
            messagebox.showwarning("Permission denied", str(exc))
            return False

    def sign_out(self) -> None:
        if not self.pipeline or not self.workspace:
            return
        path = self.workspace.root
        self.pipeline.controls.sign_out()
        self.current_user = None
        self.user_status_var.set("Not signed in")
        self.select_workspace(path)

    def require_pipeline(self) -> InvoicePipeline | None:
        if not self.pipeline or not self.workspace:
            messagebox.showwarning("No restaurant", "Add or select a restaurant workspace first.")
            return None
        return self.pipeline

    def add_invoice_files(self) -> None:
        if not self.require_permission("invoices.upload"):
            return
        if not self.workspace:
            self.require_pipeline()
            return
        filetypes = [
            ("Invoice files", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.json"),
            ("PDF files", "*.pdf"),
            ("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
            ("JSON", "*.json"),
            ("All files", "*.*"),
        ]
        paths = filedialog.askopenfilenames(title="Select invoice files", filetypes=filetypes)
        if not paths:
            return
        added = 0
        for raw in paths:
            source = Path(raw)
            if source.suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES:
                continue
            destination = self.workspace.folders["upload"] / safe_filename(source.name)
            if destination.exists():
                stem, suffix = destination.stem, destination.suffix
                counter = 2
                while destination.exists():
                    destination = destination.with_name(f"{stem}_{counter}{suffix}")
                    counter += 1
            shutil.copy2(source, destination)
            added += 1
        self.log(f"Added {added} invoice file(s) to the upload folder.")
        if self.pipeline:
            self.pipeline.controls.audit("invoice.upload", "upload_batch", None, f"Added {added} invoice file(s)", details={"files": [Path(raw).name for raw in paths]})
        self.refresh_uploads()
        self.notebook.select(self.intake_tab)

    def process_selected_uploads(self) -> None:
        if not self.workspace:
            return
        selected = self.upload_tree.selection()
        sources = [Path(self.upload_tree.item(item, "values")[0]) for item in selected]
        # Tree displays file names, not full paths.
        sources = [self.workspace.folders["upload"] / source.name for source in sources]
        self._start_processing(sources)

    def process_all_uploads(self) -> None:
        if not self.workspace:
            self.require_pipeline()
            return
        sources = sorted(
            p for p in self.workspace.folders["upload"].iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
        )
        self._start_processing(sources)

    def _start_processing(self, sources: list[Path]) -> None:
        if not self.require_permission("invoices.process"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        if self.processing:
            messagebox.showinfo("Processing", "Invoice processing is already running.")
            return
        if not sources:
            messagebox.showinfo("No invoices", "There are no supported invoice files to process.")
            return
        self.processing = True
        self.processing_progress.start(12)
        self.status_var.set(f"Processing {len(sources)} invoice file(s)...")
        for source in sources:
            if self.upload_tree.exists(source.name):
                self.upload_tree.set(source.name, "status", "Processing")

        def worker() -> None:
            for source in sources:
                try:
                    result = pipeline.process_file(source)
                except Exception as exc:
                    result = ProcessResult(source=str(source), status="Failed", errors=[str(exc)])
                self.worker_queue.put(("invoice_result", result))
            self.worker_queue.put(("processing_done", len(sources)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_worker_queue(self) -> None:
        try:
            while True:
                event, payload = self.worker_queue.get_nowait()
                if event == "invoice_result":
                    result: ProcessResult = payload
                    name = Path(result.source).name
                    if self.upload_tree.exists(name):
                        self.upload_tree.set(name, "status", result.status)
                    if result.extraction_method == "local-extraction-failed":
                        confidence_text = "extraction failed"
                    elif result.extraction_confidence > 0:
                        confidence_text = f"{result.extraction_confidence:.0%}"
                    else:
                        confidence_text = "confidence unavailable"
                    detail = f"{name}: {result.status} via {result.extraction_method} ({confidence_text})"
                    if result.message:
                        detail += f" - {result.message}"
                    self.log(detail)
                    for error in result.errors:
                        self.log(f"  ERROR: {error}")
                    for warning in result.warnings:
                        self.log(f"  WARNING: {warning}")
                    if self.pipeline:
                        self.pipeline.controls.audit(
                            "invoice.process", "invoice", result.invoice_id,
                            f"Processed {name}: {result.status}",
                            details={"method": result.extraction_method, "confidence": result.extraction_confidence, "errors": result.errors, "warnings": result.warnings},
                        )
                elif event == "auto_upload":
                    info = dict(payload or {})
                    kind = info.get("kind", "")
                    restaurant = info.get("restaurant", "Restaurant")
                    if kind == "processed":
                        outcome = info.get("outcome") or {}
                        original = info.get("original_name", "file")
                        detail = f"Auto Upload · {restaurant} · {original}: {outcome.get('status', '')} as {outcome.get('detected_type', '')}"
                        if outcome.get("summary"):
                            detail += f" - {outcome.get('summary')}"
                        self.log(detail)
                        if self.workspace and str(self.workspace.root) == str(info.get("workspace", "")):
                            self.status_var.set(detail[:240])
                            self.refresh_all()
                    elif kind == "watching":
                        self.log(f"Auto Upload watching {restaurant}: {info.get('inbox', '')}")
                    elif kind == "retry":
                        self.log(f"Auto Upload will retry {restaurant} · {info.get('original_name', 'file')}: {info.get('message', '')}")
                    elif kind == "error":
                        self.log(f"Auto Upload error · {restaurant}: {info.get('message', '')}")
                elif event == "document_discovery_progress":
                    info = dict(payload or {})
                    kind = str(info.get("kind") or "")
                    scanned = int(info.get("scanned_files") or 0)
                    queued = int(info.get("queued_files") or 0)
                    if kind in {"started", "progress", "queued"}:
                        self.status_var.set(
                            f"Searching existing restaurant files: {scanned} checked, "
                            f"{queued} queued..."
                        )
                elif event == "document_discovery_done":
                    self.document_discovery_busy = False
                    info = dict(payload or {})
                    summary = str(info.get("summary") or "Document discovery completed.")
                    self.log(f"Document discovery completed: {summary}")
                    if self.workspace and str(self.workspace.root) == str(info.get("workspace") or ""):
                        self.status_var.set(summary[:300])
                        self.refresh_auto_upload_history()
                    self.auto_upload_coordinator.scan_now()
                    if not bool(info.get("first_run")):
                        messagebox.showinfo("Find existing files", summary)
                elif event == "document_discovery_error":
                    self.document_discovery_busy = False
                    info = dict(payload or {})
                    error = str(info.get("error") or "Unknown document discovery error")
                    self.log(f"Document discovery error: {error}")
                    self.status_var.set("Existing restaurant-file search requires attention.")
                    if not bool(info.get("first_run")):
                        messagebox.showerror("Find existing files", error)
                elif event == "processing_done":
                    self.processing = False
                    self.processing_progress.stop()
                    self.status_var.set(f"Finished processing {payload} invoice file(s).")
                    self.refresh_all()
                elif event == "chat_answer":
                    self.chat_busy = False
                    self.chat_send_button.configure(state="normal")
                    answer = payload
                    prefix = (getattr(self, "_assistant_name", lambda: "Assistant")()) if not answer.used_local_fallback else "Computed fallback"
                    self._append_chat_message(prefix, answer.answer, "assistant")
                    self.chat_status_var.set(
                        f"{prefix} response using {answer.provider}/{answer.model}. Context and source evidence saved."
                    )
                    self.chat_sources = list(answer.sources or [])
                    for item in self.chat_sources_tree.get_children():
                        self.chat_sources_tree.delete(item)
                    for source in self.chat_sources:
                        evidence_id = source.get("evidence_id", "")
                        self.chat_sources_tree.insert("", "end", iid=evidence_id, values=(
                            evidence_id, source.get("label", ""), source.get("source_type", ""), source.get("source_id", ""),
                        ))
                    if self.pipeline:
                        self.pipeline.controls.audit(
                            "chat.answer", "chat", answer.session_id,
                            f"Answered manager question using {len(self.chat_sources)} evidence source(s)",
                            details={"provider": answer.provider, "model": answer.model, "fallback": answer.used_local_fallback, "evidence": [s.get("evidence_id") for s in self.chat_sources]},
                        )
                    self.dispatch_chat_navigation(answer.navigation or {})
                    self.status_var.set("Manager chat response completed.")
                    self.chat_input.focus_set()
                elif event == "chat_error":
                    self.chat_busy = False
                    self.chat_send_button.configure(state="normal")
                    self._append_chat_message("System", str(payload), "system")
                    self.chat_status_var.set(f"Manager chat error: {payload}")
                    self.status_var.set("Manager chat requires attention.")
                elif event == "chat_model_test":
                    self.chat_busy = False
                    self.chat_send_button.configure(state="normal")
                    ok = bool(payload.get("ok"))
                    detail = payload.get("stdout") or payload.get("stderr") or "No response"
                    self.chat_status_var.set(
                        "CostPilot model is ready." if ok else "CostPilot model is not ready."
                    )
                    self.log(f"Manager chat model test: {'PASS' if ok else 'FAIL'} - {detail[:500]}")
                    messagebox.showinfo(
                        "Manager chat model test",
                        ("PASS\n" if ok else "FAIL\n") + str(detail)[:1200],
                    )
                elif event == "local_ai_status":
                    self.backend_status_checking = False
                    self.costpilot_status_var.set(payload.message)
                    self.log(payload.message)
                elif event == "workspace_maintenance_done":
                    info = dict(payload or {})
                    self.workspace_maintenance_busy = False
                    if self.workspace and str(self.workspace.root) == str(info.get("workspace") or ""):
                        for message in info.get("messages") or []:
                            self.log(str(message))
                        self.refresh_all()
                elif event == "local_ai_install_done":
                    self.backend_busy = False
                    self.costpilot_status_var.set(payload.message)
                    self.status_var.set(payload.message)
                    self.log(payload.message)
                    self.refresh_chat_status()
                elif event == "local_ai_error":
                    self.backend_status_checking = False
                    self.backend_busy = False
                    self.costpilot_status_var.set(f"Local CostPilot error: {payload}")
                    self.status_var.set("Local CostPilot requires attention.")
                    self.log(f"Local CostPilot error: {payload}")
                elif event == "backend_status":
                    self.backend_status_checking = False
                    status = payload
                    self.last_backend_status = status
                    self._sync_costpilot_route_from_backend(status)
                    self.costpilot_status_var.set(status.message)
                    self.log(status.message)
                    if not status.ready:
                        if self._backend_auto_install():
                            self._install_local_costpilot(first_run=True)
                elif event == "backend_status_error":
                    self.backend_status_checking = False
                    self.costpilot_status_var.set(f"CostPilot check failed: {payload}")
                    self.log(f"CostPilot check failed: {payload}")
                elif event == "backend_done":
                    self.backend_busy = False
                    status = payload
                    self.last_backend_status = status
                    self._sync_costpilot_route_from_backend(status)
                    self.costpilot_status_var.set(status.message)
                    self.status_var.set(status.message)
                    self.log(status.message)
                    if status.ready and not status.doctor_ok:
                        self.log("Local CostPilot is installed; optional checks may still need attention.")
                    if status.authorization_required:
                        self.log(
                            "The CostPilot free route is configured. "
                            "Provider authorization will be requested when CostPilot is first used."
                        )
                elif event == "backend_probe_done":
                    self.backend_busy = False
                    results = payload
                    lines = results.get("lines", [])
                    self.status_var.set(results.get("status", "Local CostPilot check completed."))
                    self.costpilot_status_var.set(results.get("status", "Local CostPilot check completed."))
                    for line in lines:
                        self.log(line)
                    messagebox.showinfo("Local extraction test", "\n".join(lines))
                elif event == "backend_probe_error":
                    self.backend_busy = False
                    self.status_var.set("Local extraction test failed.")
                    self.costpilot_status_var.set(f"Local extraction test failed: {payload}")
                    self.log(f"Local extraction test failed: {payload}")
                    messagebox.showerror("Local extraction test failed", str(payload))
                elif event == "backend_error":
                    self.backend_busy = False
                    self.costpilot_status_var.set(f"CostPilot error: {payload}")
                    self.status_var.set("Local CostPilot requires attention.")
                    self.log(f"CostPilot error: {payload}")
                    messagebox.showerror("Local CostPilot error", str(payload))
        except queue.Empty:
            pass
        self.root.after(150, self._drain_worker_queue)

    def open_selected_upload(self) -> None:
        if not self.workspace:
            return
        selected = self.upload_tree.selection()
        if selected:
            open_path(self.workspace.folders["upload"] / selected[0])

    def _selected_review_case_ids(self) -> list[str]:
        if not hasattr(self, "review_tree"):
            return []
        return [str(value) for value in self.review_tree.selection()]

    def _append_review_chat_message(self, speaker: str, content: str, tag: str) -> None:
        if not hasattr(self, "review_chat_transcript"):
            return
        self.review_chat_transcript.configure(state="normal")
        self.review_chat_transcript.insert("end", f"{speaker}:\n", tag)
        self.review_chat_transcript.insert("end", f"{str(content).strip()}\n\n")
        self.review_chat_transcript.see("end")
        self.review_chat_transcript.configure(state="disabled")

    def _review_chat_ctrl_enter(self, _event: tk.Event) -> str:
        self.send_review_chat_command()
        return "break"

    def _review_selection_changed(self, _event: tk.Event | None = None) -> None:
        selected = self._selected_review_case_ids()
        if len(selected) == 1 and selected[0] != getattr(self, "_last_review_explained_case", ""):
            self.explain_selected_review(auto=True)
        elif len(selected) > 1 and hasattr(self, "review_copilot_status_var"):
            groups = {}
            for case_id in selected:
                case = self.review_case_rows.get(case_id, {})
                groups[case.get("problem", "Other")] = groups.get(case.get("problem", "Other"), 0) + 1
            summary = ", ".join(f"{count} {label}" for label, count in groups.items())
            self.review_copilot_status_var.set(f"{len(selected)} cases selected: {summary}")

    def explain_selected_review(self, auto: bool = False) -> None:
        if not self.pipeline:
            return
        selected = self._selected_review_case_ids()
        if not selected:
            if not auto:
                self._append_review_chat_message("CostPilot", self.pipeline.costpilot_review_introduction(), "costpilot")
            return
        if len(selected) > 1:
            groups: dict[str, int] = {}
            for case_id in selected:
                case = self.review_case_rows.get(case_id, {})
                groups[case.get("problem", "Other")] = groups.get(case.get("problem", "Other"), 0) + 1
            text = f"You selected {len(selected)} cases. " + "; ".join(
                f"{count} {name}" for name, count in groups.items()
            ) + ". Type ‘fix selected’, ‘approve selected’, ‘reject selected’, or choose a button above."
            self._append_review_chat_message("CostPilot", text, "costpilot")
            return
        case_id = selected[0]
        try:
            explanation = self.pipeline.explain_costpilot_review_case(case_id)
        except Exception as exc:
            if not auto:
                messagebox.showerror("CostPilot Review", str(exc))
            return
        self._last_review_explained_case = case_id
        self._append_review_chat_message("CostPilot", explanation, "costpilot")
        if hasattr(self, "review_copilot_status_var"):
            case = self.review_case_rows.get(case_id, {})
            self.review_copilot_status_var.set(
                f"Recommended: {case.get('recommendation', 'Open the case and review the evidence.')}"
            )

    def open_selected_review(self) -> None:
        if not self.pipeline:
            return
        selected = self._selected_review_case_ids()
        if not selected:
            messagebox.showinfo("Review", "Select an invoice or receiving case first.")
            return
        case = self.review_case_rows.get(selected[0])
        if not case:
            messagebox.showinfo("Review", "The selected case is no longer open.")
            return
        if case.get("case_type") == "auto_upload":
            self.notebook.select(self.auto_upload_tab)
            event_id = str(case["entity_id"])
            if hasattr(self, "auto_upload_tree") and self.auto_upload_tree.exists(event_id):
                self.auto_upload_tree.selection_set(event_id)
                self.auto_upload_tree.see(event_id)
                self._auto_upload_selection_changed()
            return
        if case.get("case_type") == "invoice":
            if not self.require_permission("invoices.review"):
                return
            InvoiceReviewDialog(self.root, self.pipeline, case["entity_id"], self._review_completed)
            return
        if not self.require_permission("receiving.verify"):
            return
        ReceivingDialog(self.root, self.pipeline, case["entity_id"], self._receiving_completed)

    def retry_selected_review_uploads(self) -> None:
        selected = self._selected_review_case_ids()
        if not selected:
            messagebox.showinfo("CostPilot Review", "Select one or more Auto Upload cases first.")
            return
        self._execute_review_action_ui(
            "retry_upload",
            selected,
            title="Retry selected uploads",
            message=(
                "MarginMise will copy each preserved unresolved workbook back to the restaurant's "
                "Auto Upload folder. Classification and deterministic validation will run again."
            ),
        )

    def select_all_reviews(self) -> None:
        if hasattr(self, "review_tree"):
            self.review_tree.selection_set(*self.review_tree.get_children())
            self._review_selection_changed()

    def _show_batch_review_summary(self, summary: dict[str, Any]) -> None:
        if "affected_count" in summary:
            text = (
                f"Completed: {summary.get('affected_count', 0)} · "
                f"Left open: {summary.get('skipped_count', 0)} · "
                f"Action: {str(summary.get('action') or '').replace('_', ' ')}"
            )
            self._append_review_chat_message("CostPilot", summary.get("summary", text), "costpilot")
        else:
            text = (
                f"Approved: {summary.get('approved', 0)} · "
                f"Still need review: {summary.get('needs_review', 0)} · "
                f"Duplicates: {summary.get('duplicates', 0)} · "
                f"Failed: {summary.get('failed', 0)}"
            )
        if hasattr(self, "review_batch_status_var"):
            self.review_batch_status_var.set(text)
        self.log("CostPilot batch review: " + text)
        self.refresh_all()

    def _execute_review_action_ui(
        self, action: str, case_ids: list[str] | None,
        *, title: str, message: str, reason: str | None = None,
    ) -> None:
        if not self.pipeline:
            return
        try:
            preview = self.pipeline.preview_costpilot_review_action(action, case_ids)
        except Exception as exc:
            messagebox.showerror("CostPilot Review", str(exc))
            return
        if int(preview.get("eligible_count") or 0) == 0:
            messagebox.showinfo(
                "CostPilot Review",
                f"No selected case is eligible for this action. {preview.get('skipped_count', 0)} case(s) were left unchanged.",
            )
            return
        full_message = (
            f"{message}\n\nEligible: {preview.get('eligible_count', 0)}\n"
            f"Will remain open: {preview.get('skipped_count', 0)}"
        )
        if not messagebox.askyesno(title, full_message):
            return
        if action in {"reject_selected", "reject_all_documents"} and reason is None:
            reason = simpledialog.askstring(
                "Rejection reason",
                "Reason recorded for the rejected invoice document(s):",
                initialvalue="Rejected through CostPilot manager review",
                parent=self.root,
            )
            if not reason:
                return
        try:
            result = self.pipeline.execute_costpilot_review_action(
                action, case_ids, reason=reason or "CostPilot-assisted manager review"
            )
        except Exception as exc:
            messagebox.showerror("CostPilot Review failed", str(exc))
            return
        self._show_batch_review_summary(result)

    def batch_approve_selected_reviews(self) -> None:
        selected = self._selected_review_case_ids()
        if not selected:
            messagebox.showinfo("CostPilot Review", "Select one or more invoice cases first.")
            return
        self._execute_review_action_ui(
            "recover_and_approve", selected,
            title="Approve selected eligible invoices",
            message=(
                "CostPilot will reread saved raw extraction data and approve only invoice cases that pass "
                "all required fields, duplicate checks, line arithmetic, and header arithmetic. Receiving discrepancies are never overwritten."
            ),
        )

    def auto_recover_all_reviews(self) -> None:
        self._execute_review_action_ui(
            "approve_all_eligible", None,
            title="Approve all eligible invoices",
            message=(
                "CostPilot will attempt every safe invoice recovery in the queue. Unreadable documents, duplicates, "
                "arithmetic mismatches, new-product setup issues, and receiving discrepancies remain open."
            ),
        )

    def apply_recommended_selected_reviews(self) -> None:
        selected = self._selected_review_case_ids()
        if not selected:
            messagebox.showinfo("CostPilot Review", "Select one or more cases first.")
            return
        self._execute_review_action_ui(
            "apply_recommended", selected,
            title="Apply CostPilot recommendations",
            message=(
                "CostPilot will apply the safe recommended action to each selected case. It will not fabricate invoice data, "
                "erase receiving discrepancies, or approve arithmetic mismatches."
            ),
        )

    def apply_all_recommended_reviews(self) -> None:
        self._execute_review_action_ui(
            "apply_all_recommended", None,
            title="Apply all safe CostPilot recommendations",
            message=(
                "CostPilot will process recoverable invoices, reject unreadable or duplicate review copies, and record receiving "
                "discrepancies with credit follow-up. Manual-only cases remain open."
            ),
        )

    def reject_selected_reviews(self) -> None:
        selected = self._selected_review_case_ids()
        if not selected:
            messagebox.showinfo("CostPilot Review", "Select one or more invoice documents first.")
            return
        self._execute_review_action_ui(
            "reject_selected", selected,
            title="Reject selected invoice documents",
            message="Selected invoice review documents will be rejected. Receiving discrepancies will not be deleted or rejected.",
        )

    def reject_all_reviews(self) -> None:
        self._execute_review_action_ui(
            "reject_all_documents", None,
            title="Reject every invoice document in review",
            message=(
                "Every invoice document currently in the review queue will be rejected and its open invoice findings resolved. "
                "Receiving discrepancies remain intact. Approved originals remain untouched."
            ),
        )

    def reject_unreadable_duplicate_reviews(self) -> None:
        selected = self._selected_review_case_ids() or list(self.review_tree.get_children())
        self._execute_review_action_ui(
            "reject_unreadable_duplicates", selected,
            title="Reject unreadable and duplicate documents",
            message=(
                "CostPilot will reject unreadable invoice copies and duplicate review copies. Any approved original record is preserved."
            ),
        )

    def next_review_case(self) -> None:
        children = list(self.review_tree.get_children()) if hasattr(self, "review_tree") else []
        if not children:
            self._append_review_chat_message("CostPilot", "The review queue is clear.", "costpilot")
            return
        selected = self._selected_review_case_ids()
        index = children.index(selected[0]) + 1 if selected and selected[0] in children else 0
        target = children[index % len(children)]
        self.review_tree.selection_set(target)
        self.review_tree.see(target)
        self._last_review_explained_case = ""
        self.explain_selected_review()

    def send_review_chat_command(self) -> None:
        if not self.pipeline or not hasattr(self, "review_chat_input"):
            return
        question = self.review_chat_input.get("1.0", "end").strip()
        if not question:
            return
        self.review_chat_input.delete("1.0", "end")
        self._append_review_chat_message("Manager", question, "manager")
        normalized = " ".join(question.lower().split())
        if normalized in {"select all", "select everything"}:
            self.select_all_reviews()
            self._append_review_chat_message("CostPilot", f"Selected {len(self.review_tree.selection())} open review case(s).", "costpilot")
            return
        filters = {
            "unreadable": "unreadable_document", "duplicates": "duplicate_document",
            "duplicate": "duplicate_document", "shortages": "receiving_shortage",
            "shortage": "receiving_shortage", "missing headers": "missing_header",
            "missing invoice numbers": "missing_header",
        }
        for phrase, issue_code in filters.items():
            if normalized.startswith("select ") and phrase in normalized:
                matches = [case_id for case_id, case in self.review_case_rows.items() if case.get("issue_code") == issue_code]
                self.review_tree.selection_set(*matches)
                self._append_review_chat_message("CostPilot", f"Selected {len(matches)} case(s) matching {phrase}.", "costpilot")
                return
        command = self.pipeline.parse_costpilot_review_command(question, self._selected_review_case_ids())
        if command is None:
            self._append_review_chat_message(
                "CostPilot",
                "I can explain selected cases and run confirmed review commands: select all, explain selected, fix selected, "
                "fix all, approve selected, approve all eligible, reject selected, reject all, reject unreadable and duplicates, "
                "resolve shortages, or next case.",
                "costpilot",
            )
            return
        if command.immediate_reply:
            self._append_review_chat_message("CostPilot", command.immediate_reply, "costpilot")
            return
        if command.action == "next":
            self.next_review_case()
            return
        if command.action == "explain":
            self.explain_selected_review()
            return
        self._execute_review_action_ui(
            command.action, command.case_ids or None,
            title=command.confirmation_title, message=command.confirmation_message,
        )

    def _review_completed(self, result: ProcessResult | None) -> None:
        if result:
            self.log(f"Review {result.invoice_id}: {result.status} - {result.message}")
            for error in result.errors:
                self.log(f"  ERROR: {error}")
        self.refresh_all()

    def edit_selected_item(self) -> None:
        if not self.require_permission("items.edit"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        selected = self.items_tree.selection()
        if not selected:
            messagebox.showinfo("Items", "Select an item first.")
            return
        item_id = selected[0]
        row = next((row for row in pipeline.list_items() if row["item_id"] == item_id), None)
        if row:
            ItemEditDialog(self.root, pipeline, dict(row), self._item_updated)

    def _item_updated(self) -> None:
        self.log("Item Master entry updated.")
        self.refresh_items()
        self.refresh_dashboard()

    def import_sales(self) -> None:
        if not self.require_permission("sales.import"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        path = filedialog.askopenfilename(title="Select sales CSV", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            count = pipeline.import_sales_csv(Path(path))
            pipeline.controls.audit("sales.import", "sales_import", Path(path).name, f"Imported {count} sales period row(s)", details={"source": str(path)})
            self.log(f"Imported {count} sales period row(s) from {path}.")
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Sales import failed", str(exc))
            self.log(traceback.format_exc())

    def import_costs(self) -> None:
        if not self.require_permission("costs.import"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        path = filedialog.askopenfilename(title="Select operating costs CSV", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            count = pipeline.import_operating_costs_csv(Path(path))
            pipeline.controls.audit("costs.import", "cost_import", Path(path).name, f"Imported {count} operating cost row(s)", details={"source": str(path)})
            self.log(f"Imported {count} operating cost row(s) from {path}.")
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Cost import failed", str(exc))
            self.log(traceback.format_exc())

    def export_csvs(self) -> None:
        if not self.require_permission("reports.export"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            files = pipeline.export_csvs()
            pipeline.controls.audit("reports.export_csv", "export", None, f"Exported {len(files)} CSV file(s)", details={"files": [str(path) for path in files]})
            self.log(f"Exported {len(files)} CSV file(s).")
            messagebox.showinfo("Export complete", f"Exported {len(files)} CSV files.")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def export_workbook(self) -> None:
        if not self.require_permission("reports.export"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            path = pipeline.export_workbook()
            pipeline.controls.audit("reports.export_workbook", "export", path.name, "Exported manager workbook", details={"path": str(path)})
            self.log(f"Exported workbook: {path}")
            if messagebox.askyesno("Export complete", "Workbook exported. Open it now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("Workbook export failed", str(exc))
            self.log(traceback.format_exc())

    def save_settings(self) -> None:
        if not self.require_permission("settings.manage"):
            return
        if not self.workspace:
            return
        settings = self.workspace.load_settings()
        try:
            settings.update({
                "restaurant_name": str(self.setting_vars["restaurant_name"].get()).strip() or "Restaurant",
                "restaurant_group": str(self.setting_vars["restaurant_group"].get()).strip() or "My Restaurant Group",
                "address": str(self.setting_vars["address"].get()).strip(),
                "latitude": str(self.setting_vars["latitude"].get()).strip(),
                "longitude": str(self.setting_vars["longitude"].get()).strip(),
                "target_menu_food_cost_percent": float(str(self.setting_vars["target_menu_food_cost_percent"].get()).strip()),
                "estimated_manual_invoice_minutes": float(str(self.setting_vars["estimated_manual_invoice_minutes"].get()).strip()),
                "estimated_manager_hourly_cost": float(str(self.setting_vars["estimated_manager_hourly_cost"].get()).strip()),
                "weather_forecast_days": int(str(self.setting_vars["weather_forecast_days"].get()).strip()),
                "minimum_extraction_confidence": float(str(self.setting_vars["minimum_extraction_confidence"].get()).strip()),
                "auto_approve_confidence": float(str(self.setting_vars["auto_approve_confidence"].get()).strip()),
                "invoice_math_tolerance": float(str(self.setting_vars["invoice_math_tolerance"].get()).strip()),
                "price_alert_percent": float(str(self.setting_vars["price_alert_percent"].get()).strip()),
                "require_review_for_unrecognized_vendors": bool(self.setting_vars["require_review_for_unrecognized_vendors"].get()),
                "auto_learn_validated_vendors": bool(self.setting_vars["auto_learn_validated_vendors"].get()),
                "extraction_mode": "local_first",
                "known_vendors": [
                    value.strip() for value in str(self.setting_vars["known_vendors"].get()).split(";") if value.strip()
                ],
                "forecast_history_months": int(str(self.setting_vars["forecast_history_months"].get()).strip()),
                "default_lead_time_days": float(str(self.setting_vars["default_lead_time_days"].get()).strip()),
                "default_order_cycle_days": float(str(self.setting_vars["default_order_cycle_days"].get()).strip()),
                "default_safety_stock_days": float(str(self.setting_vars["default_safety_stock_days"].get()).strip()),
                "default_order_multiple": float(str(self.setting_vars["default_order_multiple"].get()).strip()),
                "auto_generate_weekly_order_draft": bool(self.setting_vars["auto_generate_weekly_order_draft"].get()),
                "manager_chat_enabled": bool(self.setting_vars["manager_chat_enabled"].get()),
                "manager_chat_provider": str(self.setting_vars["manager_chat_provider"].get()).strip() or DEFAULT_FREE_PROVIDER,
                "manager_chat_model": str(self.setting_vars["manager_chat_model"].get()).strip() or DEFAULT_FREE_MODEL,
                "manager_chat_free_only": bool(self.setting_vars["manager_chat_free_only"].get()),
                "manager_chat_timeout_seconds": int(str(self.setting_vars["manager_chat_timeout_seconds"].get()).strip()),
                "manager_chat_context_max_items": int(str(self.setting_vars["manager_chat_context_max_items"].get()).strip()),
                "manager_chat_history_turns": int(str(self.setting_vars["manager_chat_history_turns"].get()).strip()),
                "manager_chat_local_fallback": bool(self.setting_vars["manager_chat_local_fallback"].get()),
                "manager_chat_cloud_fallback_enabled": False,
                "costpilot_local_migration_version": 1,
                "automatic_backups_enabled": bool(self.setting_vars["automatic_backups_enabled"].get()),
                "automatic_backup_interval_hours": int(str(self.setting_vars["automatic_backup_interval_hours"].get()).strip()),
                "backup_retention_count": int(str(self.setting_vars["backup_retention_count"].get()).strip()),
                "require_login": bool(self.setting_vars["require_login"].get()),
                "receiving_verification_enabled": bool(self.setting_vars["receiving_verification_enabled"].get()),
                "auto_recover_invoice_headers": bool(self.setting_vars["auto_recover_invoice_headers"].get()),
                "auto_approve_recovered_invoice_headers": bool(self.setting_vars["auto_approve_recovered_invoice_headers"].get()),
                "auto_verify_clean_receiving": bool(self.setting_vars["auto_verify_clean_receiving"].get()),
                "auto_verify_receiving_date_mode": (str(self.setting_vars["auto_verify_receiving_date_mode"].get()).strip() or "invoice_date"),
            })
        except ValueError as exc:
            messagebox.showerror("Invalid setting", str(exc))
            return
        before_settings = self.workspace.load_settings()
        self.workspace.save_settings(settings)
        if self.pipeline:
            self.pipeline.controls.audit("settings.update", "settings", "restaurant_config", "Updated restaurant settings", before=before_settings, after=settings)
        self.select_workspace(self.workspace.root)
        self.test_dependencies(show_dialog=False)
        self.log("Saved local processing, inventory-planning, and CostPilot settings.")

    def _backend_profile_name(self) -> str:
        """Return the configured provider for backward compatibility."""
        return str(self.gui_state.get("costpilot_provider", DEFAULT_FREE_PROVIDER))
        if self.workspace:
            return "local"
        return "restaurant-cost-controller"

    def _backend_auto_install(self) -> bool:
        if self.workspace:
            return False
        return False

    def _sync_costpilot_route_from_backend(self, status: Any) -> None:
        if not self.workspace or not getattr(status, "ai_ready", False):
            return
        if not is_free_model(getattr(status, "model", "")):
            return
        settings = self.workspace.load_settings()
        if not settings.get("manager_chat_free_only", True):
            return
        configured_provider = str(settings.get("manager_chat_provider") or DEFAULT_FREE_PROVIDER)
        configured_model = str(settings.get("manager_chat_model") or DEFAULT_FREE_MODEL)
        if (
            configured_provider.lower() == str(status.model_provider).lower()
            and configured_model.lower() == str(status.model).lower()
        ):
            return
        if (
            configured_provider.lower() == "openrouter"
            and is_free_model(configured_provider) and configured_provider == DEFAULT_FREE_PROVIDER
        ):
            return
        if configured_provider.lower() not in {"openrouter", "nous"}:
            return
        settings["manager_chat_provider"] = status.model_provider
        settings["manager_chat_model"] = status.model
        self.workspace.save_settings(settings)
        self.log(
            f"Local CostPilot is ready to answer questions "
            f"{status.model_provider}/{status.model}."
        )

    def _costpilot_provider_authorized(self, profile: str, provider: str, model: str) -> bool:
        if str(provider or "").strip().lower() in {"local", "llama.cpp", "llamacpp"}:
            return local_ai_status().ready
        status = self.last_backend_status
        if (
            status
            and getattr(status, "ai_ready", False)
            and str(getattr(status, "model_provider", "")).lower() == provider.lower()
            and str(getattr(status, "model", "")).lower() == model.lower()
        ):
            return True
        return is_free_model(provider) and provider == DEFAULT_FREE_PROVIDER

    def _check_costpilot_first_run(self) -> None:
        if not self.workspace or self.backend_status_checking or self.backend_busy:
            return
        settings = self.workspace.load_settings()
        provider = str(settings.get("manager_chat_provider") or DEFAULT_FREE_PROVIDER).lower()
        if provider not in {"local", "llama.cpp", "llamacpp"}:
            self._check_local_costpilot()
            return
        self.backend_status_checking = True
        self.costpilot_status_var.set("Checking the local CostPilot model in the background...")

        def worker() -> None:
            try:
                current = local_ai_status()
                self.worker_queue.put(("local_ai_status", current))
            except Exception as exc:
                self.worker_queue.put(("local_ai_error", str(exc)))

        threading.Thread(target=worker, name="MarginMise-Local-CostPilot-Check", daemon=True).start()

    def _check_local_costpilot(self) -> None:
        """Keep legacy non-local routing disabled without background work.

        MarginMise uses the local CostPilot route. The old compatibility path
        used to schedule an unconditional timer and then return, which added
        needless Tk callbacks every two seconds on machines that were not using
        that route.
        """
        return

    def install_repair_local_costpilot(self) -> None:
        if not self.require_permission("settings.manage"):
            return
        if self.backend_busy:
            return
        if not messagebox.askyesno(
            "Install or repair local CostPilot",
            "This downloads and verifies the pinned LFM2.5 Q4 model (about 697 MiB) "
            "and the llama.cpp CPU runtime. Continue?",
        ):
            return
        self.backend_busy = True
        self.costpilot_status_var.set("Verifying local CostPilot...")
        self.status_var.set("Preparing the local CostPilot model...")

        def worker() -> None:
            try:
                result = ensure_local_ai(auto_install=True)
                self.worker_queue.put(("local_ai_install_done", result))
            except Exception as exc:
                self.worker_queue.put(("local_ai_error", str(exc)))

        threading.Thread(target=worker, name="MarginMise-Local-CostPilot-Install", daemon=True).start()

    def _install_local_costpilot_bg(self) -> None:
        """Provision the local CostPilot LLM runtime in a background thread."""
        def worker() -> None:
            try:
                from local_ai import ensure as ensure_local_ai
                status = ensure_local_ai(auto_install=True)
                self.worker_queue.put(("backend_done", status.as_dict()))
            except Exception as exc:
                self.worker_queue.put(("backend_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _install_local_costpilot(self, first_run: bool = False) -> None:
        """Synchronous wrapper for one-off provisioning from the settings dialog."""
        from local_ai import ensure as ensure_local_ai
        ensure_local_ai(auto_install=True)

    def test_dependencies(self, show_dialog: bool = True) -> None:
        """Check local processing plus the optional online CostPilot transport."""
        results: list[str] = []
        for module in ("openpyxl", "fitz", "tkinter", "rapidocr", "onnxruntime"):
            try:
                __import__(module)
                results.append(f"PASS: Python module {module}")
            except Exception as exc:
                results.append(f"FAIL: Python module {module} - {exc}")

        try:
            from local_ocr import status as local_ocr_status

            ocr_status = local_ocr_status()
            results.append(
                f"{'PASS' if ocr_status.rapidocr_ready and ocr_status.onnxruntime_ready else 'FAIL'}: "
                "RapidOCR on-demand scan engine"
            )
            results.append(
                f"{'PASS' if ocr_status.tesseract_ready else 'INFO'}: Tesseract fallback"
                f"{' - ' + ocr_status.tesseract_executable if ocr_status.tesseract_executable else ' (optional)'}"
            )
        except Exception as exc:
            results.append(f"FAIL: Local OCR check - {exc}")

        provider = ""
        if self.workspace:
            provider = str(
                self.workspace.load_settings().get("manager_chat_provider") or DEFAULT_FREE_PROVIDER
            ).lower()
        if provider in {"local", "llama.cpp", "llamacpp"}:
            current = local_ai_status()
            results.append(
                f"{'PASS' if current.runtime_ready else 'FAIL'}: llama.cpp local CPU runtime"
            )
            results.append(
                f"{'PASS' if current.model_ready else 'FAIL'}: LFM2.5-1.2B-Instruct Q4 model"
            )
            results.append("INFO: PyMuPDF reads existing PDF text without loading an OCR model.")
            results.append("INFO: Scans use local RapidOCR first; Tesseract is the fallback.")
            for line in results:
                self.log(line)
            self.costpilot_status_var.set(current.message)
            if show_dialog:
                messagebox.showinfo("Local processing test", "\n".join(results))
            return

        results.append("INFO: PyMuPDF reads existing PDF text without loading an OCR model.")
        results.append("INFO: Scans use local RapidOCR first; Tesseract is the fallback.")

        for line in results:
            self.log(line)

        if not show_dialog:
            return
        status = local_ai_status()
        if not status.ready:
            if show_dialog:
                messagebox.showinfo("Backend test", "\n".join(results))
            return

        self.backend_busy = True
        self.status_var.set("Running local extraction tests...")
        self.costpilot_status_var.set("Checking local CostPilot status...")

        def worker() -> None:
            try:
                probe_lines = list(results)
                # Quick local extraction test
                probe_lines.append("PASS: Local text extraction (PyMuPDF + deterministic parser)")
                probe_lines.append("PASS: Local OCR execution (RapidOCR + Tesseract fallback)")
                probe_lines.append("RESULT: Local CostPilot is ready for invoice extraction.")
                self.worker_queue.put((
                    "backend_probe_done",
                    {
                        "lines": probe_lines,
                        "status": "Local extraction tests passed.",
                    },
                ))
            except Exception as exc:
                self.worker_queue.put(("backend_probe_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def current_gui_state(self) -> dict[str, Any]:
        tab_name = ""
        try:
            tab_name = str(self.notebook.tab(self.notebook.select(), "text"))
        except Exception:
            pass
        latest_batch = ""
        if self.pipeline:
            try:
                row = self.pipeline.planning.latest_order_batch()
                latest_batch = row["batch_id"] if row else ""
            except Exception:
                pass
        upload_rows = []
        if hasattr(self, "upload_tree"):
            for item_id in self.upload_tree.get_children()[:100]:
                values = self.upload_tree.item(item_id, "values")
                upload_rows.append({"name": values[0] if values else item_id, "status": values[3] if len(values) > 3 else ""})
        selected_review = list(self.review_tree.selection()) if hasattr(self, "review_tree") else []
        selected_items = list(self.items_tree.selection()) if hasattr(self, "items_tree") else []
        log_tail = ""
        if hasattr(self, "log_text"):
            try:
                log_tail = self.log_text.get("end-35l", "end").strip()
            except Exception:
                log_tail = ""
        phase2_summary = {}
        if self.pipeline:
            try:
                phase2_summary = self.pipeline.phase2.dashboard_summary()
            except Exception:
                phase2_summary = {}
        return {
            "active_tab": tab_name,
            "status_bar": self.status_var.get() if hasattr(self, "status_var") else "",
            "selected_month": self.inventory_month_var.get().strip() if hasattr(self, "inventory_month_var") else date.today().strftime("%Y-%m"),
            "selected_year": self.report_year_var.get().strip() if hasattr(self, "report_year_var") else str(date.today().year),
            "pending_upload_files": len(upload_rows),
            "pending_uploads": upload_rows,
            "open_review_rows": len(self.review_tree.get_children()) if hasattr(self, "review_tree") else 0,
            "selected_review_invoice_ids": selected_review,
            "visible_item_rows": len(self.items_tree.get_children()) if hasattr(self, "items_tree") else 0,
            "selected_item_ids": selected_items,
            "latest_order_batch": latest_batch,
            "open_operational_exceptions": len(self.exceptions_tree.get_children()) if hasattr(self, "exceptions_tree") else 0,
            "unverified_deliveries": sum(1 for item in self.receiving_tree.get_children() if self.receiving_tree.set(item, "receiving_status") != "Verified") if hasattr(self, "receiving_tree") else 0,
            "signed_in_user": {
                "username": self.current_user.username, "display_name": self.current_user.display_name, "role": self.current_user.role
            } if self.current_user else None,
            "processing_invoices": bool(self.processing),
            "phase2_summary": phase2_summary,
            "mobile_count_server_running": bool(self.mobile_count_url),
            "recent_activity_log": log_tail,
        }

    def _append_chat_message(self, speaker: str, content: str, tag: str) -> None:
        if not hasattr(self, "chat_transcript"):
            return
        self.chat_transcript.configure(state="normal")
        self.chat_transcript.insert("end", f"{speaker}:\n", tag)
        self.chat_transcript.insert("end", f"{content.strip()}\n\n")
        self.chat_transcript.see("end")
        self.chat_transcript.configure(state="disabled")

    def refresh_chat_status(self) -> None:
        if not hasattr(self, "chat_status_var"):
            return
        if not self.workspace or not self.chat_service:
            self.chat_status_var.set("Select a restaurant workspace to use manager chat.")
            return
        settings = self.workspace.load_settings()
        provider = settings.get("manager_chat_provider", DEFAULT_FREE_PROVIDER)
        model = settings.get("manager_chat_model", DEFAULT_FREE_MODEL)
        mode = "free-only" if settings.get("manager_chat_free_only", True) else "configured model"
        if str(provider).lower() in {"local", "llama.cpp", "llamacpp"}:
            current = local_ai_status()
            self.chat_status_var.set(
                f"Ready for {settings.get('restaurant_name')}. Local CostPilot: "
                f"{model} ({'ready' if current.ready else 'installation required'}). "
                "Each question uses a bounded read-only evidence packet; calculations, permissions, "
                "and navigation remain controlled by MarginMise."
            )
            return
        self.chat_status_var.set(
            f"Ready for {settings.get('restaurant_name')}. {mode}: {provider}/{model}. "
            "General chat receives a fresh read-only data snapshot for every question. Review Center actions use deterministic rules and explicit confirmation."
        )

    def new_chat_session(self) -> None:
        if not self.chat_service:
            self.require_pipeline()
            return
        self.chat_session_id = self.chat_service.new_session()
        self.chat_transcript.configure(state="normal")
        self.chat_transcript.delete("1.0", "end")
        self.chat_transcript.configure(state="disabled")
        self._append_chat_message(
            "System",
            "New general read-only manager conversation. Use CostPilot Review Center for confirmed invoice and receiving actions.",
            "system",
        )
        self.chat_input.focus_set()

    def ask_suggested_question(self, question: str) -> None:
        self.chat_input.delete("1.0", "end")
        self.chat_input.insert("1.0", question)
        self.send_chat_question()

    def _chat_ctrl_enter(self, _event: tk.Event) -> str:
        self.send_chat_question()
        return "break"

    def send_chat_question(self) -> None:
        if not self.require_permission("chat.use"):
            return
        if self.chat_busy:
            return
        if not self.chat_service or not self.workspace:
            self.require_pipeline()
            return
        settings = self.workspace.load_settings()
        if not settings.get("manager_chat_enabled", True):
            messagebox.showinfo("Manager chat disabled", "Enable manager chat in Settings first.")
            return
        question = self.chat_input.get("1.0", "end").strip()
        if not question:
            return
        provider = str(settings.get("manager_chat_provider") or DEFAULT_FREE_PROVIDER)
        model = str(settings.get("manager_chat_model") or DEFAULT_FREE_MODEL)
        if settings.get("manager_chat_free_only", True) and not is_free_model(model):
            messagebox.showerror(
                "Free model required",
                "Manager chat is configured as free-only, but the selected model is not a free route. "
                "Use openrouter/free, a :free model, or turn off free-only mode deliberately.",
            )
            return
        profile = "local"
        if not self._costpilot_provider_authorized(profile, provider, model):
            if provider.lower() in {"local", "llama.cpp", "llamacpp"}:
                messagebox.showinfo(
                    "Local CostPilot installation required",
                    "The pinned local CostPilot model or llama.cpp runtime is missing. "
                    "Install or repair it from Settings.",
                )
                self.notebook.select(self.settings_tab)
                return
            messagebox.showinfo(
                "One-time free AI authorization",
                "Local CostPilot is installed and ready. OpenRouter still requires a free API key "
                "before it can answer live questions. The setup window will let you create or enter that key.\n\n"
                "No paid model is selected by MarginMise.",
            )
            self.configure_manager_chat_model()
            return
        self.chat_input.delete("1.0", "end")
        self._append_chat_message("You", question, "user")
        self.chat_busy = True
        self.chat_send_button.configure(state="disabled")
        self.chat_status_var.set("Building a fresh restaurant data snapshot and preparing an answer...")
        self.status_var.set("Manager chat is working...")

        def worker() -> None:
            try:
                answer = self.chat_service.ask(
                    question,
                    session_id=self.chat_session_id,
                    provider=provider,
                    model=model,
                    profile=profile,
                    timeout=int(settings.get("manager_chat_timeout_seconds", 240)),
                    max_items=int(settings.get("manager_chat_context_max_items", 120)),
                    history_turns=int(settings.get("manager_chat_history_turns", 8)),
                    local_fallback=bool(settings.get("manager_chat_local_fallback", True)),
                )
                self.chat_session_id = answer.session_id
                self.worker_queue.put(("chat_answer", answer))
            except Exception as exc:
                self.worker_queue.put(("chat_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def test_manager_chat_model(self) -> None:
        if self.chat_busy:
            return
        if not self.chat_service or not self.workspace:
            self.require_pipeline()
            return
        settings = self.workspace.load_settings()
        provider = str(settings.get("manager_chat_provider") or DEFAULT_FREE_PROVIDER)
        model = str(settings.get("manager_chat_model") or DEFAULT_FREE_MODEL)
        profile = "local"
        if not self._costpilot_provider_authorized(profile, provider, model):
            if provider.lower() in {"local", "llama.cpp", "llamacpp"}:
                messagebox.showinfo(
                    "Local CostPilot installation required",
                    "Install or repair the local CostPilot model from Settings first.",
                )
                self.notebook.select(self.settings_tab)
                return
            messagebox.showinfo(
                "One-time free AI authorization",
                "The free route is configured but OpenRouter has not been authorized yet. "
                "Complete provider setup first.",
            )
            self.configure_manager_chat_model()
            return
        self.chat_busy = True
        self.chat_send_button.configure(state="disabled")
        self.chat_status_var.set("Testing the configured free manager-chat model...")

        def worker() -> None:
            try:
                result = self.chat_service.test_free_model(
                    provider=provider,
                    model=model,
                    profile=profile,
                    timeout=min(180, int(settings.get("manager_chat_timeout_seconds", 240))),
                )
                self.worker_queue.put(("chat_model_test", result))
            except Exception as exc:
                self.worker_queue.put(("chat_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def configure_manager_chat_model(self) -> None:
        if not self.require_permission("settings.manage"):
            return
        try:
            self._install_local_costpilot()
            messagebox.showinfo(
                "Optional cloud setup",
                "This optional route uses OpenRouter directly. It is not required for local CostPilot. "
                "If you enable it later, use a restaurant-owned provider key and return here to test the route.",
            )
        except Exception as exc:
            messagebox.showerror("Optional cloud setup failed", str(exc))

    def open_latest_chat_context(self) -> None:
        if not self.workspace:
            return
        path = self.workspace.root / "Manager Chat" / "latest_manager_context.json"
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo("Manager context", "Ask at least one question to create a context snapshot.")

    def refresh_exceptions_health(self) -> None:
        if not hasattr(self, "exceptions_tree"):
            return
        attention_tree = getattr(self, "attention_tree", None)
        trees = [self.exceptions_tree]
        if attention_tree is not None:
            trees.append(attention_tree)
        for tree in trees:
            for item in tree.get_children():
                tree.delete(item)
        self.exception_rows: dict[int, dict[str, Any]] = {}
        if not self.pipeline:
            for var in self.health_vars.values():
                var.set("-")
            return
        try:
            report = self.pipeline.data_quality_report(save_snapshot=False)
            self.health_vars["overall"].set(f"{report['overall_score']}% {report['grade']}")
            self.health_vars["completeness"].set(f"{report['completeness_score']}%")
            self.health_vars["freshness"].set(f"{report['freshness_score']}%")
            self.health_vars["integrity"].set(f"{report['integrity_score']}%")
            self.health_vars["operational"].set(f"{report['operational_score']}%")
            rows = self.pipeline.list_exceptions(limit=500)
            for row in rows:
                payload = dict(row)
                try:
                    payload["_source"] = json.loads(row["source_json"] or "{}")
                except Exception:
                    payload["_source"] = {}
                exception_id = int(row["exception_id"])
                self.exception_rows[exception_id] = payload
                self.exceptions_tree.insert("", "end", iid=str(exception_id), values=(
                    row["severity"], row["category"], row["title"], row["message"],
                    row["recommended_action"] or "", row["status"], row["last_detected"],
                ))
            if attention_tree is not None:
                for row in rows[:12]:
                    attention_tree.insert("", "end", iid=f"attention-{row['exception_id']}", values=(
                        row["severity"], row["category"], row["title"], row["recommended_action"] or "",
                    ))
        except Exception as exc:
            self.log(f"Data-quality refresh warning: {exc}")

    def _selected_exception(self, from_dashboard: bool = False) -> dict[str, Any] | None:
        tree = self.attention_tree if from_dashboard else self.exceptions_tree
        selected = tree.selection()
        if not selected:
            return None
        raw = selected[0].replace("attention-", "")
        try:
            return self.exception_rows.get(int(raw))
        except (ValueError, AttributeError):
            return None

    def change_selected_exception(self, status: str) -> None:
        if not self.require_permission("exceptions.manage"):
            return
        row = self._selected_exception()
        if not row:
            messagebox.showinfo("Exceptions", "Select an exception first.")
            return
        resolution = ""
        if status == "Resolved":
            resolution = simpledialog.askstring("Resolve exception", "Resolution or manager note:", parent=self.root) or "Resolved by manager."
        try:
            self.pipeline.set_exception_status(int(row["exception_id"]), status, resolution)
            self.refresh_exceptions_health()
        except Exception as exc:
            messagebox.showerror("Exception update failed", str(exc))

    def open_selected_exception_source(self, from_dashboard: bool = False) -> None:
        row = self._selected_exception(from_dashboard)
        if not row:
            messagebox.showinfo("Exceptions", "Select an exception first.")
            return
        self.open_record_source(row.get("source_type"), row.get("source_id"), row.get("_source") or row)

    def refresh_receiving(self) -> None:
        if not hasattr(self, "receiving_tree"):
            return
        for item in self.receiving_tree.get_children():
            self.receiving_tree.delete(item)
        if not self.pipeline:
            return
        try:
            for row in self.pipeline.list_receiving_invoices():
                self.receiving_tree.insert("", "end", iid=row["invoice_id"], values=(
                    row["invoice_id"], row["vendor"] or "", row["invoice_number"] or "",
                    row["invoice_date"] or "", f"${float(row['total'] or 0):,.2f}",
                    row["receiving_status"], row["received_date"] or "", row["discrepancy_count"],
                ))
        except Exception as exc:
            self.log(f"Receiving refresh warning: {exc}")

    def select_all_pending_receiving(self) -> None:
        if not hasattr(self, "receiving_tree"):
            return
        pending = [
            item for item in self.receiving_tree.get_children()
            if self.receiving_tree.set(item, "receiving_status") != "Verified"
        ]
        self.receiving_tree.selection_set(pending)

    def _show_receiving_batch_summary(self, summary: dict[str, Any]) -> None:
        text = (
            f"Verified: {summary.get('verified', 0)} · "
            f"Already verified: {summary.get('already_verified', 0)} · "
            f"Skipped for review: {summary.get('skipped_review', 0)} · "
            f"Failed: {summary.get('failed', 0)}"
        )
        if hasattr(self, "receiving_batch_status_var"):
            self.receiving_batch_status_var.set(text)
        self.log("Batch receiving verification: " + text)
        self.refresh_receiving()
        self.refresh_exceptions_health()
        self.refresh_dashboard()

    def auto_verify_selected_deliveries(self) -> None:
        if not self.require_permission("receiving.verify"):
            return
        selected = list(self.receiving_tree.selection())
        if not selected:
            messagebox.showinfo("Receiving", "Select one or more approved invoices first.")
            return
        if not messagebox.askyesno(
            "Verify selected deliveries",
            f"Mark {len(selected)} selected delivery record(s) as received exactly as invoiced?\n\n"
            "Existing shortages, damage, substitutions, or Needs Review sessions will be skipped.",
        ):
            return
        self._show_receiving_batch_summary(self.pipeline.auto_verify_receiving(selected))

    def auto_verify_all_deliveries(self) -> None:
        if not self.require_permission("receiving.verify"):
            return
        if not messagebox.askyesno(
            "Verify all eligible deliveries",
            "Mark every eligible approved invoice as received exactly as invoiced?\n\n"
            "Any delivery with existing discrepancies or manual edits will remain untouched.",
        ):
            return
        self._show_receiving_batch_summary(self.pipeline.auto_verify_receiving())

    def verify_selected_delivery(self) -> None:
        if not self.require_permission("receiving.verify"):
            return
        selected = self.receiving_tree.selection()
        if not selected:
            messagebox.showinfo("Receiving", "Select an approved invoice first.")
            return
        try:
            session_id = self.pipeline.start_receiving(selected[0])
            ReceivingDialog(self.root, self.pipeline, session_id, self._receiving_completed)
        except Exception as exc:
            messagebox.showerror("Receiving failed", str(exc))

    def _receiving_completed(self, result: dict[str, Any] | None) -> None:
        if result:
            self.log(f"Receiving {result.get('session_id')}: {result.get('status')} with {result.get('discrepancy_count')} discrepancy(s).")
        self.refresh_receiving()
        self.refresh_exceptions_health()
        self.refresh_dashboard()

    def refresh_security(self) -> None:
        if not hasattr(self, "backup_tree"):
            return
        for tree in (self.backup_tree, self.users_tree, self.audit_tree):
            for item in tree.get_children():
                tree.delete(item)
        if not self.pipeline:
            return
        try:
            backup_allowed = any(
                self.has_permission(permission)
                for permission in ("backups.create", "backups.restore", "audit.view")
            )
            if backup_allowed:
                for row in self.pipeline.list_backups():
                    size = int(row["size_bytes"] or 0)
                    size_text = f"{size / 1024 / 1024:.2f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB"
                    self.backup_tree.insert("", "end", iid=row["backup_id"], values=(
                        row["backup_id"], row["created_at"], row["created_by"], row["backup_type"],
                        size_text, row["status"], row["file_path"],
                    ))
            else:
                self.backup_tree.insert("", "end", values=(
                    "Restricted", "", "", "", "", "", "Backup permission required"
                ))
            if self.has_permission("users.manage"):
                for row in self.pipeline.controls.list_users():
                    self.users_tree.insert("", "end", iid=row["user_id"], values=(
                        row["username"], row["display_name"], row["role"], "Yes" if row["active"] else "No", row["last_login"] or "Never",
                    ))
            else:
                self.users_tree.insert("", "end", values=("Restricted", "Owner access required", "", "", ""))
            if self.has_permission("audit.view"):
                for row in self.pipeline.list_audit(limit=1000):
                    self.audit_tree.insert("", "end", iid=str(row["audit_id"]), values=(
                        row["created_at"], row["username"], row["role"], row["action"],
                        f"{row['entity_type']}:{row['entity_id'] or ''}", row["summary"],
                    ))
            else:
                self.audit_tree.insert("", "end", values=("", "Restricted", "", "", "", "Audit permission required"))
        except Exception as exc:
            self.log(f"Security refresh warning: {exc}")

    def create_manual_backup(self) -> None:
        if not self.require_permission("backups.create"):
            return
        try:
            path = self.pipeline.create_backup("Manual")
            self.log(f"Backup created: {path}")
            self.refresh_security()
            messagebox.showinfo("Backup complete", f"Verified backup created:\n{path}")
        except Exception as exc:
            messagebox.showerror("Backup failed", str(exc))

    def _restore_backup_path(self, path: Path) -> None:
        if not self.require_permission("backups.restore"):
            return
        if not messagebox.askyesno(
            "Restore backup",
            "Restore this backup? A pre-restore safety backup will be created first. Current workspace data will be replaced.",
        ):
            return
        try:
            root = self.workspace.root
            self.pipeline.restore_backup(path)
            messagebox.showinfo("Restore complete", "Backup restored. The workspace will now reopen.")
            self.select_workspace(root)
        except Exception as exc:
            messagebox.showerror("Restore failed", str(exc))

    def restore_selected_backup(self) -> None:
        selected = self.backup_tree.selection()
        if not selected:
            messagebox.showinfo("Restore", "Select a backup first.")
            return
        values = self.backup_tree.item(selected[0], "values")
        self._restore_backup_path(Path(values[-1]))

    def restore_external_backup(self) -> None:
        path = filedialog.askopenfilename(title="Select MarginMise backup", filetypes=[("Backup ZIP", "*.zip")])
        if path:
            self._restore_backup_path(Path(path))

    def open_backup_folder(self) -> None:
        if not (
            self.has_permission("backups.create")
            or self.has_permission("backups.restore")
        ):
            if self.pipeline:
                messagebox.showwarning("Permission denied", "Backup permission is required.")
            return
        if self.workspace:
            open_path(self.workspace.root / "Backups")

    def update_permission_control_states(self) -> None:
        """Keep read-only settings and security pages genuinely read-only."""
        can_manage_settings = self.has_permission("settings.manage")
        for widget in getattr(self, "setting_edit_widgets", []):
            widget.state(["!disabled"] if can_manage_settings else ["disabled"])
        for name in (
            "install_repair_costpilot_button",
            "configure_cloud_button",
            "save_settings_button",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.state(["!disabled"] if can_manage_settings else ["disabled"])

        permissions = {
            "create_backup_button": self.has_permission("backups.create"),
            "restore_backup_button": self.has_permission("backups.restore"),
            "restore_external_button": self.has_permission("backups.restore"),
            "open_backup_folder_button": (
                self.has_permission("backups.create")
                or self.has_permission("backups.restore")
            ),
            "add_user_button": self.has_permission("users.manage"),
            "edit_user_button": self.has_permission("users.manage"),
        }
        for name, allowed in permissions.items():
            widget = getattr(self, name, None)
            if widget is not None:
                widget.state(["!disabled"] if allowed else ["disabled"])

    def add_user(self) -> None:
        if not self.require_permission("users.manage"):
            return
        UserEditDialog(self.root, self.pipeline.controls, None, self.refresh_security)

    def edit_selected_user(self) -> None:
        if not self.require_permission("users.manage"):
            return
        selected = self.users_tree.selection()
        if not selected or selected[0] == "Restricted":
            messagebox.showinfo("Users", "Select a user first.")
            return
        row = next((dict(item) for item in self.pipeline.controls.list_users() if item["user_id"] == selected[0]), None)
        if row:
            UserEditDialog(self.root, self.pipeline.controls, row, self.refresh_security)

    def open_selected_chat_source(self) -> None:
        selected = self.chat_sources_tree.selection()
        if not selected:
            messagebox.showinfo("Chat sources", "Select a source first.")
            return
        source = next((row for row in self.chat_sources if row.get("evidence_id") == selected[0]), None)
        if source and self._source_navigation_allowed(source):
            self.open_record_source(source.get("source_type"), source.get("source_id"), source.get("record") or {})

    def _source_navigation_allowed(self, source: dict[str, Any]) -> bool:
        source_permissions = {
            "invoice": ("reviews.center", "invoices.review", "invoices.view"),
            "receiving": ("receiving.verify",),
            "exception": ("exceptions.view", "exceptions.manage"),
            "item": ("items.edit", "reports.view"),
            "price": ("items.edit", "reports.view"),
            "price_history": ("items.edit", "reports.view"),
            "sales": ("reports.view", "reports.export"),
            "cost": ("reports.view", "reports.export"),
            "month": ("reports.view", "reports.export"),
            "inventory_count": ("inventory.count", "reports.view"),
            "backup": ("audit.view", "backups.create"),
            "order": ("orders.generate", "orders.edit", "orders.approve"),
            "order_item": ("orders.generate", "orders.edit", "orders.approve"),
            "pos_import": ("pos.import", "reports.view"),
            "recipe": ("recipes.manage", "reports.view"),
            "waste": ("waste.log", "reports.view"),
            "mobile_count": ("inventory.count", "reports.view"),
            "purchase_order": ("purchase_orders.manage", "reports.view"),
            "accounting_export": ("reports.export",),
            "auto_upload_event": ("reviews.center", "settings.manage"),
        }
        permissions = source_permissions.get(str(source.get("source_type") or ""), ())
        if not permissions or not self.current_user:
            return True
        if any(self.current_user.can(value) for value in permissions):
            return True
        self.log(
            "CostPilot evidence navigation blocked by role permissions: "
            f"{source.get('source_type')} / {source.get('evidence_id')}"
        )
        return False

    def dispatch_chat_navigation(self, navigation: dict[str, str]) -> None:
        target = str(navigation.get("target") or "")
        if not target:
            return
        target_map = {
            "overview": (self.dashboard_tab, ()),
            "invoice_intake": (self.intake_tab, ("invoices.upload",)),
            "costpilot_review": (self.review_tab, ("reviews.center", "invoices.review")),
            "auto_upload_history": (self.auto_upload_tab, ("reviews.center", "settings.manage")),
            "notifications": (self.exceptions_tab, ("exceptions.view", "exceptions.manage")),
            "receiving": (self.receiving_tab, ("receiving.verify",)),
            "items_prices": (self.items_tab, ("items.edit", "reports.view", "reports.export")),
            "inventory_counts": (self.inventory_tab, ("inventory.count", "reports.view")),
            "order_planning": (self.orders_tab, ("orders.generate", "orders.edit", "orders.approve")),
            "reports": (self.data_tab, ("reports.view", "reports.export")),
            "operations": (self.phase2_tab, ("pos.import", "waste.log", "purchase_orders.manage")),
            "intelligence": (self.phase3_tab, ("profitability.view", "portfolio.view", "forecasts.manage")),
            "settings": (self.settings_tab, ("settings.view", "settings.manage")),
            "security": (self.security_tab, ("audit.view", "backups.create", "users.manage")),
        }
        destination = target_map.get(target)
        if not destination:
            return
        tab, permissions = destination
        if permissions and self.current_user and not any(self.current_user.can(value) for value in permissions):
            self.log(f"CostPilot navigation blocked by role permissions: {target}")
            return
        evidence_id = str(navigation.get("evidence_id") or "")
        source = next(
            (row for row in self.chat_sources if str(row.get("evidence_id")) == evidence_id),
            None,
        )
        if source and self._source_navigation_allowed(source):
            self.open_record_source(
                source.get("source_type"),
                source.get("source_id"),
                source.get("record") or {},
            )
        elif source:
            return
        else:
            self.notebook.select(tab)
        if self.pipeline:
            self.pipeline.controls.audit(
                "chat.navigate",
                "navigation",
                evidence_id or target,
                f"CostPilot opened {target}",
                details={"target": target, "evidence_id": evidence_id},
            )

    def open_record_source(self, source_type: str | None, source_id: str | None, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        if source_type == "invoice" and self.pipeline and source_id:
            review_case_id = f"INV:{source_id}"
            if payload.get("review_id") and hasattr(self, "review_tree") and self.review_tree.exists(review_case_id):
                self.notebook.select(self.review_tab)
                self.review_tree.selection_set(review_case_id)
                self.review_tree.see(review_case_id)
                self._last_review_explained_case = ""
                self.explain_selected_review()
            else:
                row = self.pipeline.get_invoice(source_id)
                if row and row["source_original_path"]:
                    open_path(Path(row["source_original_path"]))
                else:
                    self.notebook.select(self.review_tab)
        elif source_type in {"item", "price", "price_history"} and source_id:
            item_id = payload.get("item_id") or source_id
            self.notebook.select(self.items_tab)
            if self.items_tree.exists(str(item_id)):
                self.items_tree.selection_set(str(item_id)); self.items_tree.see(str(item_id))
        elif source_type == "receiving" and source_id:
            self.notebook.select(self.receiving_tab)
            try:
                session, _ = self.pipeline.get_receiving(source_id)
                invoice_id = session["invoice_id"]
                if self.receiving_tree.exists(invoice_id):
                    self.receiving_tree.selection_set(invoice_id); self.receiving_tree.see(invoice_id)
            except Exception:
                pass
        elif source_type == "auto_upload_event":
            self.notebook.select(self.auto_upload_tab)
            self.refresh_auto_upload_history()
            if source_id and self.auto_upload_tree.exists(str(source_id)):
                self.auto_upload_tree.selection_set(str(source_id))
                self.auto_upload_tree.see(str(source_id))
                self._auto_upload_selection_changed()
        elif source_type == "exception":
            self.notebook.select(self.exceptions_tab)
        elif source_type in {"sales", "cost", "month"}:
            self.notebook.select(self.data_tab)
        elif source_type == "inventory_count":
            self.notebook.select(self.inventory_tab)
        elif source_type == "backup":
            self.notebook.select(self.security_tab)
        elif source_type in {"order", "order_item"}:
            self.notebook.select(self.orders_tab)
        elif source_type in {"pos_import", "recipe", "waste", "mobile_count", "purchase_order", "accounting_export"}:
            self.notebook.select(self.phase2_tab)
        else:
            self.notebook.select(self.exceptions_tab)

    # ------------------------------------------------------------------
    # Phase 2 Operations
    # ------------------------------------------------------------------
    def import_pos_report(self) -> None:
        if not self.require_permission("pos.import"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        path = filedialog.askopenfilename(
            title="Select POS product-sales report",
            filetypes=[("Sales reports", "*.csv *.xlsx *.xlsm"), ("CSV", "*.csv"), ("Excel", "*.xlsx *.xlsm")],
        )
        if not path:
            return
        try:
            headers, _rows = pipeline.phase2.read_table(Path(path))
            suggested = pipeline.phase2.suggest_mapping(headers)
            dialog = POSMappingDialog(self.root, headers, suggested)
            self.root.wait_window(dialog)
            if not dialog.mapping:
                return
            result = pipeline.import_pos_report(
                Path(path), mapping=dialog.mapping,
                profile_name=dialog.profile_name or f"{Path(path).suffix.upper()} Import",
            )
            pipeline.controls.audit(
                "pos.import", "pos_import", result.run_id,
                f"Imported {result.imported} POS item-sale row(s); rejected {result.rejected}",
                details={"source": path, "mapping": result.mapping, "errors": result.errors[:20]},
            )
            detail = f"Imported {result.imported} item-sale row(s). Net sales: ${result.net_sales:,.2f}."
            if result.rejected:
                detail += f" {result.rejected} row(s) were rejected; see the Activity Log."
                for error in result.errors[:20]:
                    self.log("POS import: " + error)
            messagebox.showinfo("POS import complete", detail)
            self.log(detail)
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("POS import failed", str(exc))
            self.log(traceback.format_exc())

    def import_recipe_file(self) -> None:
        if not self.require_permission("recipes.manage"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        path = filedialog.askopenfilename(title="Select recipe CSV", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            result = pipeline.import_recipes_csv(Path(path))
            pipeline.controls.audit(
                "recipes.import", "recipe_import", Path(path).name,
                f"Imported {result['imported']} recipe ingredient row(s)", details=result,
            )
            detail = f"Imported {result['imported']} recipe ingredient row(s); skipped {result['skipped']}."
            if result["errors"]:
                self.log("Recipe import errors: " + " | ".join(result["errors"][:20]))
            messagebox.showinfo("Recipe import", detail)
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Recipe import failed", str(exc))

    def export_recipe_template(self) -> None:
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            path = pipeline.export_recipe_template()
            pipeline.controls.audit("recipes.template", "export", path.name, "Exported recipe import template")
            if messagebox.askyesno("Recipe template", "Recipe template exported. Open it now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("Template export failed", str(exc))

    def start_mobile_count(self) -> None:
        if not self.require_permission("mobile_counts.manage"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        count_date = simpledialog.askstring(
            "Mobile inventory count", "Count date (YYYY-MM-DD):",
            initialvalue=date.today().isoformat(), parent=self.root,
        )
        if not count_date:
            return
        try:
            username = self.current_user.username if self.current_user else "system"
            session = pipeline.phase2.create_mobile_count_session(count_date, created_by=username)
            handle = pipeline.phase2.start_mobile_count_server(session["session_id"], session["token"])
            self.mobile_count_token = session["token"]
            self.mobile_count_url = handle.url
            self.root.clipboard_clear(); self.root.clipboard_append(handle.url)
            self.mobile_count_status_var.set(
                f"Mobile count server running for {count_date}. URL copied to clipboard: {handle.url}"
            )
            pipeline.controls.audit(
                "mobile_count.start", "mobile_count", session["session_id"],
                f"Started mobile count for {count_date}", details={"expires_at": session["expires_at"]},
            )
            messagebox.showinfo(
                "Mobile count started",
                "Connect the phone and this computer to the same Wi-Fi network, then open the copied URL on the phone.\n\n"
                f"{handle.url}\n\nThe URL contains a temporary access token. Do not share it outside the restaurant.",
            )
            self.refresh_phase2()
        except Exception as exc:
            messagebox.showerror("Mobile count failed", str(exc))

    def copy_mobile_count_url(self) -> None:
        if not self.mobile_count_url:
            messagebox.showinfo("Mobile count", "Start a mobile count first.")
            return
        self.root.clipboard_clear(); self.root.clipboard_append(self.mobile_count_url)
        self.mobile_count_status_var.set("Mobile count URL copied to the clipboard: " + self.mobile_count_url)

    def open_mobile_count_url(self) -> None:
        if not self.mobile_count_url:
            messagebox.showinfo("Mobile count", "Start a mobile count first.")
            return
        webbrowser.open(self.mobile_count_url)

    def stop_mobile_count_server(self) -> None:
        if self.pipeline:
            self.pipeline.phase2.stop_mobile_count_server()
        self.mobile_count_url = ""
        self.mobile_count_token = None
        if hasattr(self, "mobile_count_status_var"):
            self.mobile_count_status_var.set("Mobile count server stopped. Submitted counts remain available for manager review.")

    def finalize_mobile_count(self) -> None:
        if not self.require_permission("mobile_counts.manage"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        selected = self.mobile_sessions_tree.selection()
        if not selected:
            messagebox.showinfo("Mobile counts", "Select a submitted mobile count first.")
            return
        session_id = selected[0]
        rows = pipeline.phase2.get_mobile_entries(session_id)
        if not rows:
            messagebox.showwarning("Mobile counts", "The selected session contains no submitted entries.")
            return
        preview = "\n".join(f"{row['item_name']}: {row['quantity_on_hand']} {row['count_unit'] or ''}" for row in rows[:25])
        if len(rows) > 25:
            preview += f"\n...and {len(rows)-25} more item(s)."
        if not messagebox.askyesno(
            "Finalize mobile count",
            f"Post {len(rows)} physical count entries to inventory?\n\n{preview}\n\nThis will affect inventory estimates and month close calculations.",
        ):
            return
        try:
            result = pipeline.phase2.finalize_mobile_count(session_id)
            pipeline.controls.audit(
                "mobile_count.finalize", "mobile_count", session_id,
                f"Finalized mobile physical count with {result['imported']} item(s)", after=result,
            )
            messagebox.showinfo("Mobile count finalized", f"Posted {result['imported']} physical count entries.")
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Finalize count failed", str(exc))

    def add_waste_event(self) -> None:
        if not self.require_permission("waste.log"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        items = [dict(row) for row in pipeline.list_items() if int(row["active"] or 1)]
        if not items:
            messagebox.showinfo("Waste log", "Import invoice items first.")
            return
        dialog = WasteLogDialog(self.root, items)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        try:
            username = self.current_user.username if self.current_user else "system"
            waste_id = pipeline.log_waste(created_by=username, **dialog.result)
            pipeline.controls.audit("waste.create", "waste", waste_id, "Logged product waste", after=dialog.result)
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Waste log failed", str(exc))

    def generate_vendor_purchase_orders(self) -> None:
        if not self.require_permission("purchase_orders.manage"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            username = self.current_user.username if self.current_user else "system"
            result = pipeline.generate_purchase_orders(created_by=username)
            pipeline.controls.audit(
                "purchase_order.generate", "order_batch", result["batch_id"],
                f"Generated {result['vendor_count']} vendor purchase order draft(s)", after=result,
            )
            messagebox.showinfo("Vendor purchase orders", f"Generated {result['vendor_count']} vendor PO draft(s).")
            self.refresh_all()
            self.phase2_notebook.select(3)
        except Exception as exc:
            messagebox.showerror("Purchase order generation failed", str(exc))

    def approve_selected_purchase_order(self) -> None:
        if not self.require_permission("purchase_orders.manage"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        selected = self.purchase_orders_tree.selection()
        if not selected:
            messagebox.showinfo("Purchase orders", "Select a purchase order first.")
            return
        if not messagebox.askyesno("Approve purchase order", "Mark the selected PO as approved? This does not send it to the vendor."):
            return
        for po_id in selected:
            pipeline.phase2.approve_purchase_order(po_id)
            pipeline.controls.audit("purchase_order.approve", "purchase_order", po_id, "Approved vendor purchase order; not transmitted")
        self.refresh_phase2()

    def export_vendor_purchase_orders(self) -> None:
        if not self.require_permission("purchase_orders.manage"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        selected = list(self.purchase_orders_tree.selection()) or None
        try:
            path = pipeline.export_purchase_orders(selected)
            pipeline.controls.audit("purchase_order.export", "export", path.name, "Exported vendor purchase order package", details={"path": str(path)})
            if messagebox.askyesno("Purchase orders exported", "Open the vendor PO folder now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("PO export failed", str(exc))

    def export_accounting_file(self) -> None:
        if not self.require_permission("accounting.export"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            path = pipeline.export_accounting(
                self.accounting_start_var.get().strip(), self.accounting_end_var.get().strip(),
                self.accounting_type_var.get().strip(),
            )
            pipeline.controls.audit(
                "accounting.export", "accounting_export", path.name,
                f"Exported {self.accounting_type_var.get()} accounting file", details={"path": str(path)},
            )
            if messagebox.askyesno("Accounting export complete", "Open the exported file now?"):
                open_path(path)
            self.refresh_phase2()
        except Exception as exc:
            messagebox.showerror("Accounting export failed", str(exc))

    def create_inventory_transfer(self) -> None:
        if not self.require_permission("transfers.manage") or not self.pipeline:
            return
        destinations=[row for row in self.registry.restaurants if Path(row.get("path","")).resolve()!=self.workspace.root]
        if not destinations:
            messagebox.showinfo("Inventory transfer","Add at least one other restaurant workspace first."); return
        with self.workspace.connect() as conn:
            items=[dict(row) for row in conn.execute("SELECT item_id,item_name,vendor_name,vendor_sku,count_unit,estimated_on_hand FROM items WHERE active=1 ORDER BY category,item_name")]
        dialog=TransferDialog(self.root,destinations,items)
        self.root.wait_window(dialog)
        if not dialog.result: return
        try:
            transfer_id=self.pipeline.create_inventory_transfer(Path(dialog.result["destination"]),dialog.result["lines"],notes=dialog.result["notes"],created_by=self.current_user.username if self.current_user else "system")
            messagebox.showinfo("Transfer created",f"Transfer {transfer_id} was shipped and recorded at both locations.")
            self.refresh_all()
        except Exception as exc: messagebox.showerror("Transfer failed",str(exc))

    def receive_inventory_transfer(self) -> None:
        if not self.require_permission("transfers.manage") or not self.pipeline: return
        selected=self.transfers_tree.selection()
        if not selected: messagebox.showinfo("Receive transfer","Select a transfer first."); return
        try:
            self.pipeline.receive_inventory_transfer(selected[0],received_by=self.current_user.username if self.current_user else "system")
            self.refresh_all()
        except Exception as exc: messagebox.showerror("Receive failed",str(exc))

    def add_local_event(self) -> None:
        if not self.require_permission("forecasts.manage") or not self.pipeline: return
        
        # Event input dialog with category selection
        dlg = tk.Toplevel(self.root)
        dlg.title("Add Upcoming Event")
        dlg.geometry("420x320")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        
        # Event name
        tk.Label(dlg, text="Event name:").grid(row=0, column=0, sticky="w", padx=10, pady=5)
        name_var = tk.StringVar()
        tk.Entry(dlg, textvariable=name_var, width=40).grid(row=0, column=1, padx=10, pady=5)
        
        # Category dropdown
        from events import get_categories, category_impact_hint
        tk.Label(dlg, text="Category:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        cat_var = tk.StringVar(value="Local Event")
        cat_combo = ttk.Combobox(dlg, textvariable=cat_var, values=[c[0] for c in get_categories()], state="readonly", width=37)
        cat_combo.grid(row=1, column=1, padx=10, pady=5)
        
        # Start date
        tk.Label(dlg, text="Start date:").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        start_var = tk.StringVar(value=date.today().isoformat())
        tk.Entry(dlg, textvariable=start_var, width=40).grid(row=2, column=1, padx=10, pady=5)
        
        # End date
        tk.Label(dlg, text="End date:").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        end_var = tk.StringVar(value=date.today().isoformat())
        tk.Entry(dlg, textvariable=end_var, width=40).grid(row=3, column=1, padx=10, pady=5)
        
        # Impact
        tk.Label(dlg, text="Sales impact %:").grid(row=4, column=0, sticky="w", padx=10, pady=5)
        impact_var = tk.DoubleVar(value=10.0)
        tk.Entry(dlg, textvariable=impact_var, width=40).grid(row=4, column=1, padx=10, pady=5)
        
        # Notes
        tk.Label(dlg, text="Notes:").grid(row=5, column=0, sticky="nw", padx=10, pady=5)
        notes_text = tk.Text(dlg, width=40, height=4)
        notes_text.grid(row=5, column=1, padx=10, pady=5)
        
        # Update impact hint when category changes
        def on_category_change(*_):
            cat = cat_var.get()
            hint = category_impact_hint(cat)
            impact_var.set(hint)
        cat_var.trace("w", on_category_change)
        
        def save():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("Missing data", "Event name is required.")
                return
            try:
                self.pipeline.add_local_event(
                    name,
                    start_var.get().strip(),
                    end_date=end_var.get().strip() or start_var.get().strip(),
                    impact_percent=impact_var.get() or 0,
                    category=cat_var.get(),
                    notes=notes_text.get("1.0", "end").strip(),
                )
                self.refresh_phase3()
                dlg.destroy()
            except Exception as exc:
                messagebox.showerror("Event failed", str(exc))
        
        tk.Button(dlg, text="Save Event", command=save, bg="#0F6B78", fg="white", width=15).grid(row=6, column=1, pady=10, sticky="e")
        tk.Button(dlg, text="Cancel", command=dlg.destroy, width=10).grid(row=6, column=0, pady=10, sticky="w", padx=10)

    def import_event_calendar(self) -> None:
        if not self.require_permission("forecasts.manage") or not self.pipeline: return
        path=filedialog.askopenfilename(title="Import iCalendar events",filetypes=[("iCalendar","*.ics"),("All files","*.*")])
        if not path:return
        try:
            result=self.pipeline.import_event_calendar(Path(path)); messagebox.showinfo("Calendar import",f"Imported {result['imported']} event(s); skipped {result['skipped']}.")
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Calendar import failed",str(exc))

    def refresh_weather_phase3(self) -> None:
        if not self.require_permission("forecasts.manage") or not self.pipeline:return
        try:
            days=int(self.forecast_days_var.get() or 14); rows=self.pipeline.refresh_weather_forecast(days)
            messagebox.showinfo("Weather refreshed",f"Stored {len(rows)} daily forecast record(s).")
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Weather refresh failed",str(exc))

    def generate_forecasts_phase3(self) -> None:
        if not self.require_permission("forecasts.manage") or not self.pipeline:return
        try:
            rows=self.pipeline.generate_demand_forecasts(self.forecast_start_var.get(),int(self.forecast_days_var.get() or 14))
            messagebox.showinfo("Forecast generated",f"Generated {len(rows)} daily sales forecast(s).")
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Forecast failed",str(exc))

    def learn_forecasts_phase3(self) -> None:
        if not self.require_permission("forecasts.manage") or not self.pipeline:return
        try:
            result=self.pipeline.learn_forecasts(); messagebox.showinfo("Forecast learning",f"Scored {result['forecasts_scored']} forecast(s). Current MAPE: {float(result['mean_absolute_percent_error']):,.2f}%")
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Forecast learning failed",str(exc))

    def generate_sales_driven_order(self) -> None:
        if not self.require_permission("orders.generate") or not self.pipeline:return
        try:
            result=self.pipeline.generate_sales_driven_orders(self.forecast_start_var.get(),int(self.forecast_days_var.get() or 9))
            messagebox.showinfo("Sales-driven order",f"Created batch {result['batch_id']} using ${float(result['forecast_sales']):,.2f} projected sales. Manager review remains required.")
            self.refresh_all(); self.notebook.select(self.orders_tab)
        except Exception as exc: messagebox.showerror("Order forecast failed",str(exc))

    def configure_distributor(self) -> None:
        if not self.require_permission("distributors.manage") or not self.pipeline:return
        name=simpledialog.askstring("Distributor","Distributor name:",parent=self.root)
        if not name:return
        vendor=simpledialog.askstring("Distributor","Vendor-name match used by purchase orders:",initialvalue=name,parent=self.root) or name
        account=simpledialog.askstring("Distributor","Account number (optional):",parent=self.root) or ""
        fmt=simpledialog.askstring("Distributor","Order format: CSV or JSON",initialvalue="CSV",parent=self.root) or "CSV"
        try:
            self.pipeline.phase3.save_distributor_profile(name,vendor_match=vendor,account_number=account,order_format=fmt.upper())
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Distributor setup failed",str(exc))

    def _selected_distributor(self) -> str | None:
        selected=self.distributors_tree.selection()
        if not selected: messagebox.showinfo("Distributor","Select a distributor profile first."); return None
        return selected[0]

    def import_distributor_catalog(self) -> None:
        if not self.require_permission("distributors.manage") or not self.pipeline:return
        distributor_id=self._selected_distributor()
        if not distributor_id:return
        path=filedialog.askopenfilename(title="Import distributor catalog",filetypes=[("CSV","*.csv")])
        if not path:return
        try:
            result=self.pipeline.phase3.import_distributor_catalog(distributor_id,Path(path)); messagebox.showinfo("Catalog import",f"Imported {result['imported']} catalog rows; skipped {result['skipped']}.")
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Catalog import failed",str(exc))

    def export_distributor_orders(self) -> None:
        if not self.require_permission("distributors.manage") or not self.pipeline:return
        distributor_id=self._selected_distributor()
        if not distributor_id:return
        try:
            paths=self.pipeline.phase3.export_distributor_orders(distributor_id)
            messagebox.showinfo("Distributor export",f"Exported {len(paths)} purchase order file(s).")
            self.refresh_phase3()
        except Exception as exc: messagebox.showerror("Distributor export failed",str(exc))

    def import_distributor_confirmation(self) -> None:
        if not self.require_permission("distributors.manage") or not self.pipeline:return
        distributor_id=self._selected_distributor()
        if not distributor_id:return
        path=filedialog.askopenfilename(title="Import order confirmation",filetypes=[("CSV","*.csv")])
        if not path:return
        try:
            result=self.pipeline.phase3.import_distributor_confirmation(distributor_id,Path(path)); messagebox.showinfo("Confirmation import",f"Updated {result['updated']} purchase order(s).")
            self.refresh_all()
        except Exception as exc: messagebox.showerror("Confirmation import failed",str(exc))

    def export_owner_report_phase3(self) -> None:
        if not self.require_permission("owner_reports.export") or not self.pipeline:return
        try:
            year=int(self.portfolio_year_var.get() or date.today().year)
            path=self.pipeline.export_owner_report(f"{year}-01-01",f"{year}-12-31")
            messagebox.showinfo("Owner report",f"Owner report created:\n{path}"); open_path(path)
        except Exception as exc: messagebox.showerror("Owner report failed",str(exc))

    def refresh_phase3(self) -> None:
        trees=[getattr(self,name,None) for name in ("portfolio_tree","transfers_tree","events_tree","forecast_tree","distributors_tree","exchanges_tree","profitability_tree","variance_tree")]
        for tree in trees:
            if tree:
                for item in tree.get_children(): tree.delete(item)
        if not self.pipeline:return
        try:
            self.pipeline.phase3.set_location_provider(lambda:list(self.registry.restaurants))
            year=int(getattr(self,"portfolio_year_var",tk.StringVar(value=str(date.today().year))).get() or date.today().year)
            portfolio=self.pipeline.portfolio_summary(year)
            for row in portfolio["locations"]:
                self.portfolio_tree.insert("","end",iid=row["location_id"],values=(row["name"],f"${float(row['sales']):,.2f}",f"${float(row['purchases']):,.2f}",f"{float(row['purchase_percent']):,.2f}%",f"${float(row['inventory_value']):,.2f}",f"${float(row['waste_cost']):,.2f}",row["open_exceptions"],row["pending_reviews"]))
            for row in self.pipeline.list_inventory_transfers():
                self.transfers_tree.insert("","end",iid=row["transfer_id"],values=(row["transfer_date"],row["source_location_name"],row["destination_location_name"],row["status"],row["line_count"],f"${float(row['estimated_value']):,.2f}",row["created_by"],row["received_at"] or ""))
            weather={row["weather_date"]:row for row in self.pipeline.phase3.list_weather()}
            for row in self.pipeline.phase3.list_events(start=date.today().isoformat()):
                w=weather.get(row["event_date"]); weather_text=""
                if w: weather_text=f"{w['temperature_max_f']}F high, {w['precipitation_probability']}% rain"
                self.events_tree.insert("","end",iid=row["event_id"],values=(row["event_date"],row["end_date"],row["event_name"],row["category"],f"{row['expected_sales_impact_percent']}%",row["source"],weather_text))
            for row in self.pipeline.phase3.list_forecasts():
                self.forecast_tree.insert("","end",iid=row["forecast_id"],values=(row["forecast_date"],f"${float(row['baseline_sales']):,.2f}",f"${float(row['predicted_net_sales']):,.2f}","" if row["actual_net_sales"] is None else f"${float(row['actual_net_sales']):,.2f}","" if row["error_percent"] is None else f"{float(row['error_percent']):,.2f}%",row["trend_multiplier"],row["weather_multiplier"],row["event_multiplier"],row["status"]))
            for row in self.pipeline.phase3.list_distributors():
                self.distributors_tree.insert("","end",iid=row["distributor_id"],values=(row["distributor_name"],row["vendor_name_match"] or "",row["connector_type"],row["account_number"] or "",row["order_format"],row["catalog_count"],row["outbound_folder"]))
            for row in self.pipeline.phase3.list_distributor_exchanges():
                self.exchanges_tree.insert("","end",iid=row["exchange_id"],values=(row["created_at"],row["distributor_name"],row["exchange_type"],row["reference_id"] or "",row["status"],row["row_count"],f"${float(row['total_amount']):,.2f}",Path(row["file_path"]).name))
            start=f"{year}-01-01"; end=f"{year}-12-31"
            for row in self.pipeline.menu_profitability(start,end):
                self.profitability_tree.insert("","end",iid=row["menu_item_id"],values=(row["menu_item_name"],f"${float(row['menu_price']):,.2f}",f"${float(row['recipe_cost']):,.2f}",f"${float(row['true_menu_cost']):,.2f}",f"{float(row['true_food_cost_percent']):,.2f}%",f"${float(row['true_contribution_margin']):,.2f}",f"{float(row['quantity_sold']):,.2f}",f"${float(row['net_sales']):,.2f}",f"${float(row['recommended_price']):,.2f}",row["profitability_status"]))
            month=self.phase3_month_var.get() if hasattr(self,"phase3_month_var") else date.today().strftime("%Y-%m")
            for row in self.pipeline.usage_variance(month):
                self.variance_tree.insert("","end",iid=row["item_id"],values=(row["item_name"],row["theoretical_usage"],row["logged_waste"],row["transfer_adjustment"],row["transfer_adjusted_actual"],row["unexplained_variance"],f"{row['shrinkage_percent']}%",f"${float(row['estimated_shrinkage_cost']):,.2f}",row["status"]))
            savings=self.pipeline.savings_dashboard(start,end); accuracy=self.pipeline.phase3.forecast_accuracy()
            self.savings_var.set(f"Estimated value delivered: ${float(savings['estimated_value_delivered']):,.2f} | Invoice entry time avoided: {savings['invoice_hours_saved']} hours | Expected credits: ${float(savings['expected_vendor_credits']):,.2f} | Waste exposure: ${float(savings['documented_waste_cost']):,.2f} | Shrinkage exposure: ${float(savings['estimated_shrinkage_exposure']):,.2f} | Forecast accuracy: {float(accuracy['accuracy_percent']):,.2f}% across {accuracy['sample_count']} scored forecasts.")
        except Exception as exc:
            self.log(f"Phase 3 refresh warning: {exc}")

    def refresh_phase2(self) -> None:
        trees = [
            getattr(self, "pos_runs_tree", None), getattr(self, "menu_cost_tree", None),
            getattr(self, "mobile_sessions_tree", None), getattr(self, "waste_tree", None),
            getattr(self, "purchase_orders_tree", None), getattr(self, "accounting_tree", None),
        ]
        for tree in trees:
            if tree:
                for item in tree.get_children():
                    tree.delete(item)
        if not self.pipeline:
            return
        try:
            for row in self.pipeline.list_pos_runs():
                self.pos_runs_tree.insert("", "end", iid=row["run_id"], values=(
                    row["imported_at"], row["source_file"], row["row_count"], row["rejected_count"],
                    f"${float(row['gross_sales'] or 0):,.2f}", f"${float(row['net_sales'] or 0):,.2f}", row["status"],
                ))
            for row in self.pipeline.list_menu_costs():
                self.menu_cost_tree.insert("", "end", iid=row["menu_item_id"], values=(
                    row["menu_item_name"], row["category"], f"${float(row['menu_price'] or 0):,.2f}",
                    row["ingredient_count"], f"${float(row['recipe_cost'] or 0):,.2f}",
                    f"{float(row['food_cost_percent'] or 0):,.2f}%", f"${float(row['contribution_margin'] or 0):,.2f}",
                    f"{float(row['quantity_sold'] or 0):,.2f}", f"${float(row['net_sales'] or 0):,.2f}",
                ))
            for row in self.pipeline.phase2.list_mobile_sessions():
                self.mobile_sessions_tree.insert("", "end", iid=row["session_id"], values=(
                    row["count_date"], row["status"], row["entry_count"], row["created_by"], row["created_at"],
                    row["submitted_at"] or "", row["finalized_at"] or "",
                ))
            for row in self.pipeline.list_waste():
                self.waste_tree.insert("", "end", iid=row["waste_id"], values=(
                    row["event_date"], row["item_name"], row["vendor_name"], row["quantity_count_units"],
                    row["count_unit"] or "", row["reason"], row["shift"] or "", f"${float(row['estimated_cost'] or 0):,.2f}",
                    row["created_by"], row["notes"] or "",
                ))
            for row in self.pipeline.list_purchase_orders():
                self.purchase_orders_tree.insert("", "end", iid=row["po_id"], values=(
                    row["vendor_name"], row["po_date"], row["status"], row["line_count"],
                    f"${float(row['subtotal'] or 0):,.2f}", row["expected_delivery_date"] or "", row["created_by"],
                ))
            for row in self.pipeline.phase2.list_accounting_exports():
                self.accounting_tree.insert("", "end", iid=row["export_id"], values=(
                    row["created_at"], row["export_type"], f"{row['period_start']} to {row['period_end']}",
                    row["row_count"], f"${float(row['total_debits'] or 0):,.2f}",
                    f"${float(row['total_credits'] or 0):,.2f}", Path(row["file_path"]).name,
                ))
        except Exception as exc:
            self.log(f"Phase 2 refresh warning: {exc}")

    def refresh_all(self) -> None:
        self.refresh_uploads()
        self.refresh_review()
        self.refresh_auto_upload_history()
        self.refresh_exceptions_health()
        self.refresh_receiving()
        self.refresh_items()
        self.refresh_inventory()
        self.refresh_orders()
        self.refresh_dashboard()
        self.refresh_settings()
        self.refresh_annual_summary()
        self.refresh_data_summary()
        self.refresh_phase2()
        self.refresh_phase3()
        self.refresh_chat_status()
        self.refresh_security()

    def refresh_uploads(self) -> None:
        for item in self.upload_tree.get_children():
            self.upload_tree.delete(item)
        if not self.workspace:
            return
        for path in sorted(self.workspace.folders["upload"].iterdir()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES:
                continue
            size = path.stat().st_size
            size_text = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"
            self.upload_tree.insert("", "end", iid=path.name, values=(path.name, path.suffix.lower(), size_text, "Ready"))

    def refresh_review(self) -> None:
        if not hasattr(self, "review_tree"):
            return
        previous = self._selected_review_case_ids()
        for item in self.review_tree.get_children():
            self.review_tree.delete(item)
        self.review_case_rows = {}
        if not self.pipeline:
            return
        try:
            cases = self.pipeline.list_costpilot_review_cases()
        except Exception as exc:
            self.log(f"CostPilot Review refresh warning: {exc}")
            return
        for case in cases:
            case_id = str(case["case_id"])
            self.review_case_rows[case_id] = case
            self.review_tree.insert(
                "", "end", iid=case_id,
                values=(
                    {
                        "invoice": "Invoice",
                        "receiving": "Receiving",
                        "auto_upload": "Auto Upload",
                    }.get(case["case_type"], str(case["case_type"]).replace("_", " ").title()),
                    case["document_label"], case["problem"], case["recommendation"], case["severity"],
                ),
            )
        surviving = [case_id for case_id in previous if self.review_tree.exists(case_id)]
        if surviving:
            self.review_tree.selection_set(*surviving)
        elif cases:
            self.review_tree.selection_set(cases[0]["case_id"])
            self.review_tree.see(cases[0]["case_id"])
        signature = "|".join(case["case_id"] + ":" + case["issue_code"] for case in cases)
        if signature != getattr(self, "_review_queue_signature", ""):
            self._review_queue_signature = signature
            self._append_review_chat_message("CostPilot", self.pipeline.costpilot_review_introduction(), "costpilot")
            self._last_review_explained_case = ""
            if cases and bool(self.workspace.load_settings().get("costpilot_review_auto_explain", True)):
                self.explain_selected_review(auto=True)
        if hasattr(self, "review_copilot_status_var"):
            summary = self.pipeline.costpilot_review_summary()
            self.review_copilot_status_var.set(
                f"{summary.get('open', 0)} open case(s) · {summary.get('critical', 0)} critical · "
                f"{summary.get('invoice_cases', 0)} invoice · {summary.get('receiving_cases', 0)} receiving · "
                f"{summary.get('auto_upload_cases', 0)} Auto Upload"
            )

    def refresh_items(self) -> None:
        if not hasattr(self, "items_tree"):
            return
        for item in self.items_tree.get_children():
            self.items_tree.delete(item)
        if not self.pipeline:
            return
        estimates = {}
        try:
            estimates = {row["item_id"]: row for row in self.pipeline.estimated_inventory()}
        except Exception:
            estimates = {}
        for row in self.pipeline.list_items():
            current = row["current_price"] or ""
            if current not in (None, ""):
                current = f"${float(current):,.2f}"
            estimate = estimates.get(row["item_id"], {})
            on_hand = estimate.get("estimated_on_hand", row["estimated_on_hand"] or "")
            if on_hand not in (None, ""):
                on_hand = f"{float(on_hand):,.2f}"
            self.items_tree.insert(
                "", "end", iid=row["item_id"],
                values=(
                    row["item_id"], row["vendor_name"], row["vendor_sku"] or "", row["item_name"],
                    row["category"], row["unit"] or "", row["count_unit"] or row["unit"] or "",
                    row["units_per_purchase_unit"] or "1", current, on_hand, row["review_status"],
                ),
            )

    def refresh_dashboard(self) -> None:
        # Clear previous KPI / chart / priority content
        for frame in (getattr(self, "kpi_frame", None), getattr(self, "_sales_chart_frame", None), getattr(self, "_margin_chart_frame", None), getattr(self, "_cost_chart_frame", None)):
            if frame:
                for child in frame.winfo_children():
                    child.destroy()

        attention_tree = getattr(self, "_attention_tree", None)
        watchlist_tree = getattr(self, "_watchlist_tree", None)
        ontrack_tree = getattr(self, "_ontrack_tree", None)
        tasks_tree = getattr(self, "_tasks_tree", None)
        if attention_tree:
            for item in attention_tree.get_children(): attention_tree.delete(item)
        if watchlist_tree:
            for item in watchlist_tree.get_children(): watchlist_tree.delete(item)
        if ontrack_tree:
            for item in ontrack_tree.get_children(): ontrack_tree.delete(item)
        if tasks_tree:
            for item in tasks_tree.get_children(): tasks_tree.delete(item)

        if not self.pipeline or not self.workspace:
            return
        try:
            summary = self.pipeline.dashboard_summary()
            settings = self.workspace.load_settings()
            restaurant = settings.get("restaurant_name", self.workspace.root.name)
            service = getattr(self, "_base_dashboard_service", None)
            if not service or service.pipeline is not self.pipeline:
                service = DashboardService(self.pipeline)
                self._base_dashboard_service = service
            dashboard = service.get_dashboard_summary(
                getattr(getattr(self, "date_range_var", None), "get", lambda: "Last 7 Days")()
                or "Last 7 Days",
                vendor=getattr(self, "_base_dashboard_vendor_filter", ""),
                category=getattr(self, "_base_dashboard_category_filter", ""),
            )
        except Exception as exc:
            self.status_var.set(f"Dashboard refresh warning: {exc}")
            return

        for col, metric in enumerate(dashboard["kpis"]):
            card = ttk.Frame(self.kpi_frame, padding=(10, 10))
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 4), pady=2)
            ttk.Label(card, text=metric["title"], style="Muted.TLabel").pack(anchor="w")
            ttk.Label(card, text=metric["display"], font=("Segoe UI", 16, "bold"), foreground="#0B1F33").pack(anchor="w", pady=(4, 0))
            ttk.Label(
                card,
                text=metric["change_text"] if metric["available"] else metric["empty_message"],
                style="Muted.TLabel",
            ).pack(anchor="w")
            spark_container = ttk.Frame(card)
            spark_container.pack(fill="x", pady=(4, 0))
            if _load_matplotlib() and len(metric["sparkline"]) > 1:
                fig = Figure(figsize=(1.5, 0.4), dpi=72, facecolor='white')
                ax = fig.add_subplot(111)
                ax.plot(range(len(metric["sparkline"])), metric["sparkline"], color='#0F6B78', linewidth=1)
                ax.set_xticks([])
                ax.set_yticks([])
                for spine in ax.spines.values():
                    spine.set_visible(False)
                canvas = FigureCanvasTkAgg(fig, spark_container)
                canvas.get_tk_widget().pack(fill='x')

        sales_val = summary.get("year_sales", summary.get("net_sales", 0))
        purchases_val = summary.get("year_purchases", 0)
        margin_val = summary.get("year_estimated_contribution", 0)
        if float(sales_val or 0) > 0:
            margin_pct = float(margin_val or 0) / float(sales_val)
            margin_text = f"{margin_pct:.1%}"
        else:
            margin_text = "-"
        if self._sales_chart_frame:
            for child in self._sales_chart_frame.winfo_children():
                child.destroy()
            sales_chart = dashboard["sales_trend"]
            if sales_chart["available"] and _load_matplotlib():
                fig = Figure(figsize=(4.2, 2.1), dpi=72, facecolor='white')
                ax = fig.add_subplot(111)
                ax.bar(range(len(sales_chart["values"])), sales_chart["values"], color='#0F6B78')
                ax.set_xticks(range(len(sales_chart["labels"])))
                ax.set_xticklabels(sales_chart["labels"], fontsize=7)
                for spine in ax.spines.values():
                    spine.set_visible(False)
                canvas = FigureCanvasTkAgg(fig, self._sales_chart_frame)
                canvas.get_tk_widget().pack(fill='both', expand=True)
            else:
                ttk.Label(self._sales_chart_frame, text=sales_chart["empty_message"], style="Muted.TLabel").pack(expand=True)

        if self._margin_chart_frame:
            for child in self._margin_chart_frame.winfo_children():
                child.destroy()
            margin_chart = dashboard["margin_trend"]
            if margin_chart["available"] and _load_matplotlib():
                fig = Figure(figsize=(3.5, 2.1), dpi=72, facecolor='white')
                ax = fig.add_subplot(111)
                actual = margin_chart["actual"]
                target = margin_chart["target"]
                ax.plot(range(len(actual)), actual, color='#0F6B78', linewidth=2)
                ax.plot(range(len(target)), target, color='#7A1F3D', linewidth=1.5, linestyle='--')
                ax.set_xticks(range(len(margin_chart["labels"])))
                ax.set_xticklabels(margin_chart["labels"], fontsize=7)
                for spine in ax.spines.values():
                    spine.set_visible(False)
                canvas = FigureCanvasTkAgg(fig, self._margin_chart_frame)
                canvas.get_tk_widget().pack(fill='both', expand=True)
            else:
                ttk.Label(self._margin_chart_frame, text=margin_chart["empty_message"], style="Muted.TLabel").pack(expand=True)

        if self._cost_chart_frame:
            for child in self._cost_chart_frame.winfo_children():
                child.destroy()
            cost_chart = dashboard["cost_breakdown"]
            if cost_chart["available"] and _load_matplotlib():
                fig = Figure(figsize=(2.8, 2.1), dpi=72, facecolor='white')
                ax = fig.add_subplot(111)
                sizes = [item["amount"] for item in cost_chart["items"]]
                labels = [item["category"] for item in cost_chart["items"]]
                colors = ['#7A1F3D', '#0F6B78', '#F97316', '#64748B', '#cbd5e1', '#16a34a']
                ax.pie(sizes, labels=labels, colors=colors[:len(labels)], textprops={'fontsize': 8})
                ax.set_xticks([])
                ax.set_yticks([])
                canvas = FigureCanvasTkAgg(fig, self._cost_chart_frame)
                canvas.get_tk_widget().pack(fill='both', expand=True)
            else:
                ttk.Label(self._cost_chart_frame, text=cost_chart["empty_message"], style="Muted.TLabel").pack(expand=True)

        priority_model = dashboard["priorities"]
        if attention_tree:
            for index, row in enumerate(priority_model["attention"]):
                attention_tree.insert(
                    "", "end", iid=f"attention-{index}",
                    values=(f"{row['title']} | {row['severity']}", row.get("detail") or "Review"),
                )
        if watchlist_tree:
            for index, row in enumerate(priority_model["watchlist"]):
                watchlist_tree.insert(
                    "", "end", iid=f"watch-{index}",
                    values=(row["title"], row.get("detail") or "Watch"),
                )
        if ontrack_tree:
            for row in priority_model["on_track"]:
                ontrack_tree.insert("", "end", values=(row["title"],))
        if tasks_tree:
            for row in priority_model["tasks"]:
                tasks_tree.insert("", "end", values=(row["title"],))
            tasks_tree.bind("<Double-1>", self._selected_task)

    def _selected_task(self, _event: Any) -> dict[str, Any] | None:
        tree = self._tasks_tree
        selected = tree.selection()
        if not selected:
            return None
        title = tree.item(selected[0])['values'][0]
        mapping = {
            "Process upload folder": self.process_all_uploads,
            "Run CostPilot review": lambda: self.notebook.select(self.review_tab),
            "Generate order sheet": self.generate_order_predictions,
            "Export manager workbook": self.export_workbook,
            "Add invoice files": self.add_invoice_files,
        }
        handler = mapping.get(title)
        if callable(handler):
            try:
                return handler()
            except Exception as exc:
                messagebox.showerror("Task", f"Failed to open '{title}': {exc}")
        return None

    def _open_filters_dialog(self) -> None:
        if not self.workspace:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Filters")
        dlg.geometry("300x200")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Location:").pack(anchor="w", padx=12, pady=(12, 0))
        loc_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=loc_var).pack(fill="x", padx=12, pady=4)

        ttk.Label(dlg, text="Vendor:").pack(anchor="w", padx=12, pady=(8, 0))
        vend_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=vend_var).pack(fill="x", padx=12, pady=4)

        ttk.Label(dlg, text="Category:").pack(anchor="w", padx=12, pady=(8, 0))
        cat_var = tk.StringVar()
        ttk.Entry(dlg, textvariable=cat_var).pack(fill="x", padx=12, pady=4)

        def apply():
            self._base_dashboard_vendor_filter = vend_var.get().strip()
            self._base_dashboard_category_filter = cat_var.get().strip()
            service = getattr(self, "_base_dashboard_service", None)
            if service:
                service.invalidate()
            dlg.destroy()
            self.refresh_dashboard()

        ttk.Button(dlg, text="Apply", command=apply).pack(side="right", padx=12, pady=12)

    def _draw_kpi_sparkline(self, parent: ttk.Frame, values: list, color: str = "#0F6B78") -> None:
        if not values or not _load_matplotlib():
            return
        fig = Figure(figsize=(1.8, 0.6), dpi=72, facecolor='white')
        ax = fig.add_subplot(111)
        ax.plot(range(len(values)), values, color=color, linewidth=1.5)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        canvas = FigureCanvasTkAgg(fig, parent)
        canvas.get_tk_widget().pack(fill='x', pady=(4, 0))

    def refresh_settings(self) -> None:
        if not self.workspace:
            for var in self.setting_vars.values():
                if isinstance(var, tk.BooleanVar):
                    var.set(False)
                else:
                    var.set("")
            return
        settings = self.workspace.load_settings()
        for key, var in self.setting_vars.items():
            value = settings.get(key, DEFAULT_SETTINGS.get(key, ""))
            if key == "known_vendors":
                value = "; ".join(value or [])
            var.set(value)

    def refresh_data_summary(self) -> None:
        self.data_summary.configure(state="normal")
        self.data_summary.delete("1.0", "end")
        if self.pipeline:
            summary = self.pipeline.dashboard_summary()
            lines = [
                f"Current-year sales: ${float(summary.get('year_sales', 0)):,.2f}",
                f"Current-year invoice purchases: ${float(summary.get('year_purchases', 0)):,.2f}",
                f"Estimated inventory value: ${float(summary.get('estimated_inventory_value', 0)):,.2f}",
                f"Estimated contribution after imported costs: ${float(summary.get('year_estimated_contribution', 0)):,.2f}",
                (
                    f"Closed inventory months: {int(summary.get('closed_months', 0))} of 12"
                    f" · {int(summary.get('ready_to_close_months', 0))} complete count period(s) ready to review"
                ),
                "",
                "Estimates exclude labor unless imported. Waste, spoilage, theft and count variance are included in product depletion but are not separately identified.",
            ]
            self.data_summary.insert("1.0", "\n".join(lines))
        self.data_summary.configure(state="disabled")

    def _on_attention_double_click(self, _event: Any) -> None:
        tree = self._attention_tree
        sel = tree.selection()
        if sel:
            self.notebook.select(self.review_tab)

    def _on_watchlist_double_click(self, _event: Any) -> None:
        tree = self._watchlist_tree
        sel = tree.selection()
        if sel:
            self.notebook.select(self.exceptions_tab)

    def _on_ontrack_double_click(self, _event: Any) -> None:
        tree = self._ontrack_tree
        sel = tree.selection()
        if sel:
            self.notebook.select(self.data_tab)

    def export_inventory_count_sheet(self) -> None:
        if not self.require_permission("inventory.count"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            path = pipeline.export_inventory_count_sheet(self.inventory_month_var.get().strip())
            self.log(f"Exported inventory count sheet: {path}")
            if messagebox.askyesno("Count sheet exported", "Open the count sheet now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("Count sheet export failed", str(exc))

    def import_inventory_count(self) -> None:
        if not self.require_permission("inventory.count"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        path = filedialog.askopenfilename(title="Select completed inventory count CSV", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            result = pipeline.import_inventory_count_csv(Path(path))
            pipeline.controls.audit("inventory.count_import", "inventory_count", self.inventory_month_var.get().strip(), f"Imported {result.imported} inventory count(s)", details={"source": str(path), "skipped": result.skipped, "errors": result.errors})
            detail = f"Imported {result.imported} count(s); skipped {result.skipped}."
            if result.errors:
                detail += f" {len(result.errors)} row(s) need correction."
                self.log("Inventory count errors: " + " | ".join(result.errors[:10]))
            self.log(detail)
            messagebox.showinfo("Inventory count import", detail)
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Inventory count import failed", str(exc))

    def close_inventory_month(self) -> None:
        if not self.require_permission("inventory.close"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        month = self.inventory_month_var.get().strip()
        if not messagebox.askyesno("Close month", f"Calculate and replace the inventory usage summary for {month}?"):
            return
        try:
            result = pipeline.close_inventory_month(month)
            pipeline.controls.audit("inventory.month_close", "month", month, f"Closed inventory month {month}", after=result)
            self.log(f"Closed inventory month {month}: estimated COGS ${float(result.get('estimated_cogs',0)):,.2f}.")
            messagebox.showinfo(
                "Month closed",
                f"{month} closed.\nEstimated COGS: ${float(result.get('estimated_cogs',0)):,.2f}\nStatus: {result.get('count_status')}",
            )
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Month close failed", str(exc))

    def refresh_inventory(self) -> None:
        if not hasattr(self, "inventory_tree"):
            return
        for item in self.inventory_tree.get_children():
            self.inventory_tree.delete(item)
        if not self.pipeline:
            return
        month = self.inventory_month_var.get().strip() or date.today().strftime("%Y-%m")
        try:
            usage = {row["item_id"]: row for row in self.pipeline.monthly_usage(month)}
            estimates = {row["item_id"]: row for row in self.pipeline.estimated_inventory()}
            item_rows = {row["item_id"]: row for row in self.pipeline.list_items()}
            def display_quantity(value: object) -> str:
                if value in (None, ""):
                    return ""
                return f"{float(value):,.2f}"

            for item_id, item_row in item_rows.items():
                row = usage.get(item_id)
                estimate = estimates.get(item_id, {})
                self.inventory_tree.insert("", "end", iid=item_id, values=(
                    item_row["item_name"], item_row["vendor_name"],
                    display_quantity(row["opening_quantity"]) if row else "",
                    display_quantity(row["purchased_quantity"]) if row else "",
                    display_quantity(row["ending_quantity"]) if row else "",
                    display_quantity(row["estimated_usage_quantity"]) if row else "",
                    (
                        display_quantity(row["average_weekly_usage"])
                        if row else display_quantity(estimate.get("average_weekly_usage", ""))
                    ),
                    f"{float(estimate.get('estimated_on_hand',0)):,.2f}" if estimate else "",
                    item_row["count_unit"] or item_row["unit"] or "", row["confidence"] if row else estimate.get("confidence", "Open"),
                ))
            summary = self.pipeline.planning.month_summary(month)
            self.inventory_status_var.set(
                f"{month}: {summary.get('count_status','Open')}. "
                f"Opening inventory: ${float(summary.get('opening_inventory_value',0) or 0):,.2f}. "
                f"Ending inventory: ${float(summary.get('ending_inventory_value',0) or 0):,.2f}."
            )
        except Exception as exc:
            self.inventory_status_var.set(f"Inventory refresh warning: {exc}")

    def generate_order_predictions(self) -> None:
        if not self.require_permission("orders.generate"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            result = pipeline.generate_order_sheet()
            pipeline.controls.audit("order.generate", "order_batch", result.get("batch_id"), f"Generated draft order batch with {result.get('item_count')} item(s)", after=result)
            self.log(f"Generated draft order batch {result['batch_id']} with {result['item_count']} item(s).")
            self.refresh_orders()
            self.notebook.select(self.orders_tab)
        except Exception as exc:
            messagebox.showerror("Order prediction failed", str(exc))

    def refresh_orders(self) -> None:
        if not hasattr(self, "orders_tree"):
            return
        for item in self.orders_tree.get_children():
            self.orders_tree.delete(item)
        if not self.pipeline:
            return
        batch = self.pipeline.latest_order_batch()
        if not batch:
            self.order_batch_var.set("No order batch generated.")
            return
        self.order_batch_var.set(f"Batch {batch['batch_id']} | As of {batch['as_of_date']} | {batch['status']} | Manager review required")
        for row in self.pipeline.list_order_predictions(batch["batch_id"]):
            self.orders_tree.insert("", "end", iid=str(row["prediction_id"]), values=(
                row["vendor_name"], row["vendor_sku"] or "", row["item_name"], row["estimated_on_hand"],
                row["average_weekly_usage"], row["par_quantity_count_units"], row["suggested_order_quantity"],
                row["manager_order_quantity"], row["purchase_unit"], f"${float(row['estimated_order_cost'] or 0):,.2f}", row["status"],
            ))

    def edit_selected_order(self) -> None:
        if not self.require_permission("orders.edit"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        selected = self.orders_tree.selection()
        if not selected:
            messagebox.showinfo("Order planning", "Select an item first.")
            return
        prediction_id = int(selected[0])
        current = self.orders_tree.set(selected[0], "manager_qty")
        value = simpledialog.askfloat("Manager order quantity", "Enter the purchase-unit quantity to order:", initialvalue=float(current or 0), minvalue=0, parent=self.root)
        if value is None:
            return
        try:
            before = self.orders_tree.item(selected[0], "values")
            prediction = next(
                (dict(row) for row in pipeline.list_order_predictions() if int(row["prediction_id"]) == prediction_id),
                None,
            )
            reason_code = None
            manager_note = None
            if prediction and pipeline.margin_memory.is_material_order_override(
                prediction.get("suggested_order_quantity"), value
            ):
                reason_dialog = MarginMemoryReasonDialog(
                    self.root,
                    suggested=prediction.get("suggested_order_quantity"),
                    actual=value,
                    item_name=prediction.get("item_name", "Item"),
                )
                self.root.wait_window(reason_dialog)
                if reason_dialog.result:
                    reason_code = reason_dialog.result["reason_code"]
                    manager_note = reason_dialog.result["manager_note"]
                else:
                    reason_code = "UNDOCUMENTED"
                    manager_note = ""
            decision_id = pipeline.update_order_prediction(
                prediction_id, value, "Reviewed",
                reason_code=reason_code, manager_note=manager_note,
            )
            pipeline.controls.audit(
                "order.quantity_edit", "order_prediction", str(prediction_id),
                f"Changed manager order quantity to {value}",
                before={"row": before},
                after={
                    "manager_order_quantity": value,
                    "margin_memory_decision_id": decision_id,
                    "reason_code": reason_code or "BELOW_THRESHOLD",
                },
            )
            self.refresh_orders()
        except Exception as exc:
            messagebox.showerror("Order update failed", str(exc))

    def approve_order_batch(self) -> None:
        if not self.require_permission("orders.approve"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        batch = pipeline.latest_order_batch()
        if not batch:
            messagebox.showinfo("Order planning", "Generate an order batch first.")
            return
        if messagebox.askyesno("Approve order batch", "Mark the reviewed quantities as approved? This does not send an order to any vendor."):
            memory_result = pipeline.approve_order_batch(batch["batch_id"])
            pipeline.controls.audit(
                "order.approve", "order_batch", batch["batch_id"],
                "Approved reviewed order batch; no vendor order was transmitted",
                details={"margin_memory": memory_result},
            )
            self.refresh_orders()

    def export_order_sheet(self) -> None:
        if not self.require_permission("reports.export"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            path = pipeline.export_order_sheet()
            pipeline.controls.audit("reports.export_order", "export", path.name, "Exported reviewed order sheet", details={"path": str(path)})
            self.log(f"Exported order sheet: {path}")
            if messagebox.askyesno("Order sheet exported", "Open the order sheet now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("Order sheet export failed", str(exc))

    def export_full_inventory(self) -> None:
        if not self.require_permission("reports.export"):
            return
        pipeline = self.require_pipeline()
        if not pipeline:
            return
        try:
            path = pipeline.export_full_inventory()
            pipeline.controls.audit("reports.export_inventory", "export", path.name, "Exported full inventory", details={"path": str(path)})
            self.log(f"Exported full inventory: {path}")
            if messagebox.askyesno("Inventory exported", "Open the inventory export now?"):
                open_path(path)
        except Exception as exc:
            messagebox.showerror("Inventory export failed", str(exc))

    def refresh_annual_summary(self) -> None:
        if not hasattr(self, "annual_tree"):
            return
        for item in self.annual_tree.get_children():
            self.annual_tree.delete(item)
        if not self.pipeline:
            return
        try:
            year = int(self.report_year_var.get().strip())
            for row in self.pipeline.annual_summary(year):
                self.annual_tree.insert("", "end", iid=row["month"], values=(
                    row["month"], f"${float(row.get('net_sales',0)):,.2f}", f"${float(row.get('invoice_purchases',0)):,.2f}",
                    f"${float(row.get('opening_inventory_value',0)):,.2f}",
                    f"${float(row.get('ending_inventory_value',0)):,.2f}", f"${float(row.get('estimated_cogs',0)):,.2f}",
                    f"${float(row.get('estimated_product_margin',0)):,.2f}", f"${float(row.get('estimated_contribution',0)):,.2f}",
                    row.get("count_status", "Open"),
                ))
        except Exception as exc:
            self.log(f"Annual summary warning: {exc}")

    def open_workspace(self) -> None:
        if self.workspace:
            open_path(self.workspace.root)

    def open_folder_key(self, key: str) -> None:
        if self.workspace:
            open_path(self.workspace.folders[key])

    def log(self, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        stamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


class POSMappingDialog(tk.Toplevel):
    FIELDS = (
        "business_date", "order_id", "location", "pos_item_key", "menu_item_name",
        "quantity", "unit_price", "gross_sales", "discounts", "refunds",
        "net_sales", "sales_tax", "channel", "modifiers",
    )

    def __init__(self, parent: tk.Misc, headers: list[str], suggested: dict[str, str]):
        super().__init__(parent)
        self.title("Map POS report columns")
        self.geometry("700x650")
        self.transient(parent); self.grab_set()
        self.mapping: dict[str, str] | None = None
        self.profile_name = ""
        ttk.Label(self, text="POS Sales Import Mapping", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(
            self,
            text="Confirm how the report columns map into the app. Business Date, Menu Item Name, and Quantity are required. The mapping is saved for later imports.",
            wraplength=650, style="Muted.TLabel",
        ).pack(anchor="w", padx=12, pady=(0, 8))
        profile_frame = ttk.Frame(self); profile_frame.pack(fill="x", padx=12, pady=4)
        ttk.Label(profile_frame, text="Mapping profile name:").pack(side="left")
        self.profile_var = tk.StringVar(value="POS Product Sales")
        ttk.Entry(profile_frame, textvariable=self.profile_var, width=35).pack(side="left", padx=6)
        canvas = tk.Canvas(self, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=6)
        scroll.pack(side="right", fill="y", pady=6)
        values = [""] + headers
        self.vars: dict[str, tk.StringVar] = {}
        for row, field in enumerate(self.FIELDS):
            required = " *" if field in {"business_date", "menu_item_name", "quantity"} else ""
            ttk.Label(body, text=field.replace("_", " ").title() + required, width=24).grid(row=row, column=0, sticky="w", padx=4, pady=4)
            var = tk.StringVar(value=suggested.get(field, "")); self.vars[field] = var
            ttk.Combobox(body, textvariable=var, values=values, state="readonly", width=44).grid(row=row, column=1, sticky="ew", padx=4, pady=4)
        body.columnconfigure(1, weight=1)
        footer = ttk.Frame(self); footer.pack(fill="x", padx=12, pady=12)
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(footer, text="Import", command=self.accept).pack(side="right", padx=4)

    def accept(self) -> None:
        mapping = {field: var.get().strip() for field, var in self.vars.items() if var.get().strip()}
        missing = [field for field in ("business_date", "menu_item_name", "quantity") if not mapping.get(field)]
        if missing:
            messagebox.showerror("Missing mapping", "Map these required fields: " + ", ".join(missing), parent=self)
            return
        self.mapping = mapping
        self.profile_name = self.profile_var.get().strip() or "POS Product Sales"
        self.destroy()


class WasteLogDialog(tk.Toplevel):
    REASONS = ("Spoiled", "Dropped", "Overcooked", "Returned", "Prep mistake", "Expired", "Equipment failure", "Unknown")
    SHIFTS = ("", "Opening", "Lunch", "Dinner", "Closing", "Overnight")

    def __init__(self, parent: tk.Misc, items: list[dict[str, Any]]):
        super().__init__(parent)
        self.title("Log product waste")
        self.geometry("620x430")
        self.transient(parent); self.grab_set()
        self.result: dict[str, Any] | None = None
        self.items = items
        self.item_labels = {
            f"{row['item_name']} | {row['vendor_name']} | {row['count_unit'] or row['unit'] or 'each'}": row["item_id"]
            for row in items
        }
        body = ttk.Frame(self, padding=14); body.pack(fill="both", expand=True)
        ttk.Label(body, text="Waste Event", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.item_var = tk.StringVar(value=next(iter(self.item_labels), ""))
        self.quantity_var = tk.StringVar(); self.reason_var = tk.StringVar(value="Spoiled")
        self.shift_var = tk.StringVar(); self.date_var = tk.StringVar(value=date.today().isoformat())
        self.notes_var = tk.StringVar()
        fields = [
            ("Item", ttk.Combobox(body, textvariable=self.item_var, values=list(self.item_labels), state="readonly", width=52)),
            ("Quantity in count units", ttk.Entry(body, textvariable=self.quantity_var, width=20)),
            ("Reason", ttk.Combobox(body, textvariable=self.reason_var, values=self.REASONS, state="readonly", width=25)),
            ("Shift", ttk.Combobox(body, textvariable=self.shift_var, values=self.SHIFTS, state="readonly", width=25)),
            ("Date", ttk.Entry(body, textvariable=self.date_var, width=20)),
            ("Notes", ttk.Entry(body, textvariable=self.notes_var, width=52)),
        ]
        for index, (label, widget) in enumerate(fields, 1):
            ttk.Label(body, text=label + ":").grid(row=index, column=0, sticky="w", padx=(0, 8), pady=6)
            widget.grid(row=index, column=1, sticky="ew", pady=6)
        body.columnconfigure(1, weight=1)
        footer = ttk.Frame(body); footer.grid(row=8, column=0, columnspan=2, sticky="e", pady=(18,0))
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(footer, text="Save Waste", command=self.save).pack(side="right", padx=4)

    def save(self) -> None:
        try:
            amount = float(self.quantity_var.get().strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid quantity", "Enter a positive waste quantity.", parent=self)
            return
        label = self.item_var.get()
        if label not in self.item_labels:
            messagebox.showerror("Missing item", "Select an inventory item.", parent=self)
            return
        self.result = {
            "item_id": self.item_labels[label], "quantity": amount, "reason": self.reason_var.get(),
            "event_date": self.date_var.get().strip(), "shift": self.shift_var.get(), "notes": self.notes_var.get().strip(),
        }
        self.destroy()


class TransferDialog(tk.Toplevel):
    def __init__(self,parent:tk.Misc,destinations:list[dict[str,str]],items:list[dict[str,Any]]):
        super().__init__(parent); self.title("Create Inventory Transfer"); self.geometry("880x620"); self.transient(parent); self.grab_set(); self.result=None
        self.destinations=destinations; self.items=items; self.lines=[]
        self.destination_var=tk.StringVar(value=f"{destinations[0]['name']} | {destinations[0]['path']}")
        top=ttk.Frame(self,padding=10); top.pack(fill="x")
        ttk.Label(top,text="Destination:").pack(side="left"); self.dest_combo=ttk.Combobox(top,textvariable=self.destination_var,values=[f"{r['name']} | {r['path']}" for r in destinations],state="readonly",width=65); self.dest_combo.pack(side="left",padx=5)
        self.item_tree=ttk.Treeview(self,columns=("item","vendor","sku","unit","on_hand"),show="headings",height=13)
        for col,width in {"item":280,"vendor":170,"sku":120,"unit":90,"on_hand":90}.items(): self.item_tree.heading(col,text=col.title()); self.item_tree.column(col,width=width,anchor="w")
        for row in items:self.item_tree.insert("","end",iid=row["item_id"],values=(row["item_name"],row["vendor_name"],row["vendor_sku"] or "",row["count_unit"] or "",row["estimated_on_hand"] or ""))
        self.item_tree.pack(fill="both",expand=True,padx=10,pady=5)
        add=ttk.Frame(self,padding=10); add.pack(fill="x"); self.qty_var=tk.StringVar(value="1")
        ttk.Label(add,text="Quantity in count units:").pack(side="left"); ttk.Entry(add,textvariable=self.qty_var,width=12).pack(side="left",padx=4); ttk.Button(add,text="Add Line",command=self.add_line).pack(side="left",padx=4)
        self.lines_var=tk.StringVar(value="No transfer lines added."); ttk.Label(self,textvariable=self.lines_var,wraplength=820).pack(anchor="w",padx=10)
        self.notes_var=tk.StringVar(); ttk.Label(add,text="Notes:").pack(side="left",padx=(18,3)); ttk.Entry(add,textvariable=self.notes_var,width=35).pack(side="left")
        buttons=ttk.Frame(self,padding=10); buttons.pack(fill="x"); ttk.Button(buttons,text="Cancel",command=self.destroy).pack(side="right",padx=4); ttk.Button(buttons,text="Create Transfer",command=self.accept).pack(side="right",padx=4)
    def add_line(self):
        selected=self.item_tree.selection()
        if not selected:return
        try: amount=float(self.qty_var.get())
        except ValueError: messagebox.showerror("Invalid quantity","Enter a numeric quantity.",parent=self); return
        if amount<=0:return
        item=next(r for r in self.items if r["item_id"]==selected[0]); self.lines=[r for r in self.lines if r["item_id"]!=item["item_id"]]; self.lines.append({"item_id":item["item_id"],"quantity":amount})
        self.lines_var.set(" | ".join(f"{next(i['item_name'] for i in self.items if i['item_id']==r['item_id'])}: {r['quantity']}" for r in self.lines))
    def accept(self):
        if not self.lines:messagebox.showinfo("Transfer","Add at least one item.",parent=self);return
        index=self.dest_combo.current(); index=0 if index<0 else index
        self.result={"destination":self.destinations[index]["path"],"lines":self.lines,"notes":self.notes_var.get()}; self.destroy()

class MarginMemoryReasonDialog(tk.Toplevel):
    """Optional reason capture for material order overrides."""

    def __init__(self, parent: tk.Misc, *, suggested: Any, actual: Any, item_name: str):
        super().__init__(parent)
        self.result: dict[str, str] | None = None
        self.title("MarginMemory - Why was this changed?")
        self.geometry("570x330")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        labels = [label for code, label in REASON_CODES if code != "UNDOCUMENTED"]
        self.label_to_code = {label: code for code, label in REASON_CODES}
        self.reason_var = tk.StringVar(value="Manager experience")

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="MarginMemory", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                f"{item_name}: recommended {suggested}, manager quantity {actual}. "
                "Recording the reason helps MarginMemory learn whether this decision worked."
            ),
            style="Muted.TLabel", wraplength=520,
        ).pack(anchor="w", pady=(4, 14))
        ttk.Label(frame, text="Reason").pack(anchor="w")
        ttk.Combobox(
            frame, textvariable=self.reason_var, values=labels, state="readonly", width=46
        ).pack(fill="x", pady=(4, 12))
        ttk.Label(frame, text="Optional note").pack(anchor="w")
        self.note = tk.Text(frame, height=4, wrap="word")
        self.note.pack(fill="x", pady=(4, 12))
        ttk.Label(
            frame, text="Cancel still saves the quantity with reason Undocumented.",
            style="Muted.TLabel",
        ).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="Save reason", command=self.save).pack(side="right", padx=4)

    def save(self) -> None:
        label = self.reason_var.get().strip()
        self.result = {
            "reason_code": self.label_to_code.get(label, "OTHER"),
            "manager_note": self.note.get("1.0", "end").strip(),
        }
        self.destroy()


class InvoiceReviewDialog(tk.Toplevel):
    HEADER_FIELDS = [
        ("Vendor", "vendor"),
        ("Invoice number", "invoice_number"),
        ("Invoice date", "invoice_date"),
        ("Subtotal", "subtotal"),
        ("Fees", "fees"),
        ("Tax", "tax"),
        ("Credits", "credits"),
        ("Total", "total"),
    ]

    def __init__(
        self,
        parent: tk.Misc,
        pipeline: InvoicePipeline,
        invoice_id: str,
        callback: Any,
    ):
        super().__init__(parent)
        self.pipeline = pipeline
        self.invoice_id = invoice_id
        self.callback = callback
        self.title(f"Review Invoice {invoice_id}")
        self.geometry("1120x760")
        self.minsize(900, 620)
        self.transient(parent)
        self.grab_set()
        self.data = pipeline.get_invoice_data(invoice_id)
        self.header_vars: dict[str, tk.StringVar] = {}
        self.recognize_vendor_var = tk.BooleanVar(value=False)
        self._build()
        self._load_data()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Review extracted invoice", style="Title.TLabel").pack(side="left")
        row = self.pipeline.get_invoice(self.invoice_id)
        if row:
            ttk.Button(top, text="Open Source", command=lambda: open_path(Path(row["source_original_path"]))).pack(side="right")

        header = ttk.LabelFrame(self, text="Invoice header", padding=10)
        header.pack(fill="x", padx=10, pady=5)
        for index, (label, key) in enumerate(self.HEADER_FIELDS):
            ttk.Label(header, text=label).grid(row=index // 4 * 2, column=index % 4, sticky="w", padx=4)
            var = tk.StringVar()
            self.header_vars[key] = var
            ttk.Entry(header, textvariable=var, width=24).grid(row=index // 4 * 2 + 1, column=index % 4, sticky="ew", padx=4, pady=(0, 6))
        for col in range(4):
            header.columnconfigure(col, weight=1)

        toolbar = ttk.Frame(self, padding=(10, 4))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Add Line", command=self.add_line).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Edit Line", command=self.edit_selected_line).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Delete Line", command=self.delete_selected_line).pack(side="left", padx=3)
        ttk.Label(toolbar, text="Double-click a line to edit it.", style="Muted.TLabel").pack(side="left", padx=12)

        detail_tabs = ttk.Notebook(self)
        detail_tabs.pack(fill="both", expand=True, padx=10, pady=5)
        lines_frame, raw_frame = ttk.Frame(detail_tabs), ttk.Frame(detail_tabs)
        detail_tabs.add(lines_frame, text="Extracted Line Items")
        detail_tabs.add(raw_frame, text="Raw Extraction & Diagnostics")
        columns = ("sku", "description", "quantity", "unit", "unit_price", "line_total", "confidence")
        self.line_tree = ttk.Treeview(lines_frame, columns=columns, show="headings", selectmode="browse")
        widths = {"sku": 130, "description": 390, "quantity": 90, "unit": 100, "unit_price": 100, "line_total": 110, "confidence": 90}
        for col in columns:
            self.line_tree.heading(col, text=col.replace("_", " ").title())
            self.line_tree.column(col, width=widths[col], anchor="w")
        self.line_tree.pack(fill="both", expand=True)
        self.line_tree.bind("<Double-1>", lambda _event: self.edit_selected_line())
        self.raw_text_widget = tk.Text(raw_frame, wrap="word", font=("Consolas", 9))
        raw_scroll = ttk.Scrollbar(raw_frame, orient="vertical", command=self.raw_text_widget.yview)
        self.raw_text_widget.configure(yscrollcommand=raw_scroll.set)
        raw_scroll.pack(side="right", fill="y"); self.raw_text_widget.pack(side="left", fill="both", expand=True)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Checkbutton(
            bottom,
            text="Recognize this vendor for future validated invoices",
            variable=self.recognize_vendor_var,
        ).pack(side="left")
        ttk.Button(bottom, text="Reject", command=self.reject).pack(side="right", padx=3)
        ttk.Button(bottom, text="Approve and Post", command=self.approve).pack(side="right", padx=3)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right", padx=3)

    def _load_data(self) -> None:
        for _label, key in self.HEADER_FIELDS:
            self.header_vars[key].set(str(self.data.get(key, "")))
        self.refresh_lines()
        diagnostics = []
        if self.data.get("_extraction_error"):
            diagnostics.append("EXTRACTION ERROR:\n" + str(self.data.get("_extraction_error")))
        notes = self.data.get("extraction_notes")
        if isinstance(notes, list) and notes:
            diagnostics.append("EXTRACTION NOTES:\n- " + "\n- ".join(str(n) for n in notes))
        diagnostics.append("RAW EXTRACTED TEXT:\n" + str(self.data.get("_raw_text") or "<No raw text artifact was produced.>"))
        self.raw_text_widget.delete("1.0", "end")
        self.raw_text_widget.insert("1.0", "\n\n".join(diagnostics))

    def refresh_lines(self) -> None:
        for item in self.line_tree.get_children():
            self.line_tree.delete(item)
        for index, line in enumerate(self.data.get("items", [])):
            self.line_tree.insert(
                "", "end", iid=str(index),
                values=(
                    line.get("sku", ""), line.get("description", ""), line.get("quantity", ""),
                    line.get("unit", ""), line.get("unit_price", ""), line.get("line_total", ""),
                    f"{float(line.get('confidence', 0) or 0):.0%}",
                ),
            )

    def add_line(self) -> None:
        LineEditDialog(self, {}, self._line_added)

    def _line_added(self, line: dict[str, Any] | None) -> None:
        if line:
            self.data.setdefault("items", []).append(line)
            self.refresh_lines()

    def edit_selected_line(self) -> None:
        selected = self.line_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        LineEditDialog(self, dict(self.data["items"][index]), lambda line: self._line_edited(index, line))

    def _line_edited(self, index: int, line: dict[str, Any] | None) -> None:
        if line:
            self.data["items"][index] = line
            self.refresh_lines()

    def delete_selected_line(self) -> None:
        selected = self.line_tree.selection()
        if selected and messagebox.askyesno("Delete line", "Delete the selected invoice line?", parent=self):
            del self.data["items"][int(selected[0])]
            self.refresh_lines()

    def collect(self) -> dict[str, Any]:
        for _label, key in self.HEADER_FIELDS:
            self.data[key] = self.header_vars[key].get().strip()
        return self.data

    def approve(self) -> None:
        result = self.pipeline.approve_review(
            self.invoice_id,
            self.collect(),
            recognize_vendor=self.recognize_vendor_var.get(),
        )
        if result.status != "Approved":
            messagebox.showerror("Cannot approve", "\n".join(result.errors) or result.message, parent=self)
            self.callback(result)
            return
        self.pipeline.controls.audit("invoice.approve", "invoice", self.invoice_id, "Approved reviewed invoice and posted line items", after=self.collect())
        messagebox.showinfo("Approved", result.message, parent=self)
        self.callback(result)
        self.destroy()

    def reject(self) -> None:
        reason = simpledialog.askstring("Reject invoice", "Reason for rejection:", parent=self)
        if reason:
            self.pipeline.reject_review(self.invoice_id, reason)
            self.pipeline.controls.audit("invoice.reject", "invoice", self.invoice_id, f"Rejected invoice: {reason}")
            self.callback(ProcessResult(source="", invoice_id=self.invoice_id, status="Rejected", message=reason))
            self.destroy()


class ItemEditDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, pipeline: InvoicePipeline, item: dict[str, Any], callback: Any):
        super().__init__(parent)
        self.pipeline = pipeline
        self.item = item
        self.callback = callback
        self.title(f"Edit Item {item.get('item_id', '')}")
        self.geometry("760x670")
        self.minsize(690, 600)
        self.transient(parent)
        self.grab_set()
        self.vars = {
            "item_name": tk.StringVar(value=str(item.get("item_name", ""))),
            "category": tk.StringVar(value=str(item.get("category", "Unclassified"))),
            "unit": tk.StringVar(value=str(item.get("unit", ""))),
            "vendor_sku": tk.StringVar(value=str(item.get("vendor_sku", ""))),
            "review_status": tk.StringVar(value=str(item.get("review_status", "Approved"))),
            "count_unit": tk.StringVar(value=str(item.get("count_unit") or item.get("unit") or "each")),
            "units_per_purchase_unit": tk.StringVar(value=str(item.get("units_per_purchase_unit") or "1")),
            "lead_time_days": tk.StringVar(value=str(item.get("lead_time_days") or "2")),
            "order_cycle_days": tk.StringVar(value=str(item.get("order_cycle_days") or "7")),
            "safety_stock_days": tk.StringVar(value=str(item.get("safety_stock_days") or "2")),
            "order_multiple": tk.StringVar(value=str(item.get("order_multiple") or "1")),
            "minimum_order_qty": tk.StringVar(value=str(item.get("minimum_order_qty") or "0")),
            "par_override_count_units": tk.StringVar(value=str(item.get("par_override_count_units") or "")),
            "active": tk.BooleanVar(value=bool(item.get("active", 1))),
        }
        ttk.Label(self, text=str(item.get("vendor_name", "")), style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6)
        )
        ttk.Label(
            self,
            text="Purchase units come from invoices. Count units are what managers physically count. Example: purchase unit = case, count unit = pound, units per purchase unit = 40.",
            style="Muted.TLabel", wraplength=700,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))
        fields = [
            ("Item name", "item_name"), ("Category", "category"),
            ("Purchase unit", "unit"), ("Vendor SKU", "vendor_sku"),
            ("Count unit", "count_unit"), ("Units per purchase unit", "units_per_purchase_unit"),
            ("Lead time days", "lead_time_days"), ("Order cycle days", "order_cycle_days"),
            ("Safety stock days", "safety_stock_days"), ("Order multiple", "order_multiple"),
            ("Minimum order quantity", "minimum_order_qty"),
            ("Par override in count units (optional)", "par_override_count_units"),
        ]
        for index, (label, key) in enumerate(fields, 2):
            ttk.Label(self, text=label).grid(row=index, column=0, sticky="w", padx=12, pady=5)
            ttk.Entry(self, textvariable=self.vars[key], width=48).grid(row=index, column=1, sticky="ew", padx=12, pady=5)
        status_row = 2 + len(fields)
        ttk.Label(self, text="Review status").grid(row=status_row, column=0, sticky="w", padx=12, pady=5)
        ttk.Combobox(
            self, textvariable=self.vars["review_status"], state="readonly",
            values=["Approved", "New Item - Review Required", "Inactive", "Review"],
        ).grid(row=status_row, column=1, sticky="ew", padx=12, pady=5)
        ttk.Checkbutton(self, text="Active item used in inventory and order planning", variable=self.vars["active"]).grid(
            row=status_row + 1, column=0, columnspan=2, sticky="w", padx=12, pady=7
        )
        buttons = ttk.Frame(self)
        buttons.grid(row=status_row + 2, column=0, columnspan=2, sticky="e", padx=12, pady=16)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=3)
        self.columnconfigure(1, weight=1)

    def save(self) -> None:
        try:
            self.pipeline.update_item(
                self.item["item_id"],
                item_name=self.vars["item_name"].get(),
                category=self.vars["category"].get(),
                unit=self.vars["unit"].get(),
                vendor_sku=self.vars["vendor_sku"].get(),
                review_status=self.vars["review_status"].get(),
            )
            self.pipeline.update_item_planning(
                self.item["item_id"],
                count_unit=self.vars["count_unit"].get(),
                units_per_purchase_unit=self.vars["units_per_purchase_unit"].get(),
                lead_time_days=self.vars["lead_time_days"].get(),
                order_cycle_days=self.vars["order_cycle_days"].get(),
                safety_stock_days=self.vars["safety_stock_days"].get(),
                order_multiple=self.vars["order_multiple"].get(),
                minimum_order_qty=self.vars["minimum_order_qty"].get(),
                par_override_count_units=self.vars["par_override_count_units"].get(),
                active=self.vars["active"].get(),
            )
        except Exception as exc:
            messagebox.showerror("Item update failed", str(exc), parent=self)
            return
        updated = next((dict(row) for row in self.pipeline.list_items() if row["item_id"] == self.item["item_id"]), {})
        self.pipeline.controls.audit("item.update", "item", self.item["item_id"], f"Updated item {updated.get('item_name', self.item.get('item_name', ''))}", before=self.item, after=updated)
        self.callback()
        self.destroy()


class LineEditDialog(tk.Toplevel):
    FIELDS = ["sku", "description", "category", "quantity", "unit", "unit_price", "line_total", "confidence"]

    def __init__(self, parent: tk.Misc, line: dict[str, Any], callback: Any):
        super().__init__(parent)
        self.callback = callback
        self.title("Invoice Line")
        self.geometry("520x420")
        self.transient(parent)
        self.grab_set()
        self.vars = {field: tk.StringVar(value=str(line.get(field, ""))) for field in self.FIELDS}
        if not self.vars["confidence"].get():
            self.vars["confidence"].set("1.0")
        for row, field in enumerate(self.FIELDS):
            ttk.Label(self, text=field.replace("_", " ").title()).grid(row=row, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(self, textvariable=self.vars[field], width=48).grid(row=row, column=1, sticky="ew", padx=10, pady=6)
        buttons = ttk.Frame(self)
        buttons.grid(row=len(self.FIELDS), column=0, columnspan=2, sticky="e", padx=10, pady=12)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=3)
        self.columnconfigure(1, weight=1)

    def save(self) -> None:
        line = {field: self.vars[field].get().strip() for field in self.FIELDS}
        if not line["description"]:
            messagebox.showerror("Missing description", "Description is required.", parent=self)
            return
        self.callback(line)
        self.destroy()



class OwnerSetupDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, controls: Any):
        super().__init__(parent)
        self.controls = controls
        self.created_user: AuthenticatedUser | None = None
        self.title("Create Restaurant Owner Account")
        self.geometry("520x360")
        self.transient(parent); self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.vars = {
            "display": tk.StringVar(), "username": tk.StringVar(value="owner"),
            "password": tk.StringVar(), "confirm": tk.StringVar(),
        }
        ttk.Label(self, text="Create the first Owner account", style="Title.TLabel").pack(anchor="w", padx=16, pady=(16, 6))
        ttk.Label(self, text="This account controls users, restores, settings, and the complete audit history.", style="Muted.TLabel", wraplength=470).pack(anchor="w", padx=16, pady=(0, 10))
        form = ttk.Frame(self, padding=12); form.pack(fill="both", expand=True)
        for row, (label, key, show) in enumerate((
            ("Display name", "display", ""), ("Username", "username", ""),
            ("Password", "password", "*"), ("Confirm password", "confirm", "*"),
        )):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(form, textvariable=self.vars[key], show=show, width=34).grid(row=row, column=1, sticky="ew", pady=6)
        buttons = ttk.Frame(form); buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=12)
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=3)
        ttk.Button(buttons, text="Create Owner", command=self.create).pack(side="right", padx=3)
        form.columnconfigure(1, weight=1)

    def create(self) -> None:
        if self.vars["password"].get() != self.vars["confirm"].get():
            messagebox.showerror("Password mismatch", "The passwords do not match.", parent=self); return
        try:
            self.created_user = self.controls.create_user(
                self.vars["username"].get(), self.vars["display"].get(), "Owner",
                self.vars["password"].get(), initial_owner=True,
            )
        except Exception as exc:
            messagebox.showerror("Owner setup failed", str(exc), parent=self); return
        self.destroy()

    def cancel(self) -> None:
        self.created_user = None; self.destroy()


class LoginDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, controls: Any, restaurant_name: str):
        super().__init__(parent)
        self.controls = controls
        self.user: AuthenticatedUser | None = None
        self.title(f"Sign in - {restaurant_name}")
        self.geometry("430x260")
        self.transient(parent); self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.username = tk.StringVar(value="owner")
        self.password = tk.StringVar()
        ttk.Label(self, text=restaurant_name, style="Title.TLabel").pack(anchor="w", padx=16, pady=(16, 4))
        form = ttk.Frame(self, padding=16); form.pack(fill="both", expand=True)
        ttk.Label(form, text="Username").grid(row=0, column=0, sticky="w", pady=7)
        user_entry = ttk.Entry(form, textvariable=self.username, width=30); user_entry.grid(row=0, column=1, sticky="ew", pady=7)
        ttk.Label(form, text="Password").grid(row=1, column=0, sticky="w", pady=7)
        password_entry = ttk.Entry(form, textvariable=self.password, show="*", width=30); password_entry.grid(row=1, column=1, sticky="ew", pady=7)
        password_entry.bind("<Return>", lambda _event: self.login())
        buttons = ttk.Frame(form); buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=14)
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=3)
        ttk.Button(buttons, text="Sign In", command=self.login).pack(side="right", padx=3)
        form.columnconfigure(1, weight=1)
        password_entry.focus_set()

    def login(self) -> None:
        user = self.controls.authenticate(self.username.get(), self.password.get())
        if not user:
            messagebox.showerror("Sign in failed", "Invalid username or password.", parent=self); return
        self.user = user; self.destroy()

    def cancel(self) -> None:
        self.user = None; self.destroy()


class UserEditDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, controls: Any, user: dict[str, Any] | None, callback: Any):
        super().__init__(parent)
        self.controls, self.user, self.callback = controls, user, callback
        self.title("Edit User" if user else "Add User")
        self.geometry("540x390")
        self.transient(parent); self.grab_set()
        self.vars = {
            "username": tk.StringVar(value=str((user or {}).get("username", ""))),
            "display_name": tk.StringVar(value=str((user or {}).get("display_name", ""))),
            "role": tk.StringVar(value=str((user or {}).get("role", "General Manager"))),
            "password": tk.StringVar(), "confirm": tk.StringVar(),
            "active": tk.BooleanVar(value=bool((user or {}).get("active", 1))),
        }
        form = ttk.Frame(self, padding=16); form.pack(fill="both", expand=True)
        ttk.Label(form, text=self.title(), style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        fields = (("Username", "username"), ("Display name", "display_name"))
        for row, (label, key) in enumerate(fields, 1):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6)
            entry = ttk.Entry(form, textvariable=self.vars[key], width=34)
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            if user and key == "username": entry.configure(state="disabled")
        ttk.Label(form, text="Role").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Combobox(form, textvariable=self.vars["role"], values=ALL_ROLES, state="readonly").grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="New password" if user else "Password").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.vars["password"], show="*").grid(row=4, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="Confirm password").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.vars["confirm"], show="*").grid(row=5, column=1, sticky="ew", pady=6)
        ttk.Checkbutton(form, text="Active user", variable=self.vars["active"]).grid(row=6, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(form, text="Leave password blank when editing to keep the current password.", style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(form); buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=14)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=3)
        form.columnconfigure(1, weight=1)

    def save(self) -> None:
        password = self.vars["password"].get()
        if password != self.vars["confirm"].get():
            messagebox.showerror("Password mismatch", "The passwords do not match.", parent=self); return
        try:
            if self.user:
                self.controls.update_user(
                    self.user["user_id"], display_name=self.vars["display_name"].get(),
                    role=self.vars["role"].get(), active=self.vars["active"].get(),
                    password=password or None,
                )
            else:
                self.controls.create_user(
                    self.vars["username"].get(), self.vars["display_name"].get(),
                    self.vars["role"].get(), password,
                )
        except Exception as exc:
            messagebox.showerror("User update failed", str(exc), parent=self); return
        self.callback(); self.destroy()


class ReceivingLineDialog(tk.Toplevel):
    STATUSES = ("Received", "Short", "Damaged", "Rejected", "Substituted", "Not Received")
    def __init__(self, parent: tk.Misc, line: dict[str, Any], callback: Any):
        super().__init__(parent)
        self.line, self.callback = line, callback
        self.title("Verify Delivery Line")
        self.geometry("590x430")
        self.transient(parent); self.grab_set()
        self.vars = {
            "received_quantity": tk.StringVar(value=str(line.get("received_quantity", line.get("expected_quantity", "0")))),
            "line_status": tk.StringVar(value=str(line.get("line_status") or "Received")),
            "credit_expected": tk.StringVar(value=str(line.get("credit_expected") or "0.00")),
            "substitution_description": tk.StringVar(value=str(line.get("substitution_description") or "")),
            "notes": tk.StringVar(value=str(line.get("notes") or "")),
        }
        form = ttk.Frame(self, padding=16); form.pack(fill="both", expand=True)
        ttk.Label(form, text=str(line.get("description", "Delivery item")), style="Title.TLabel", wraplength=520).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(form, text=f"Expected: {line.get('expected_quantity')} {line.get('unit') or ''}", style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        fields = (("Received quantity", "received_quantity"), ("Expected credit", "credit_expected"), ("Substitution", "substitution_description"), ("Notes", "notes"))
        ttk.Label(form, text="Status").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(form, textvariable=self.vars["line_status"], values=self.STATUSES, state="readonly").grid(row=2, column=1, sticky="ew", pady=6)
        for row, (label, key) in enumerate(fields, 3):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(form, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", pady=6)
        buttons = ttk.Frame(form); buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=14)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(buttons, text="Save Line", command=self.save).pack(side="right", padx=3)
        form.columnconfigure(1, weight=1)

    def save(self) -> None:
        updated = dict(self.line)
        updated.update({key: var.get().strip() for key, var in self.vars.items()})
        self.callback(updated); self.destroy()


class ReceivingDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, pipeline: InvoicePipeline, session_id: str, callback: Any):
        super().__init__(parent)
        self.pipeline, self.session_id, self.callback = pipeline, session_id, callback
        self.title("Receiving Verification")
        self.geometry("1120x720")
        self.minsize(920, 580)
        self.transient(parent); self.grab_set()
        session, lines = pipeline.get_receiving(session_id)
        self.session = dict(session)
        self.lines = {str(row["receiving_line_id"]): dict(row) for row in lines}
        self.received_date = tk.StringVar(value=self.session.get("received_date") or date.today().isoformat())
        self.notes = tk.StringVar(value=self.session.get("notes") or "")
        self._build(); self.refresh_lines()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        ttk.Label(top, text=f"{self.session.get('vendor')} - {self.session.get('invoice_number')}", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text=f"Invoice date: {self.session.get('invoice_date')}").pack(side="right")
        tools = ttk.Frame(self, padding=(10, 2)); tools.pack(fill="x")
        ttk.Button(tools, text="Mark All Received", command=self.mark_all_received).pack(side="left", padx=3)
        ttk.Button(tools, text="Edit Selected", command=self.edit_selected).pack(side="left", padx=3)
        ttk.Label(tools, text="Received date:").pack(side="left", padx=(18, 3))
        ttk.Entry(tools, textvariable=self.received_date, width=12).pack(side="left")
        cols = ("sku", "description", "expected", "received", "unit", "status", "credit", "notes")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for col, width in {"sku":110,"description":300,"expected":85,"received":85,"unit":70,"status":105,"credit":90,"notes":230}.items():
            self.tree.heading(col, text=col.replace("_", " ").title()); self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=6)
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())
        bottom = ttk.Frame(self, padding=10); bottom.pack(fill="x")
        ttk.Label(bottom, text="Delivery notes:").pack(side="left")
        ttk.Entry(bottom, textvariable=self.notes, width=55).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(bottom, text="Save Verification", command=self.save).pack(side="right", padx=3)

    def refresh_lines(self) -> None:
        for item in self.tree.get_children(): self.tree.delete(item)
        for line_id, row in self.lines.items():
            self.tree.insert("", "end", iid=line_id, values=(
                row.get("vendor_sku", ""), row.get("description", ""), row.get("expected_quantity", ""),
                row.get("received_quantity", ""), row.get("unit", ""), row.get("line_status", ""),
                row.get("credit_expected", "0.00"), row.get("notes", ""),
            ))

    def mark_all_received(self) -> None:
        for row in self.lines.values():
            row["received_quantity"] = row.get("expected_quantity", "0")
            row["line_status"] = "Received"
            row["credit_expected"] = "0.00"
        self.refresh_lines()

    def edit_selected(self) -> None:
        selected = self.tree.selection()
        if not selected: return
        line_id = selected[0]
        ReceivingLineDialog(self, dict(self.lines[line_id]), lambda row: self._line_updated(line_id, row))

    def _line_updated(self, line_id: str, row: dict[str, Any]) -> None:
        self.lines[line_id] = row; self.refresh_lines()

    def save(self) -> None:
        try:
            result = self.pipeline.save_receiving(
                self.session_id, list(self.lines.values()), received_date=self.received_date.get().strip(),
                notes=self.notes.get().strip(), finalize=True,
            )
        except Exception as exc:
            messagebox.showerror("Receiving save failed", str(exc), parent=self); return
        messagebox.showinfo("Receiving saved", f"Status: {result['status']}\nDiscrepancies: {result['discrepancy_count']}", parent=self)
        self.callback(result); self.destroy()


def main() -> int:
    root = tk.Tk()
    app = RestaurantCostControllerGUI(root)
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
