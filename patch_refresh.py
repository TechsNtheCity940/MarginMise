from pathlib import Path
p = Path(r"C:\devprojects\marginmise\manager_first_gui.py")
s = p.read_text(encoding="utf-8")
old = '''    def refresh_all(self) -> None:
        if self.dashboard_service:
            self.dashboard_service.invalidate()
        super().refresh_all()
        self.refresh_simple_settings()
        self.refresh_margin_memory()
        self._update_role_navigation()
'''
new = '''    def refresh_all(self) -> None:
        """Refresh only widgets that have actually been built."""
        if self.dashboard_service:
            self.dashboard_service.invalidate()
        self.refresh_dashboard()
        if hasattr(self, "simple_setting_vars"):
            self.refresh_simple_settings()
        if hasattr(self, "margin_memory_tree"):
            self.refresh_margin_memory()
        built = getattr(self, "_built_pages", set())
        refreshers = (
            (self.intake_tab, self.refresh_uploads),
            (self.review_tab, self.refresh_review),
            (self.auto_upload_tab, self.refresh_auto_upload_history),
            (self.exceptions_tab, self.refresh_exceptions_health),
            (self.receiving_tab, self.refresh_receiving),
            (self.items_tab, self.refresh_items),
            (self.inventory_tab, self.refresh_inventory),
            (self.orders_tab, self.refresh_orders),
            (self.data_tab, self.refresh_data_summary),
            (self.phase2_tab, self.refresh_phase2),
            (self.phase3_tab, self.refresh_phase3),
            (self.chat_tab, self.refresh_chat_status),
            (self.settings_tab, self.refresh_settings),
            (self.security_tab, self.refresh_security),
        )
        for frame, refresher in refreshers:
            if frame in built:
                try:
                    refresher()
                except Exception as exc:
                    self.log(f"Deferred page refresh warning: {exc}")
        self._update_role_navigation()
'''
assert old in s, "refresh_all target not found"
p.write_text(s.replace(old, new, 1), encoding="utf-8")
print("patched refresh_all")
