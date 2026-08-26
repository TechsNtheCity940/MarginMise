#!/usr/bin/env python3
"""Operational controls for Restaurant Cost Controller v2.6.

Provides:
- automatic and manual backups with verified restore
- local role-based users and permission checks
- immutable audit history
- data-quality scoring and exception management
- receiving and delivery verification

The service is intentionally local-first and uses only the Python standard
library. Each restaurant workspace owns its own users, audit history, backups,
receiving records, and exception state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

MONEY = Decimal("0.01")

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "Owner": {"*"},
    "General Manager": {
        "invoices.upload", "invoices.process", "invoices.review", "items.edit",
        "inventory.count", "inventory.close", "orders.generate", "orders.edit",
        "orders.approve", "sales.import", "costs.import", "reports.export",
        "receiving.verify", "exceptions.manage", "audit.view", "chat.use",
        "backups.create", "settings.view", "pos.import", "recipes.manage",
        "mobile_counts.manage", "waste.log", "purchase_orders.manage", "accounting.export",
        "portfolio.view", "transfers.manage", "forecasts.manage", "distributors.manage",
        "profitability.view", "owner_reports.export", "savings.manage",
        "margin_memory.view", "margin_memory.manage", "reviews.center",
    },
    "Inventory Manager": {
        "invoices.upload", "invoices.process", "items.edit", "inventory.count",
        "inventory.close", "orders.generate", "orders.edit", "orders.approve",
        "receiving.verify", "exceptions.manage", "reports.export", "chat.use",
        "settings.view", "pos.import", "recipes.manage", "mobile_counts.manage",
        "waste.log", "purchase_orders.manage", "portfolio.view", "transfers.manage",
        "forecasts.manage", "distributors.manage", "profitability.view", "owner_reports.export",
        "margin_memory.view", "margin_memory.manage", "reviews.center",
    },
    "Receiving": {
        "invoices.upload", "receiving.verify", "exceptions.view", "chat.use",
        "settings.view", "mobile_counts.manage", "waste.log", "margin_memory.view", "reviews.center",
    },
    "Bookkeeper": {
        "invoices.upload", "invoices.process", "invoices.review", "sales.import",
        "costs.import", "reports.export", "audit.view", "exceptions.view",
        "chat.use", "backups.create", "settings.view", "pos.import", "accounting.export",
        "portfolio.view", "profitability.view", "owner_reports.export", "margin_memory.view", "reviews.center",
    },
    "Viewer": {"reports.view", "exceptions.view", "chat.use", "settings.view", "portfolio.view", "profitability.view", "margin_memory.view", "reviews.center"},
}

ALL_ROLES = tuple(ROLE_PERMISSIONS)

CONTROL_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_iterations INTEGER NOT NULL DEFAULT 240000,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id TEXT,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    summary TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS backup_history (
    backup_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    backup_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS operational_exceptions (
    exception_id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    recommended_action TEXT,
    source_type TEXT,
    source_id TEXT,
    source_json TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    first_detected TEXT NOT NULL,
    last_detected TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_exceptions_status ON operational_exceptions(status, severity, last_detected DESC);

CREATE TABLE IF NOT EXISTS data_quality_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    overall_score INTEGER NOT NULL,
    completeness_score INTEGER NOT NULL,
    freshness_score INTEGER NOT NULL,
    integrity_score INTEGER NOT NULL,
    operational_score INTEGER NOT NULL,
    grade TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receiving_sessions (
    session_id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL UNIQUE REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    vendor TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    received_date TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    created_by TEXT NOT NULL,
    notes TEXT,
    discrepancy_count INTEGER NOT NULL DEFAULT 0,
    expected_value TEXT NOT NULL DEFAULT '0.00',
    received_value TEXT NOT NULL DEFAULT '0.00',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT
);

CREATE TABLE IF NOT EXISTS receiving_lines (
    receiving_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES receiving_sessions(session_id) ON DELETE CASCADE,
    invoice_line_id INTEGER NOT NULL,
    item_id TEXT,
    vendor_sku TEXT,
    description TEXT NOT NULL,
    expected_quantity TEXT NOT NULL,
    received_quantity TEXT NOT NULL DEFAULT '0',
    unit TEXT,
    unit_price TEXT NOT NULL DEFAULT '0.00',
    line_status TEXT NOT NULL DEFAULT 'Pending',
    substitution_description TEXT,
    credit_expected TEXT NOT NULL DEFAULT '0.00',
    notes TEXT,
    UNIQUE(session_id, invoice_line_id)
);
CREATE TABLE IF NOT EXISTS costpilot_review_resolutions (
    case_type TEXT NOT NULL,
    case_id TEXT NOT NULL,
    resolution_status TEXT NOT NULL,
    resolution_code TEXT NOT NULL,
    resolution_note TEXT,
    estimated_value TEXT NOT NULL DEFAULT '0.00',
    resolved_by TEXT,
    resolved_by_role TEXT,
    resolved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(case_type, case_id)
);

CREATE TABLE IF NOT EXISTS costpilot_review_actions (
    action_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT,
    created_by_role TEXT,
    action_code TEXT NOT NULL,
    action_scope TEXT NOT NULL,
    case_count INTEGER NOT NULL DEFAULT 0,
    requested_case_ids_json TEXT NOT NULL,
    affected_case_ids_json TEXT NOT NULL,
    skipped_case_ids_json TEXT NOT NULL,
    result_status TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_costpilot_review_actions_created
ON costpilot_review_actions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_costpilot_review_resolutions_status
ON costpilot_review_resolutions(resolution_status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_receiving_status ON receiving_sessions(status, invoice_date DESC);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


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


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def money(value: Any) -> Decimal:
    return dec(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str
    display_name: str
    role: str

    def can(self, permission: str) -> bool:
        granted = ROLE_PERMISSIONS.get(self.role, set())
        return "*" in granted or permission in granted


class OperationalControlsError(RuntimeError):
    pass


class PermissionDenied(OperationalControlsError):
    pass


class OperationalControlsService:
    def __init__(self, workspace: Any):
        self.workspace = workspace
        self.backup_dir = workspace.root / "Backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.current_user: AuthenticatedUser | None = None
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.workspace.connect() as conn:
            conn.executescript(CONTROL_SCHEMA_SQL)

    # ------------------------------------------------------------------
    # Users and permissions
    # ------------------------------------------------------------------
    @staticmethod
    def _password_digest(password: str, salt_hex: str, iterations: int) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
        ).hex()

    def has_users(self) -> bool:
        with self.workspace.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def create_user(
        self,
        username: str,
        display_name: str,
        role: str,
        password: str,
        *,
        actor: AuthenticatedUser | None = None,
        initial_owner: bool = False,
    ) -> AuthenticatedUser:
        username = username.strip()
        display_name = display_name.strip() or username
        if not username or any(ch.isspace() for ch in username):
            raise OperationalControlsError("Username is required and cannot contain spaces.")
        if role not in ROLE_PERMISSIONS:
            raise OperationalControlsError(f"Unsupported role: {role}")
        if len(password) < 8:
            raise OperationalControlsError("Password must contain at least 8 characters.")
        if not initial_owner:
            self.require_permission("users.manage", actor)
        elif self.has_users():
            raise OperationalControlsError("An owner account already exists.")
        salt = secrets.token_hex(16)
        iterations = 240000
        digest = self._password_digest(password, salt, iterations)
        user_id = f"USR-{uuid.uuid4().hex[:12].upper()}"
        stamp = now_iso()
        try:
            with self.workspace.connect() as conn:
                conn.execute(
                    """INSERT INTO users(user_id,username,display_name,role,password_salt,password_hash,
                       password_iterations,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,1,?,?)""",
                    (user_id, username, display_name, role, salt, digest, iterations, stamp, stamp),
                )
        except sqlite3.IntegrityError as exc:
            raise OperationalControlsError("That username already exists.") from exc
        created = AuthenticatedUser(user_id, username, display_name, role)
        self.audit(
            "user.create", "user", user_id, f"Created user {username} with role {role}",
            after={"username": username, "display_name": display_name, "role": role},
            actor=actor or created,
        )
        return created

    def authenticate(self, username: str, password: str) -> AuthenticatedUser | None:
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username.strip(),)
            ).fetchone()
        if not row:
            return None
        digest = self._password_digest(password, row["password_salt"], int(row["password_iterations"]))
        if not hmac.compare_digest(digest, row["password_hash"]):
            return None
        user = AuthenticatedUser(row["user_id"], row["username"], row["display_name"], row["role"])
        with self.workspace.connect() as conn:
            conn.execute("UPDATE users SET last_login=?,updated_at=? WHERE user_id=?", (now_iso(), now_iso(), user.user_id))
        self.current_user = user
        self.audit("user.login", "user", user.user_id, f"{user.username} signed in", actor=user)
        return user

    def sign_out(self) -> None:
        if self.current_user:
            self.audit("user.logout", "user", self.current_user.user_id, f"{self.current_user.username} signed out")
        self.current_user = None

    def list_users(self) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute(
                "SELECT user_id,username,display_name,role,active,created_at,updated_at,last_login FROM users ORDER BY username"
            ).fetchall()

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        role: str | None = None,
        active: bool | None = None,
        password: str | None = None,
        actor: AuthenticatedUser | None = None,
    ) -> None:
        self.require_permission("users.manage", actor)
        with self.workspace.connect() as conn:
            before = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not before:
            raise OperationalControlsError("User not found.")
        changes: dict[str, Any] = {}
        if display_name is not None:
            changes["display_name"] = display_name.strip() or before["display_name"]
        if role is not None:
            if role not in ROLE_PERMISSIONS:
                raise OperationalControlsError(f"Unsupported role: {role}")
            changes["role"] = role
        if active is not None:
            if before["role"] == "Owner" and not active:
                with self.workspace.connect() as conn:
                    active_owners = conn.execute(
                        "SELECT COUNT(*) FROM users WHERE role='Owner' AND active=1"
                    ).fetchone()[0]
                if active_owners <= 1:
                    raise OperationalControlsError("The final active Owner account cannot be disabled.")
            changes["active"] = 1 if active else 0
        if password is not None:
            if len(password) < 8:
                raise OperationalControlsError("Password must contain at least 8 characters.")
            salt = secrets.token_hex(16)
            iterations = 240000
            changes.update({
                "password_salt": salt,
                "password_hash": self._password_digest(password, salt, iterations),
                "password_iterations": iterations,
            })
        if not changes:
            return
        changes["updated_at"] = now_iso()
        assignments = ",".join(f"{key}=?" for key in changes)
        with self.workspace.connect() as conn:
            conn.execute(f"UPDATE users SET {assignments} WHERE user_id=?", tuple(changes.values()) + (user_id,))
            after = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        safe_before = {k: before[k] for k in ("username", "display_name", "role", "active")}
        safe_after = {k: after[k] for k in ("username", "display_name", "role", "active")}
        self.audit("user.update", "user", user_id, f"Updated user {before['username']}", before=safe_before, after=safe_after, actor=actor)

    def require_permission(self, permission: str, actor: AuthenticatedUser | None = None) -> None:
        user = actor or self.current_user
        if user is None:
            raise PermissionDenied("Sign in to continue.")
        if not user.can(permission):
            raise PermissionDenied(f"{user.role} does not have permission: {permission}")

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    def audit(
        self,
        action: str,
        entity_type: str,
        entity_id: str | None,
        summary: str,
        *,
        before: Any = None,
        after: Any = None,
        details: Any = None,
        actor: AuthenticatedUser | None = None,
    ) -> None:
        user = actor or self.current_user
        username = user.username if user else "system"
        role = user.role if user else "System"
        user_id = user.user_id if user else None
        with self.workspace.connect() as conn:
            conn.execute(
                """INSERT INTO audit_log(created_at,user_id,username,role,action,entity_type,entity_id,
                   summary,before_json,after_json,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now_iso(), user_id, username, role, action, entity_type, entity_id, summary,
                    json.dumps(json_safe(before), separators=(",", ":")) if before is not None else None,
                    json.dumps(json_safe(after), separators=(",", ":")) if after is not None else None,
                    json.dumps(json_safe(details), separators=(",", ":")) if details is not None else None,
                ),
            )

    def list_audit(self, limit: int = 1000, *, entity_type: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if entity_type:
            sql += " WHERE entity_type=?"
            params.append(entity_type)
        sql += " ORDER BY audit_id DESC LIMIT ?"
        params.append(int(limit))
        with self.workspace.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------
    # Backups and restore
    # ------------------------------------------------------------------
    def _backup_settings(self) -> tuple[int, int]:
        settings = self.workspace.load_settings()
        interval = max(1, int(settings.get("automatic_backup_interval_hours", 24)))
        retention = max(3, int(settings.get("backup_retention_count", 30)))
        return interval, retention

    def _sync_backup_history(self) -> None:
        with self.workspace.connect() as conn:
            known = {row[0] for row in conn.execute("SELECT file_path FROM backup_history").fetchall()}
        for path in sorted(self.backup_dir.glob("*.zip")):
            if str(path) in known:
                continue
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    manifest = json.loads(archive.read("backup_manifest.json").decode("utf-8"))
                if manifest.get("format") != "restaurant-cost-controller-backup":
                    continue
                backup_id = str(manifest.get("backup_id") or path.stem)
                created_at = str(manifest.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0).isoformat())
                with self.workspace.connect() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO backup_history(backup_id,created_at,created_by,backup_type,file_path,
                           size_bytes,sha256,status,notes) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (backup_id, created_at, "system", "Recovered", str(path), path.stat().st_size, sha256_file(path), "Complete", "Recovered from backup folder scan"),
                    )
            except Exception:
                continue

    def latest_backup(self) -> sqlite3.Row | None:
        self._sync_backup_history()
        with self.workspace.connect() as conn:
            return conn.execute(
                "SELECT * FROM backup_history WHERE status='Complete' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    def backup_due(self) -> bool:
        interval, _ = self._backup_settings()
        latest = self.latest_backup()
        if not latest:
            return True
        try:
            created = datetime.fromisoformat(latest["created_at"])
        except Exception:
            return True
        return datetime.now() - created >= timedelta(hours=interval)

    def automatic_backup_if_due(self) -> Path | None:
        settings = self.workspace.load_settings()
        if not settings.get("automatic_backups_enabled", True) or not self.backup_due():
            return None
        return self.create_backup("Automatic")

    def _iter_backup_files(self) -> Iterable[tuple[Path, Path]]:
        excluded_roots = {self.backup_dir.resolve(), (self.workspace.root / "Logs").resolve(), (self.workspace.root / "Exports").resolve()}
        excluded_names = {"restaurant_costs.sqlite3", "restaurant_costs.sqlite3-wal", "restaurant_costs.sqlite3-shm"}
        for path in self.workspace.root.rglob("*"):
            if not path.is_file() or path.name in excluded_names:
                continue
            resolved = path.resolve()
            if any(root == resolved or root in resolved.parents for root in excluded_roots):
                continue
            yield path, path.relative_to(self.workspace.root)

    def create_backup(self, backup_type: str = "Manual", notes: str = "") -> Path:
        if backup_type.lower() == "manual":
            self.require_permission("backups.create")
        backup_id = f"BKP-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        filename = f"{backup_id}_{self.workspace.root.name}.zip"
        destination = self.backup_dir / filename
        with tempfile.TemporaryDirectory(prefix="restaurant-backup-") as temp_name:
            temp = Path(temp_name)
            db_snapshot = temp / "restaurant_costs.sqlite3"
            source_conn = self.workspace.connect()
            target_conn = sqlite3.connect(db_snapshot)
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
                source_conn.close()
            manifest = {
                "format": "restaurant-cost-controller-backup",
                "version": 1,
                "backup_id": backup_id,
                "created_at": now_iso(),
                "restaurant_root_name": self.workspace.root.name,
                "database": "restaurant_costs.sqlite3",
            }
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_snapshot, "restaurant_costs.sqlite3")
                for source, relative in self._iter_backup_files():
                    archive.write(source, str(relative).replace("\\", "/"))
                archive.writestr("backup_manifest.json", json.dumps(manifest, indent=2))
        checksum = sha256_file(destination)
        with self.workspace.connect() as conn:
            conn.execute(
                """INSERT INTO backup_history(backup_id,created_at,created_by,backup_type,file_path,
                   size_bytes,sha256,status,notes) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    backup_id, manifest["created_at"], self.current_user.username if self.current_user else "system",
                    backup_type, str(destination), destination.stat().st_size, checksum, "Complete", notes,
                ),
            )
        self.audit("backup.create", "backup", backup_id, f"Created {backup_type.lower()} backup", after={"file": str(destination), "sha256": checksum})
        self.prune_backups()
        return destination

    def prune_backups(self) -> None:
        _, retention = self._backup_settings()
        with self.workspace.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM backup_history WHERE status='Complete' ORDER BY created_at DESC"
            ).fetchall()
            for row in rows[retention:]:
                path = Path(row["file_path"])
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    continue
                conn.execute("UPDATE backup_history SET status='Pruned' WHERE backup_id=?", (row["backup_id"],))

    def list_backups(self, limit: int = 100) -> list[sqlite3.Row]:
        self._sync_backup_history()
        with self.workspace.connect() as conn:
            return conn.execute("SELECT * FROM backup_history ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()

    def validate_backup(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise OperationalControlsError("Backup file does not exist.")
        try:
            with zipfile.ZipFile(path, "r") as archive:
                if archive.testzip() is not None:
                    raise OperationalControlsError("Backup ZIP failed its integrity check.")
                names = set(archive.namelist())
                if "backup_manifest.json" not in names or "restaurant_costs.sqlite3" not in names:
                    raise OperationalControlsError("This is not a valid MarginMise backup.")
                manifest = json.loads(archive.read("backup_manifest.json").decode("utf-8"))
                if manifest.get("format") != "restaurant-cost-controller-backup":
                    raise OperationalControlsError("Unsupported backup format.")
                with tempfile.TemporaryDirectory(prefix="restaurant-backup-check-") as temp_name:
                    db_path = Path(temp_name) / "restaurant_costs.sqlite3"
                    db_path.write_bytes(archive.read("restaurant_costs.sqlite3"))
                    conn = sqlite3.connect(db_path)
                    try:
                        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    finally:
                        conn.close()
                    if result != "ok":
                        raise OperationalControlsError(f"Backup database integrity check failed: {result}")
        except zipfile.BadZipFile as exc:
            raise OperationalControlsError("Backup file is not a readable ZIP archive.") from exc
        return manifest

    def restore_backup(self, path: Path) -> None:
        self.require_permission("backups.restore")
        manifest = self.validate_backup(path)
        safety = self.create_backup("Pre-Restore", notes=f"Created before restoring {path.name}")
        managed_top_level = {
            "Upload Invoices", "Processed Invoices", "Needs Review", "Original Documents",
            "Extracted JSON", "Sales", "Operating Costs", "Manager Chat",
        }
        with tempfile.TemporaryDirectory(prefix="restaurant-restore-") as temp_name:
            temp = Path(temp_name)
            with zipfile.ZipFile(path, "r") as archive:
                archive.extractall(temp)
            for name in managed_top_level:
                target = self.workspace.root / name
                source = temp / name
                if target.exists():
                    shutil.rmtree(target)
                if source.exists():
                    shutil.copytree(source, target)
                else:
                    target.mkdir(parents=True, exist_ok=True)
            config_source = temp / "restaurant_config.json"
            if config_source.exists():
                shutil.copy2(config_source, self.workspace.config_path)
            db_source = temp / "restaurant_costs.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                target = Path(str(self.workspace.db_path) + suffix)
                if target.exists():
                    target.unlink()
            shutil.copy2(db_source, self.workspace.db_path)
        self.ensure_schema()
        self._sync_backup_history()
        self.audit(
            "backup.restore", "backup", manifest.get("backup_id"),
            f"Restored backup {path.name}",
            details={"restored_file": str(path), "pre_restore_backup": str(safety)},
        )

    # ------------------------------------------------------------------
    # Receiving verification
    # ------------------------------------------------------------------
    def list_receiving_invoices(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute(
                """SELECT i.invoice_id,i.vendor,i.invoice_number,i.invoice_date,i.total,i.status AS invoice_status,
                   COALESCE(r.status,'Not Started') AS receiving_status,r.session_id,r.received_date,
                   COALESCE(r.discrepancy_count,0) AS discrepancy_count
                   FROM invoices i LEFT JOIN receiving_sessions r ON r.invoice_id=i.invoice_id
                   WHERE i.status='Approved'
                   ORDER BY i.invoice_date DESC,i.vendor LIMIT ?""",
                (int(limit),),
            ).fetchall()

    def start_receiving(self, invoice_id: str) -> str:
        self.require_permission("receiving.verify")
        with self.workspace.connect() as conn:
            existing = conn.execute("SELECT session_id FROM receiving_sessions WHERE invoice_id=?", (invoice_id,)).fetchone()
            if existing:
                return existing["session_id"]
            invoice = conn.execute("SELECT * FROM invoices WHERE invoice_id=? AND status='Approved'", (invoice_id,)).fetchone()
            if not invoice:
                raise OperationalControlsError("Only approved invoices can be received.")
            lines = conn.execute("SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_number", (invoice_id,)).fetchall()
            if not lines:
                raise OperationalControlsError("The invoice has no line items to receive.")
            session_id = f"RCV-{uuid.uuid4().hex[:12].upper()}"
            stamp = now_iso()
            expected_value = sum((money(row["line_total"]) for row in lines), Decimal("0"))
            conn.execute(
                """INSERT INTO receiving_sessions(session_id,invoice_id,vendor,invoice_number,invoice_date,
                   status,created_by,expected_value,received_value,created_at,updated_at)
                   VALUES(?,?,?,?,?,'Open',?,?, '0.00',?,?)""",
                (
                    session_id, invoice_id, invoice["vendor"], invoice["invoice_number"], invoice["invoice_date"],
                    self.current_user.username if self.current_user else "system", f"{expected_value:.2f}", stamp, stamp,
                ),
            )
            for row in lines:
                conn.execute(
                    """INSERT INTO receiving_lines(session_id,invoice_line_id,item_id,vendor_sku,description,
                       expected_quantity,received_quantity,unit,unit_price,line_status,credit_expected,notes)
                       VALUES(?,?,?,?,?,?,?, ?,?,'Pending','0.00','')""",
                    (
                        session_id, row["line_id"], row["item_id"], row["vendor_sku"], row["description"],
                        row["quantity"], row["quantity"], row["unit"], row["unit_price"],
                    ),
                )
        self.audit("receiving.start", "receiving", session_id, f"Started receiving for invoice {invoice_id}", details={"invoice_id": invoice_id})
        return session_id

    def get_receiving(self, session_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        with self.workspace.connect() as conn:
            session = conn.execute("SELECT * FROM receiving_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not session:
                raise OperationalControlsError("Receiving session not found.")
            lines = conn.execute("SELECT * FROM receiving_lines WHERE session_id=? ORDER BY receiving_line_id", (session_id,)).fetchall()
        return session, lines

    def save_receiving(
        self,
        session_id: str,
        lines: Iterable[dict[str, Any]],
        *,
        received_date: str | None = None,
        notes: str = "",
        finalize: bool = True,
    ) -> dict[str, Any]:
        self.require_permission("receiving.verify")
        session, before_lines = self.get_receiving(session_id)
        before = {"session": json_safe(session), "lines": json_safe(before_lines)}
        discrepancy_count = 0
        received_value = Decimal("0")
        stamp = now_iso()
        with self.workspace.connect() as conn:
            for supplied in lines:
                line_id = int(supplied["receiving_line_id"])
                row = conn.execute(
                    "SELECT * FROM receiving_lines WHERE receiving_line_id=? AND session_id=?", (line_id, session_id)
                ).fetchone()
                if not row:
                    raise OperationalControlsError(f"Receiving line {line_id} was not found.")
                expected = dec(row["expected_quantity"])
                received = dec(supplied.get("received_quantity"), str(expected))
                status = str(supplied.get("line_status") or "Received")
                if status not in {"Received", "Short", "Damaged", "Rejected", "Substituted", "Not Received"}:
                    raise OperationalControlsError(f"Unsupported receiving status: {status}")
                if received < 0:
                    raise OperationalControlsError("Received quantity cannot be negative.")
                if status == "Received" and received != expected:
                    status = "Short" if received < expected else "Substituted"
                if status != "Received" or received != expected:
                    discrepancy_count += 1
                unit_price = money(row["unit_price"])
                received_value += (received * unit_price).quantize(MONEY, rounding=ROUND_HALF_UP)
                credit = money(supplied.get("credit_expected", "0"))
                conn.execute(
                    """UPDATE receiving_lines SET received_quantity=?,line_status=?,substitution_description=?,
                       credit_expected=?,notes=? WHERE receiving_line_id=?""",
                    (
                        str(received), status, str(supplied.get("substitution_description") or ""),
                        f"{credit:.2f}", str(supplied.get("notes") or ""), line_id,
                    ),
                )
            final_status = "Verified" if finalize and discrepancy_count == 0 else ("Needs Review" if finalize else "Open")
            final_date = received_date or date.today().isoformat()
            conn.execute(
                """UPDATE receiving_sessions SET received_date=?,status=?,notes=?,discrepancy_count=?,
                   received_value=?,updated_at=?,finalized_at=? WHERE session_id=?""",
                (
                    final_date, final_status, notes, discrepancy_count, f"{received_value:.2f}", stamp,
                    stamp if finalize else None, session_id,
                ),
            )
        session_after, lines_after = self.get_receiving(session_id)
        after = {"session": json_safe(session_after), "lines": json_safe(lines_after)}
        self.audit(
            "receiving.verify", "receiving", session_id,
            f"Receiving {final_status.lower()} with {discrepancy_count} discrepancy(s)",
            before=before, after=after,
        )
        self.refresh_exceptions()
        return {"session_id": session_id, "status": final_status, "discrepancy_count": discrepancy_count, "received_value": f"{received_value:.2f}"}

    def auto_verify_receiving(
        self,
        invoice_ids: Iterable[str] | None = None,
        *,
        date_mode: str = "invoice_date",
        actor: AuthenticatedUser | None = None,
    ) -> dict[str, Any]:
        """Verify eligible approved invoices as received in full.

        Existing Needs Review sessions are never overwritten. Existing Open
        sessions are eligible only while every line remains Pending/Received and
        no quantity differs from the invoice. This keeps automation fast without
        erasing a manager's shortage or damage entry.
        """
        automation_actor = actor or self.current_user or AuthenticatedUser(
            "SYSTEM-RECEIVING-AUTOMATION",
            "receiving_automation",
            "Receiving Automation",
            "Owner",
        )
        self.require_permission("receiving.verify", automation_actor)
        requested = [str(value) for value in invoice_ids] if invoice_ids is not None else None
        with self.workspace.connect() as conn:
            if requested:
                placeholders = ",".join("?" for _ in requested)
                invoices = conn.execute(
                    f"SELECT * FROM invoices WHERE status='Approved' AND invoice_id IN ({placeholders}) ORDER BY invoice_date,invoice_id",
                    tuple(requested),
                ).fetchall()
            else:
                invoices = conn.execute(
                    """SELECT i.* FROM invoices i
                       LEFT JOIN receiving_sessions r ON r.invoice_id=i.invoice_id
                       WHERE i.status='Approved' AND (r.session_id IS NULL OR r.status!='Verified')
                       ORDER BY i.invoice_date,i.invoice_id"""
                ).fetchall()

        summary: dict[str, Any] = {
            "requested": len(requested) if requested is not None else len(invoices),
            "eligible": 0, "verified": 0, "already_verified": 0,
            "skipped_review": 0, "skipped_ineligible": 0, "failed": 0,
            "results": [],
        }
        previous_user = self.current_user
        self.current_user = automation_actor
        try:
            for invoice in invoices:
                invoice_id = invoice["invoice_id"]
                try:
                    invoice_date = str(invoice["invoice_date"] or "")
                    try:
                        parsed_invoice_date = date.fromisoformat(invoice_date)
                    except ValueError:
                        parsed_invoice_date = None
                    if parsed_invoice_date and parsed_invoice_date > date.today() + timedelta(days=1):
                        summary["skipped_ineligible"] += 1
                        summary["results"].append({"invoice_id": invoice_id, "status": "Skipped", "reason": "future invoice date"})
                        continue

                    with self.workspace.connect() as conn:
                        existing = conn.execute(
                            "SELECT * FROM receiving_sessions WHERE invoice_id=?", (invoice_id,)
                        ).fetchone()
                    if existing and existing["status"] == "Verified":
                        summary["already_verified"] += 1
                        summary["results"].append({"invoice_id": invoice_id, "status": "Already Verified"})
                        continue
                    if existing and existing["status"] == "Needs Review":
                        summary["skipped_review"] += 1
                        summary["results"].append({"invoice_id": invoice_id, "status": "Skipped", "reason": "receiving discrepancy needs review"})
                        continue

                    session_id = existing["session_id"] if existing else self.start_receiving(invoice_id)
                    session, lines = self.get_receiving(session_id)
                    if not lines:
                        summary["skipped_ineligible"] += 1
                        summary["results"].append({"invoice_id": invoice_id, "status": "Skipped", "reason": "no receiving lines"})
                        continue
                    safe_open = True
                    payload = []
                    for line in lines:
                        expected = dec(line["expected_quantity"])
                        received = dec(line["received_quantity"], str(expected))
                        status = str(line["line_status"] or "Pending")
                        if expected <= 0:
                            safe_open = False
                            break
                        if existing and status not in {"Pending", "Received"}:
                            safe_open = False
                            break
                        if existing and received != expected:
                            safe_open = False
                            break
                        payload.append({
                            "receiving_line_id": line["receiving_line_id"],
                            "received_quantity": str(expected),
                            "line_status": "Received",
                            "credit_expected": "0.00",
                            "substitution_description": "",
                            "notes": str(line["notes"] or ""),
                        })
                    if not safe_open:
                        summary["skipped_review"] += 1
                        summary["results"].append({"invoice_id": invoice_id, "status": "Skipped", "reason": "existing receiving edits or invalid quantity"})
                        continue

                    summary["eligible"] += 1
                    received_date = (
                        invoice_date if date_mode == "invoice_date" and parsed_invoice_date else date.today().isoformat()
                    )
                    result = self.save_receiving(
                        session_id,
                        payload,
                        received_date=received_date,
                        notes="Automatically verified as received in full from the approved invoice.",
                        finalize=True,
                    )
                    summary["verified"] += 1
                    summary["results"].append({"invoice_id": invoice_id, **result})
                except Exception as exc:
                    summary["failed"] += 1
                    summary["results"].append({"invoice_id": invoice_id, "status": "Failed", "error": str(exc)})
        finally:
            self.current_user = previous_user
        self.audit(
            "receiving.batch_auto_verify",
            "receiving_batch",
            None,
            f"Automatically verified {summary['verified']} delivery record(s) as received in full.",
            details={key: value for key, value in summary.items() if key != "results"},
            actor=automation_actor,
        )
        return summary

    # ------------------------------------------------------------------
    # Exceptions and data quality
    # ------------------------------------------------------------------
    def _safe_rows(self, conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []

    def _detected_exceptions(self) -> list[dict[str, Any]]:
        detected: list[dict[str, Any]] = []
        today = date.today()
        settings = self.workspace.load_settings()
        with self.workspace.connect() as conn:
            for row in self._safe_rows(conn, """SELECT r.review_id,r.invoice_id,r.severity,r.issue_type,r.issue,
                i.vendor,i.invoice_number FROM reviews r JOIN invoices i ON i.invoice_id=r.invoice_id
                WHERE r.status='Open' AND r.issue_type!='New Item' ORDER BY r.created_at DESC"""):
                detected.append({
                    "key": f"review:{row['review_id']}", "severity": "Critical" if row["severity"] == "Error" else "Warning",
                    "category": "Invoice Review", "title": f"Invoice {row['invoice_number'] or row['invoice_id']} needs review",
                    "message": row["issue"], "recommended_action": "Open CostPilot Review Center to explain, correct, approve, or reject the invoice.",
                    "source_type": "invoice", "source_id": row["invoice_id"], "source": dict(row),
                })
            for row in self._safe_rows(conn, """SELECT p.price_id,p.invoice_id,p.item_id,p.item_description,p.previous_price,
                p.unit_price,p.price_change_percent FROM price_history p
                JOIN (SELECT item_id,MAX(price_id) AS max_price_id FROM price_history
                      WHERE price_alert=1 AND invoice_date>=date('now','-45 day') GROUP BY item_id) latest
                  ON latest.max_price_id=p.price_id
                ORDER BY ABS(CAST(p.price_change_percent AS REAL)) DESC LIMIT 100"""):
                detected.append({
                    "key": f"price:{row['price_id']}", "severity": "Warning", "category": "Price Change",
                    "title": f"Price alert: {row['item_description']}",
                    "message": f"Price changed from ${dec(row['previous_price']):,.2f} to ${dec(row['unit_price']):,.2f} ({dec(row['price_change_percent']):,.2f}%).",
                    "recommended_action": "Review the invoice and compare vendor pricing.",
                    "source_type": "price_history", "source_id": str(row["price_id"]), "source": dict(row),
                })
            for row in self._safe_rows(conn, """SELECT item_id,item_name,vendor_name,category,count_unit,
                units_per_purchase_unit,estimated_on_hand,last_purchase_date FROM items WHERE active=1"""):
                if not row["count_unit"] or dec(row["units_per_purchase_unit"], "0") <= 0:
                    detected.append({
                        "key": f"conversion:{row['item_id']}", "severity": "Warning", "category": "Item Setup",
                        "title": f"Missing count conversion: {row['item_name']}",
                        "message": "Purchase-to-count conversion is incomplete, reducing inventory and order accuracy.",
                        "recommended_action": "Open Items & Prices and set count unit and units per purchase unit.",
                        "source_type": "item", "source_id": row["item_id"], "source": dict(row),
                    })
                if not row["category"] or row["category"] == "Unclassified":
                    detected.append({
                        "key": f"category:{row['item_id']}", "severity": "Info", "category": "Item Setup",
                        "title": f"Unclassified item: {row['item_name']}",
                        "message": "The item has no manager-approved category.",
                        "recommended_action": "Assign a category in Items & Prices.",
                        "source_type": "item", "source_id": row["item_id"], "source": dict(row),
                    })
            count_rows = self._safe_rows(conn, """SELECT i.item_id,i.item_name,MAX(c.count_date) AS last_count
                FROM items i LEFT JOIN inventory_counts c ON c.item_id=i.item_id AND c.finalized=1
                WHERE i.active=1 GROUP BY i.item_id,i.item_name""")
            stale_counts = []
            for row in count_rows:
                last_count = row["last_count"]
                stale = True
                if last_count:
                    try:
                        stale = (today - date.fromisoformat(last_count)).days > 40
                    except ValueError:
                        pass
                if stale:
                    stale_counts.append(dict(row))
            if len(stale_counts) > 5:
                detected.append({
                    "key": "count:overdue-summary", "severity": "Warning", "category": "Inventory Count",
                    "title": f"Physical counts overdue for {len(stale_counts)} items",
                    "message": "One or more active items have no recent finalized physical count.",
                    "recommended_action": "Export and complete the current month-end inventory count sheet.",
                    "source_type": "inventory_count", "source_id": "overdue", "source": {"items": stale_counts},
                })
            else:
                for row in stale_counts:
                    detected.append({
                        "key": f"count:{row['item_id']}", "severity": "Warning", "category": "Inventory Count",
                        "title": f"Physical count overdue: {row['item_name']}",
                        "message": f"Last finalized count: {row['last_count'] or 'none'}.",
                        "recommended_action": "Complete or import a physical inventory count.",
                        "source_type": "item", "source_id": row["item_id"], "source": row,
                    })
            receiving_rows = self._safe_rows(conn, """SELECT i.invoice_id,i.vendor,i.invoice_number,i.invoice_date,
                r.status AS receiving_status,c.resolution_status AS costpilot_resolution
                FROM invoices i
                LEFT JOIN receiving_sessions r ON r.invoice_id=i.invoice_id
                LEFT JOIN costpilot_review_resolutions c
                  ON c.case_type='receiving' AND c.case_id=r.session_id
                WHERE i.status='Approved' AND (
                    r.status IS NULL OR r.status='Open' OR
                    (r.status='Needs Review' AND COALESCE(c.resolution_status,'Open') NOT IN
                        ('Resolved','Credit Pending','Replacement Pending'))
                )""") if settings.get("receiving_verification_enabled", True) else []
            for row in receiving_rows:
                try:
                    age = (today - date.fromisoformat(row["invoice_date"])).days
                except Exception:
                    age = 0
                if age >= 2 or row["receiving_status"] == "Needs Review":
                    detected.append({
                        "key": f"receiving:{row['invoice_id']}",
                        "severity": "Critical" if row["receiving_status"] == "Needs Review" else "Warning",
                        "category": "Receiving", "title": f"Delivery not verified: {row['vendor']} {row['invoice_number']}",
                        "message": f"Invoice date {row['invoice_date']}; receiving status {row['receiving_status'] or 'Not Started'}.",
                        "recommended_action": "Open Receiving and verify delivered quantities, damage, substitutions, and shortages.",
                        "source_type": "invoice", "source_id": row["invoice_id"], "source": dict(row),
                    })
            discrepancy_rows = self._safe_rows(conn, """SELECT s.session_id,s.invoice_id,s.vendor,s.invoice_number,
                s.discrepancy_count,s.status,c.resolution_status
                FROM receiving_sessions s
                LEFT JOIN costpilot_review_resolutions c
                  ON c.case_type='receiving' AND c.case_id=s.session_id
                WHERE s.status='Needs Review' AND COALESCE(c.resolution_status,'Open') NOT IN
                    ('Resolved','Credit Pending','Replacement Pending')""") if settings.get("receiving_verification_enabled", True) else []
            for row in discrepancy_rows:
                detected.append({
                    "key": f"receiving-discrepancy:{row['session_id']}", "severity": "Critical", "category": "Receiving",
                    "title": f"Receiving discrepancy: {row['vendor']} {row['invoice_number']}",
                    "message": f"{row['discrepancy_count']} line discrepancy(s) require manager follow-up.",
                    "recommended_action": "Review shortages, damage, substitutions, and expected vendor credits.",
                    "source_type": "receiving", "source_id": row["session_id"], "source": dict(row),
                })
            followup_rows = self._safe_rows(conn, """SELECT c.case_id AS session_id,c.resolution_status,c.resolution_code,
                c.estimated_value,c.resolution_note,c.updated_at,s.invoice_id,s.vendor,s.invoice_number
                FROM costpilot_review_resolutions c
                JOIN receiving_sessions s ON s.session_id=c.case_id
                WHERE c.case_type='receiving' AND c.resolution_status IN ('Credit Pending','Replacement Pending')""") if settings.get("receiving_verification_enabled", True) else []
            for row in followup_rows:
                value = float(row["estimated_value"] or 0)
                detected.append({
                    "key": f"receiving-followup:{row['session_id']}",
                    "severity": "Warning",
                    "category": "Vendor Follow-up",
                    "title": f"{row['resolution_status']}: {row['vendor']} {row['invoice_number']}",
                    "message": (
                        f"CostPilot review is complete. Expected value ${value:,.2f}. "
                        "The original receiving discrepancy remains preserved."
                    ),
                    "recommended_action": "Confirm the vendor credit or replacement, then resolve this follow-up exception.",
                    "source_type": "receiving", "source_id": row["session_id"], "source": dict(row),
                })
            sales = self._safe_rows(conn, "SELECT MAX(period_end) AS latest FROM sales")
            latest_sales = sales[0]["latest"] if sales else None
            if not latest_sales:
                detected.append({
                    "key": "sales:none", "severity": "Warning", "category": "Sales Data",
                    "title": "No sales data imported", "message": "Sales-driven usage and profitability estimates are incomplete.",
                    "recommended_action": "Import the latest sales report.", "source_type": "sales", "source_id": "latest", "source": {},
                })
            else:
                try:
                    days = (today - date.fromisoformat(latest_sales)).days
                except Exception:
                    days = 999
                if days > 7:
                    detected.append({
                        "key": "sales:stale", "severity": "Warning", "category": "Sales Data",
                        "title": "Sales data is stale", "message": f"Latest sales period ends {latest_sales}.",
                        "recommended_action": "Import a current sales report.", "source_type": "sales", "source_id": latest_sales,
                        "source": {"latest_period_end": latest_sales},
                    })
            estimates = self._safe_rows(conn, """SELECT item_id,item_name,count_unit,estimated_on_hand,
                (SELECT average_daily_usage FROM monthly_item_usage u WHERE u.item_id=items.item_id ORDER BY month DESC LIMIT 1) AS avg_daily,
                lead_time_days FROM items WHERE active=1""")
            for row in estimates:
                on_hand = dec(row["estimated_on_hand"])
                avg_daily = dec(row["avg_daily"])
                lead = max(dec(row["lead_time_days"], "2"), Decimal("1"))
                if avg_daily > 0 and on_hand <= avg_daily * lead:
                    detected.append({
                        "key": f"stockout:{row['item_id']}", "severity": "Critical", "category": "Stockout Risk",
                        "title": f"Likely stockout: {row['item_name']}",
                        "message": f"Estimated on hand {on_hand} {row['count_unit'] or 'units'} is below expected lead-time usage.",
                        "recommended_action": "Review the current draft order and physically verify stock.",
                        "source_type": "item", "source_id": row["item_id"], "source": dict(row),
                    })
        # Phase 2 readiness and workflow exceptions. These queries are safe on older
        # databases because _safe_rows returns an empty list when a table has not yet
        # been created.
        with self.workspace.connect() as conn:
            pos_runs = self._safe_rows(conn, "SELECT MAX(imported_at) AS last_import FROM pos_import_runs WHERE status='Imported'")
            last_pos = pos_runs[0]["last_import"] if pos_runs else None
            if not last_pos:
                detected.append({
                    "key": "pos:no-import", "severity": "Info", "category": "POS Sales",
                    "title": "No item-level POS sales imported",
                    "message": "Recipe usage and menu profitability cannot be calculated without item-level sales.",
                    "recommended_action": "Open Phase 2 Operations and import a CSV or Excel product-sales report.",
                    "source_type": "pos_import", "source_id": "none", "source": {},
                })
            else:
                try:
                    age = (datetime.now() - datetime.fromisoformat(last_pos)).days
                except ValueError:
                    age = 999
                if age > 8:
                    detected.append({
                        "key": "pos:stale", "severity": "Warning", "category": "POS Sales",
                        "title": "Item-level POS sales are stale",
                        "message": f"Last POS product-sales import: {last_pos}.",
                        "recommended_action": "Import the newest item-mix or product-sales report.",
                        "source_type": "pos_import", "source_id": last_pos, "source": {"last_import": last_pos},
                    })
            recipe_rows = self._safe_rows(conn, """SELECT COUNT(*) AS menu_count,
                SUM(CASE WHEN EXISTS(SELECT 1 FROM recipe_ingredients r WHERE r.menu_item_id=m.menu_item_id) THEN 1 ELSE 0 END) AS configured
                FROM menu_items m WHERE m.active=1""")
            if recipe_rows and int(recipe_rows[0]["menu_count"] or 0) > int(recipe_rows[0]["configured"] or 0):
                missing = int(recipe_rows[0]["menu_count"] or 0) - int(recipe_rows[0]["configured"] or 0)
                detected.append({
                    "key": "recipes:missing", "severity": "Warning", "category": "Recipes",
                    "title": f"{missing} menu item(s) lack recipes",
                    "message": "Theoretical usage and menu food cost are incomplete.",
                    "recommended_action": "Import or complete recipe ingredient quantities.",
                    "source_type": "recipe", "source_id": "missing", "source": {"missing": missing},
                })
            mobile = self._safe_rows(conn, "SELECT session_id,count_date,status FROM mobile_count_sessions WHERE status='Submitted' ORDER BY submitted_at DESC")
            for row in mobile[:5]:
                detected.append({
                    "key": f"mobile-count:{row['session_id']}", "severity": "Warning", "category": "Mobile Count",
                    "title": f"Mobile count awaiting manager finalization",
                    "message": f"Count dated {row['count_date']} has been submitted but not posted.",
                    "recommended_action": "Open Phase 2 Operations, review the mobile entries, and finalize the count.",
                    "source_type": "mobile_count", "source_id": row["session_id"], "source": dict(row),
                })
            draft_pos = self._safe_rows(conn, "SELECT po_id,vendor_name,subtotal,po_date FROM purchase_orders WHERE status='Draft' ORDER BY po_date DESC")
            if draft_pos:
                detected.append({
                    "key": "po:drafts", "severity": "Info", "category": "Purchase Orders",
                    "title": f"{len(draft_pos)} vendor purchase order draft(s) need review",
                    "message": "Draft POs were generated from manager order quantities and have not been approved.",
                    "recommended_action": "Review and approve or export the vendor purchase orders.",
                    "source_type": "purchase_order", "source_id": draft_pos[0]["po_id"], "source": [dict(r) for r in draft_pos[:20]],
                })
        if self.backup_due():
            latest = self.latest_backup()
            detected.append({
                "key": "backup:overdue", "severity": "Warning", "category": "Backup",
                "title": "Automatic backup is overdue",
                "message": f"Last complete backup: {latest['created_at'] if latest else 'none'}.",
                "recommended_action": "Create a backup from Security & Audit.",
                "source_type": "backup", "source_id": latest["backup_id"] if latest else "none", "source": json_safe(latest) if latest else {},
            })
        return detected

    def refresh_exceptions(self) -> list[sqlite3.Row]:
        detected = self._detected_exceptions()
        stamp = now_iso()
        active_keys = {row["key"] for row in detected}
        with self.workspace.connect() as conn:
            existing = {row["exception_key"]: row for row in conn.execute("SELECT * FROM operational_exceptions").fetchall()}
            for issue in detected:
                row = existing.get(issue["key"])
                if row:
                    status = row["status"] if row["status"] in {"Acknowledged", "Open"} else "Open"
                    conn.execute(
                        """UPDATE operational_exceptions SET severity=?,category=?,title=?,message=?,recommended_action=?,
                           source_type=?,source_id=?,source_json=?,status=?,last_detected=?,resolved_by=NULL,
                           resolved_at=NULL,resolution=NULL WHERE exception_key=?""",
                        (
                            issue["severity"], issue["category"], issue["title"], issue["message"], issue["recommended_action"],
                            issue["source_type"], issue["source_id"], json.dumps(json_safe(issue["source"])), status, stamp, issue["key"],
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO operational_exceptions(exception_key,severity,category,title,message,recommended_action,
                           source_type,source_id,source_json,status,first_detected,last_detected)
                           VALUES(?,?,?,?,?,?,?,?,?,'Open',?,?)""",
                        (
                            issue["key"], issue["severity"], issue["category"], issue["title"], issue["message"],
                            issue["recommended_action"], issue["source_type"], issue["source_id"],
                            json.dumps(json_safe(issue["source"])), stamp, stamp,
                        ),
                    )
            if active_keys:
                placeholders = ",".join("?" for _ in active_keys)
                conn.execute(
                    f"""UPDATE operational_exceptions SET status='Auto Resolved',resolved_by='system',resolved_at=?,
                        resolution='Underlying condition no longer detected.'
                        WHERE status IN ('Open','Acknowledged') AND exception_key NOT IN ({placeholders})""",
                    (stamp, *sorted(active_keys)),
                )
            else:
                conn.execute(
                    """UPDATE operational_exceptions SET status='Auto Resolved',resolved_by='system',resolved_at=?,
                       resolution='Underlying condition no longer detected.' WHERE status IN ('Open','Acknowledged')""",
                    (stamp,),
                )
        return self.list_exceptions()

    def list_exceptions(self, *, include_resolved: bool = False, limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM operational_exceptions"
        if not include_resolved:
            sql += " WHERE status IN ('Open','Acknowledged')"
        sql += " ORDER BY CASE severity WHEN 'Critical' THEN 1 WHEN 'Warning' THEN 2 ELSE 3 END,last_detected DESC LIMIT ?"
        with self.workspace.connect() as conn:
            return conn.execute(sql, (int(limit),)).fetchall()

    def set_exception_status(self, exception_id: int, status: str, resolution: str = "") -> None:
        self.require_permission("exceptions.manage")
        if status not in {"Open", "Acknowledged", "Resolved"}:
            raise OperationalControlsError("Unsupported exception status.")
        stamp = now_iso()
        username = self.current_user.username if self.current_user else "system"
        with self.workspace.connect() as conn:
            before = conn.execute("SELECT * FROM operational_exceptions WHERE exception_id=?", (int(exception_id),)).fetchone()
            if not before:
                raise OperationalControlsError("Exception not found.")
            if status == "Acknowledged":
                conn.execute(
                    "UPDATE operational_exceptions SET status=?,acknowledged_by=?,acknowledged_at=? WHERE exception_id=?",
                    (status, username, stamp, int(exception_id)),
                )
            elif status == "Resolved":
                conn.execute(
                    "UPDATE operational_exceptions SET status=?,resolved_by=?,resolved_at=?,resolution=? WHERE exception_id=?",
                    (status, username, stamp, resolution, int(exception_id)),
                )
            else:
                conn.execute(
                    "UPDATE operational_exceptions SET status='Open',resolved_by=NULL,resolved_at=NULL,resolution=NULL WHERE exception_id=?",
                    (int(exception_id),),
                )
            after = conn.execute("SELECT * FROM operational_exceptions WHERE exception_id=?", (int(exception_id),)).fetchone()
        self.audit("exception.status", "exception", str(exception_id), f"Changed exception to {status}", before=dict(before), after=dict(after))

    def data_quality_report(self, *, save_snapshot: bool = True) -> dict[str, Any]:
        exceptions = self.refresh_exceptions()
        counts = {"Critical": 0, "Warning": 0, "Info": 0}
        by_category: dict[str, int] = {}
        for row in exceptions:
            counts[row["severity"]] = counts.get(row["severity"], 0) + 1
            by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        with self.workspace.connect() as conn:
            item_total = conn.execute("SELECT COUNT(*) FROM items WHERE active=1").fetchone()[0]
            invoice_total = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            approved_invoices = conn.execute("SELECT COUNT(*) FROM invoices WHERE status='Approved'").fetchone()[0]
            item_setup = conn.execute(
                """SELECT COUNT(*) FROM items WHERE active=1 AND category!='Unclassified' AND count_unit IS NOT NULL
                   AND CAST(COALESCE(units_per_purchase_unit,'0') AS REAL)>0"""
            ).fetchone()[0]
            latest_sales = conn.execute("SELECT MAX(period_end) FROM sales").fetchone()[0]
            count_coverage = self._safe_rows(conn, """SELECT COUNT(DISTINCT c.item_id) AS n FROM inventory_counts c
                JOIN items i ON i.item_id=c.item_id WHERE c.finalized=1 AND i.active=1
                AND c.count_date>=date('now','-45 day')""")
            recent_counts = int(count_coverage[0]["n"]) if count_coverage else 0
            receiving_total = conn.execute("SELECT COUNT(*) FROM invoices WHERE status='Approved'").fetchone()[0]
            receiving_verified = conn.execute("SELECT COUNT(*) FROM receiving_sessions WHERE status='Verified'").fetchone()[0]
            try:
                menu_total = conn.execute("SELECT COUNT(*) FROM menu_items WHERE active=1").fetchone()[0]
                recipes_configured = conn.execute("SELECT COUNT(DISTINCT menu_item_id) FROM recipe_ingredients").fetchone()[0]
                latest_pos_import = conn.execute("SELECT MAX(imported_at) FROM pos_import_runs WHERE status='Imported'").fetchone()[0]
                submitted_mobile_counts = conn.execute("SELECT COUNT(*) FROM mobile_count_sessions WHERE status='Submitted'").fetchone()[0]
            except sqlite3.Error:
                menu_total = recipes_configured = submitted_mobile_counts = 0
                latest_pos_import = None
        completeness = 100
        if item_total:
            inventory_component = round(60 * item_setup / item_total + 40 * recent_counts / item_total)
            if menu_total:
                recipe_component = round(100 * recipes_configured / menu_total)
                completeness = round(inventory_component * 0.75 + recipe_component * 0.25)
            else:
                completeness = inventory_component
        elif invoice_total == 0:
            completeness = 20
        freshness = 100
        if not latest_sales:
            freshness -= 45
        else:
            try:
                sales_age = (date.today() - date.fromisoformat(latest_sales)).days
                freshness -= min(45, max(0, sales_age - 2) * 5)
            except ValueError:
                freshness -= 45
        if self.backup_due():
            freshness -= 25
        if menu_total and not latest_pos_import:
            freshness -= 20
        elif latest_pos_import:
            try:
                pos_age = (datetime.now() - datetime.fromisoformat(latest_pos_import)).days
                freshness -= min(20, max(0, pos_age - 2) * 2)
            except ValueError:
                freshness -= 20
        integrity = 100
        if invoice_total:
            integrity -= round(45 * max(0, invoice_total - approved_invoices) / invoice_total)
        integrity -= min(35, counts.get("Critical", 0) * 8)
        operational = 100
        if receiving_total:
            operational -= round(35 * max(0, receiving_total - receiving_verified) / receiving_total)
        operational -= min(45, counts.get("Warning", 0) * 3)
        operational -= min(20, int(submitted_mobile_counts) * 5)
        component_scores = [max(0, min(100, int(v))) for v in (completeness, freshness, integrity, operational)]
        overall = round(sum(component_scores) / 4)
        grade = "Excellent" if overall >= 90 else "Good" if overall >= 75 else "Needs Attention" if overall >= 55 else "High Risk"
        report = {
            "generated_at": now_iso(), "overall_score": overall, "grade": grade,
            "completeness_score": component_scores[0], "freshness_score": component_scores[1],
            "integrity_score": component_scores[2], "operational_score": component_scores[3],
            "open_exceptions": len(exceptions), "severity_counts": counts, "category_counts": by_category,
            "metrics": {
                "items": item_total, "items_configured": item_setup, "recently_counted_items": recent_counts,
                "invoices": invoice_total, "approved_invoices": approved_invoices,
                "latest_sales_period": latest_sales, "receiving_verified": receiving_verified,
                "receiving_expected": receiving_total, "menu_items": menu_total,
                "recipes_configured": recipes_configured, "latest_pos_import": latest_pos_import,
                "submitted_mobile_counts": submitted_mobile_counts,
            },
            "issues": [dict(row) for row in exceptions[:100]],
        }
        if save_snapshot:
            with self.workspace.connect() as conn:
                conn.execute(
                    """INSERT INTO data_quality_snapshots(created_at,overall_score,completeness_score,
                       freshness_score,integrity_score,operational_score,grade,details_json)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        report["generated_at"], overall, component_scores[0], component_scores[1],
                        component_scores[2], component_scores[3], grade, json.dumps(json_safe(report)),
                    ),
                )
        return report
