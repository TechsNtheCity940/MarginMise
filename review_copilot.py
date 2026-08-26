#!/usr/bin/env python3
"""Deterministic CostPilot review-center automation for MarginMise.

This module powers the conversational exception-review panel.  It explains
invoice and receiving problems in plain language, previews safe actions, and
executes only explicit, permission-checked manager commands.  Natural-language
explanation may be supplemented elsewhere, but all writes here are governed by
local rules and audit history.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from operational_controls import AuthenticatedUser, OperationalControlsError, PermissionDenied

MONEY = Decimal("0.01")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def money(value: Any) -> Decimal:
    return dec(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, sqlite3.Row):
        return {key: json_safe(value[key]) for key in value.keys()}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


REVIEW_COPILOT_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

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
"""


@dataclass(frozen=True)
class ReviewCommand:
    action: str
    case_ids: list[str]
    confirmation_title: str
    confirmation_message: str
    immediate_reply: str = ""


class ReviewCopilotService:
    """Unifies invoice and receiving exceptions into one review queue."""

    def __init__(self, workspace: Any, pipeline: Any, controls: Any):
        self.workspace = workspace
        self.pipeline = pipeline
        self.controls = controls
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.workspace.connect() as conn:
            conn.executescript(REVIEW_COPILOT_SCHEMA_SQL)

    # ------------------------------------------------------------------
    # Queue construction
    # ------------------------------------------------------------------
    @staticmethod
    def _case_id(case_type: str, entity_id: str) -> str:
        prefix = {
            "invoice": "INV",
            "receiving": "RECV",
            "auto_upload": "UPLOAD",
        }.get(case_type, case_type.upper())
        return f"{prefix}:{entity_id}"

    @staticmethod
    def split_case_id(case_id: str) -> tuple[str, str]:
        prefix, _, entity_id = str(case_id).partition(":")
        mapping = {"INV": "invoice", "RECV": "receiving", "UPLOAD": "auto_upload"}
        if prefix not in mapping or not entity_id:
            raise OperationalControlsError(f"Unsupported review case: {case_id}")
        return mapping[prefix], entity_id

    def _invoice_review_rows(self) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute(
                """SELECT i.invoice_id,i.vendor,i.invoice_number,i.invoice_date,i.total,i.status,
                          i.source_name,i.source_original_path,i.source_archive_path,
                          i.source_sha256,i.duplicate_key,i.extraction_method,
                          i.extraction_confidence,i.canonical_json,i.notes,i.created_at,
                          COUNT(r.review_id) AS issue_count,
                          GROUP_CONCAT(DISTINCT r.issue_type) AS issue_types,
                          GROUP_CONCAT(r.issue, ' || ') AS issues
                   FROM invoices i
                   JOIN reviews r ON r.invoice_id=i.invoice_id AND r.status='Open'
                   WHERE i.status='Needs Review'
                   GROUP BY i.invoice_id
                   ORDER BY i.created_at DESC"""
            ).fetchall()

    def _receiving_review_rows(self) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            return conn.execute(
                """SELECT s.*,i.total,i.source_name,i.source_original_path
                   FROM receiving_sessions s
                   JOIN invoices i ON i.invoice_id=s.invoice_id
                   LEFT JOIN costpilot_review_resolutions c
                     ON c.case_type='receiving' AND c.case_id=s.session_id
                   WHERE s.status='Needs Review'
                     AND COALESCE(c.resolution_status,'Open') NOT IN ('Resolved','Credit Pending','Replacement Pending')
                   ORDER BY COALESCE(s.received_date,s.invoice_date) DESC,s.created_at DESC"""
            ).fetchall()

    def _auto_upload_review_rows(self) -> list[sqlite3.Row]:
        with self.workspace.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_upload_events'"
            ).fetchone()
            if not exists:
                return []
            return conn.execute(
                """SELECT event.*
                   FROM auto_upload_events AS event
                   WHERE event.status IN ('Needs Review','Failed')
                     AND event.event_id = (
                         SELECT MAX(newer.event_id)
                         FROM auto_upload_events AS newer
                         WHERE newer.source_sha256=event.source_sha256
                     )
                   ORDER BY event.completed_at DESC,event.event_id DESC"""
            ).fetchall()

    @staticmethod
    def _canonical(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        try:
            value = row["canonical_json"]
        except Exception:
            value = None
        if not value:
            return {}
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _duplicate_family(self, row: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT invoice_id,status,vendor,invoice_number,invoice_date,total,created_at
                   FROM invoices
                   WHERE (source_sha256=? AND source_sha256<>'')
                      OR (duplicate_key=? AND duplicate_key<>'')
                   ORDER BY CASE status WHEN 'Approved' THEN 0 WHEN 'Rejected' THEN 2 ELSE 1 END,
                            created_at""",
                (str(row["source_sha256"] or ""), str(row["duplicate_key"] or "")),
            ).fetchall()
        return [dict(item) for item in rows]

    @staticmethod
    def _text_contains(text: str, *terms: str) -> bool:
        lowered = str(text or "").lower()
        return any(term.lower() in lowered for term in terms)

    def _invoice_issue_code(self, row: sqlite3.Row, canonical: dict[str, Any], family: list[dict[str, Any]]) -> str:
        issues = f"{row['issue_types'] or ''} {row['issues'] or ''} {row['notes'] or ''}".lower()
        raw = str(canonical.get("_raw_text") or "").strip()
        items = canonical.get("items") if isinstance(canonical.get("items"), list) else []
        extraction_failed = str(row["extraction_method"] or "") == "local-extraction-failed"
        unreadable = extraction_failed or (
            not items
            and len(re.sub(r"\s+", "", raw)) < 40
            and not str(row["invoice_number"] or "").strip()
            and not str(row["invoice_date"] or "").strip()
        )
        if unreadable:
            return "unreadable_document"
        if len(family) > 1 or "duplicate" in issues:
            return "duplicate_document"
        missing = []
        if not str(row["invoice_number"] or "").strip():
            missing.append("invoice number")
        if not str(row["invoice_date"] or "").strip():
            missing.append("invoice date")
        if missing:
            return "missing_header"
        if any(term in issues for term in ("subtotal", "total", "arithmetic", "line total", "quantity", "unit price")):
            return "arithmetic_mismatch"
        if any(term in issues for term in (
            "missing invoice number",
            "missing invoice date",
            "required header",
            "required field",
            "header information is missing",
        )):
            return "missing_header"
        if any(term in issues for term in ("new item", "new product", "category")):
            return "new_item"
        if "vendor" in issues or "layout" in issues:
            return "vendor_layout"
        if float(row["extraction_confidence"] or 0) < 0.92:
            return "low_confidence"
        return "invoice_exception"

    @staticmethod
    def _invoice_problem(code: str, row: sqlite3.Row, family: list[dict[str, Any]]) -> tuple[str, str, str, str]:
        issue_text = str(row["issues"] or "").strip()
        if code == "unreadable_document":
            return (
                "Unreadable document",
                "CostPilot could not obtain enough reliable text or line-item data to validate this document.",
                "Reject unreadable copy and related duplicates, then request or rescan a clearer document.",
                "Critical",
            )
        if code == "duplicate_document":
            approved = [item for item in family if item.get("status") == "Approved"]
            original = approved[0].get("invoice_id") if approved else "the earliest retained record"
            return (
                "Possible duplicate",
                f"This document belongs to a duplicate family containing {len(family)} stored record(s). The retained original is {original}.",
                "Reject the review copies while preserving any approved original.",
                "Critical",
            )
        if code == "missing_header":
            missing = []
            if not str(row["invoice_number"] or "").strip():
                missing.append("invoice number")
            if not str(row["invoice_date"] or "").strip():
                missing.append("invoice date")
            label = " and ".join(missing) or "required header information"
            return (
                "Missing invoice header",
                f"The {label} is missing or was not trusted during extraction.",
                "Reread the saved raw extraction and approve automatically only if every validation passes.",
                "Warning",
            )
        if code == "arithmetic_mismatch":
            return (
                "Invoice arithmetic mismatch",
                issue_text or "A line amount or invoice total does not reconcile.",
                "Open the invoice, compare the image with extracted values, and correct the mismatched field before approval.",
                "Critical",
            )
        if code == "new_item":
            return (
                "New product needs classification",
                issue_text or "The invoice contains a product that is not fully configured.",
                "Confirm the product name, category, purchase unit, and count conversion, then approve the invoice.",
                "Warning",
            )
        if code == "vendor_layout":
            return (
                "Vendor or layout needs confirmation",
                issue_text or "This vendor layout has not yet earned automatic approval.",
                "Confirm the extracted values once; the layout can then be learned for future invoices.",
                "Warning",
            )
        if code == "low_confidence":
            return (
                "Low extraction confidence",
                issue_text or "The extracted values did not meet the configured automatic-approval confidence threshold.",
                "Reread the raw extraction and approve only if all required values and arithmetic validate.",
                "Warning",
            )
        return (
            "Invoice needs review",
            issue_text or "One or more invoice checks require a manager decision.",
            "Open the invoice to correct the remaining issue, or use CostPilot's recommended safe action.",
            "Warning",
        )

    @staticmethod
    def _linked_invoice_ids(row: sqlite3.Row | dict[str, Any]) -> set[str]:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except Exception:
            return set()
        outcome = details.get("outcome") if isinstance(details, dict) else {}
        outcome_details = outcome.get("details") if isinstance(outcome, dict) else {}
        results = outcome_details.get("results") if isinstance(outcome_details, dict) else []
        if not isinstance(results, list):
            return set()
        return {
            str(result.get("invoice_id"))
            for result in results
            if isinstance(result, dict) and str(result.get("invoice_id") or "").strip()
        }

    def _invoice_case(
        self,
        row: sqlite3.Row,
        upload_row: sqlite3.Row | None = None,
    ) -> dict[str, Any]:
        canonical = self._canonical(row)
        family = self._duplicate_family(row)
        code = self._invoice_issue_code(row, canonical, family)
        title, explanation, recommendation, severity = self._invoice_problem(code, row, family)
        vendor = str(row["vendor"] or "Unknown vendor")
        number = str(row["invoice_number"] or row["source_name"] or row["invoice_id"])
        eligible_actions = ["open"]
        if code in {"missing_header", "low_confidence", "vendor_layout", "invoice_exception"}:
            eligible_actions += ["recover_and_approve", "reject_selected"]
        elif code in {"unreadable_document", "duplicate_document"}:
            eligible_actions += ["reject_unreadable_duplicates", "reject_selected"]
        elif code in {"arithmetic_mismatch", "new_item"}:
            eligible_actions += ["reject_selected"]
        evidence = {
            "extraction_method": str(row["extraction_method"] or ""),
            "extraction_confidence": float(row["extraction_confidence"] or 0),
            "invoice_number": str(row["invoice_number"] or ""),
            "invoice_date": str(row["invoice_date"] or ""),
            "raw_text_available": bool(str(canonical.get("_raw_text") or "").strip()),
            "line_count": len(canonical.get("items") or []),
            "duplicate_family_count": len(family),
        }
        if upload_row is not None:
            upload_evidence = self._auto_upload_case(upload_row)["evidence"]
            evidence["auto_upload"] = {
                "event_id": upload_evidence["event_id"],
                "workbook": upload_evidence["workbook"],
                "detected_type": upload_evidence["detected_type"],
                "classification_confidence": upload_evidence["classification_confidence"],
                "upload_status": upload_evidence["upload_status"],
                "summary": upload_evidence["summary"],
                "errors": upload_evidence["errors"],
                "archived_path": upload_evidence["archived_path"],
            }
        return {
            "case_id": self._case_id("invoice", str(row["invoice_id"])),
            "case_type": "invoice",
            "entity_id": str(row["invoice_id"]),
            "document_id": str(row["invoice_id"]),
            "vendor": vendor,
            "document_label": f"{vendor} · {number}",
            "date": str(row["invoice_date"] or ""),
            "amount": f"{money(row['total']):.2f}",
            "issue_code": code,
            "problem": title,
            "explanation": explanation,
            "recommendation": recommendation,
            "severity": severity,
            "status": "Needs Review",
            "issue_count": int(row["issue_count"] or 0),
            "issue_types": str(row["issue_types"] or ""),
            "issues": str(row["issues"] or ""),
            "source_path": str(row["source_archive_path"] or row["source_original_path"] or ""),
            "duplicate_family": family,
            "eligible_actions": eligible_actions,
            "recommended_action": (
                "reject_unreadable_duplicates" if code in {"unreadable_document", "duplicate_document"}
                else "recover_and_approve" if code in {"missing_header", "low_confidence", "vendor_layout", "invoice_exception"}
                else "open"
            ),
            "batch_key": f"invoice:{code}",
            "evidence": evidence,
        }

    def _receiving_lines(self, session_id: str) -> list[dict[str, Any]]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT r.*,i.category
                   FROM receiving_lines r
                   LEFT JOIN items i ON i.item_id=r.item_id
                   WHERE r.session_id=?
                   ORDER BY r.receiving_line_id""",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _receiving_issue(lines: list[dict[str, Any]]) -> tuple[str, str, str, str, str]:
        statuses = Counter(str(line.get("line_status") or "Received") for line in lines)
        discrepancy_lines = [line for line in lines if str(line.get("line_status") or "Received") != "Received"]
        if statuses.get("Short") and len(statuses) <= 2:
            shortage = sum(max(Decimal("0"), dec(line.get("expected_quantity")) - dec(line.get("received_quantity"))) for line in discrepancy_lines if line.get("line_status") == "Short")
            return (
                "receiving_shortage", "Received less than invoiced",
                f"The delivery is short by {shortage.normalize()} unit(s) across {statuses['Short']} line(s).",
                "Record the shortage, calculate the expected vendor credit, and close the manager review without changing the received quantities.",
                "Critical",
            )
        if statuses.get("Damaged") or statuses.get("Rejected") or statuses.get("Not Received"):
            affected = statuses.get("Damaged", 0) + statuses.get("Rejected", 0) + statuses.get("Not Received", 0)
            return (
                "receiving_damage_or_rejection", "Damaged, rejected, or missing delivery",
                f"{affected} delivery line(s) require a vendor credit or replacement record.",
                "Preserve the discrepancy, estimate the vendor credit, and mark the manager review as resolved with follow-up pending.",
                "Critical",
            )
        if statuses.get("Substituted"):
            return (
                "receiving_substitution", "Product substitution",
                f"{statuses['Substituted']} delivered line(s) were substituted.",
                "Confirm that the substitution was accepted, preserve the replacement description, and close the manager review.",
                "Warning",
            )
        return (
            "receiving_mixed", "Receiving discrepancy",
            f"{len(discrepancy_lines)} delivery line(s) differ from the invoice.",
            "Preserve every line-level discrepancy, estimate credits where appropriate, and close only the manager review layer.",
            "Critical",
        )

    def _receiving_case(self, row: sqlite3.Row) -> dict[str, Any]:
        lines = self._receiving_lines(str(row["session_id"]))
        code, title, explanation, recommendation, severity = self._receiving_issue(lines)
        discrepancies = [line for line in lines if str(line.get("line_status") or "Received") != "Received"]
        expected_credit = sum(dec(line.get("credit_expected")) for line in discrepancies)
        return {
            "case_id": self._case_id("receiving", str(row["session_id"])),
            "case_type": "receiving",
            "entity_id": str(row["session_id"]),
            "document_id": str(row["invoice_id"]),
            "vendor": str(row["vendor"] or "Unknown vendor"),
            "document_label": f"{row['vendor'] or 'Unknown vendor'} · {row['invoice_number'] or row['invoice_id']}",
            "date": str(row["received_date"] or row["invoice_date"] or ""),
            "amount": f"{money(row['total']):.2f}",
            "issue_code": code,
            "problem": title,
            "explanation": explanation,
            "recommendation": recommendation,
            "severity": severity,
            "status": "Needs Review",
            "issue_count": len(discrepancies),
            "issue_types": ", ".join(sorted({str(line.get("line_status") or "") for line in discrepancies})),
            "issues": " || ".join(
                f"{line.get('description')}: expected {line.get('expected_quantity')}, received {line.get('received_quantity')} ({line.get('line_status')})"
                for line in discrepancies
            ),
            "source_path": str(row["source_original_path"] or ""),
            "duplicate_family": [],
            "eligible_actions": ["open", "resolve_receiving"],
            "recommended_action": "resolve_receiving",
            "batch_key": f"receiving:{code}",
            "evidence": {
                "session_id": str(row["session_id"]),
                "invoice_id": str(row["invoice_id"]),
                "discrepancy_count": int(row["discrepancy_count"] or 0),
                "expected_value": str(row["expected_value"] or "0"),
                "received_value": str(row["received_value"] or "0"),
                "existing_expected_credit": f"{money(expected_credit):.2f}",
                "lines": discrepancies,
            },
        }

    def _auto_upload_case(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except Exception:
            details = {}
        outcome = details.get("outcome") if isinstance(details, dict) else {}
        outcome = outcome if isinstance(outcome, dict) else {}
        outcome_details = outcome.get("details")
        outcome_details = outcome_details if isinstance(outcome_details, dict) else {}
        errors = outcome_details.get("errors")
        if not isinstance(errors, list):
            error = outcome_details.get("error")
            errors = [str(error)] if error else []
        errors = [str(error) for error in errors if str(error).strip()]
        failed = str(row["status"]) == "Failed"
        issue_count = int(outcome.get("rejected") or 0) or len(errors) or 1
        explanation = str(row["summary"] or "").strip()
        if errors:
            explanation += " " + " | ".join(errors[:3])
        return {
            "case_id": self._case_id("auto_upload", str(row["event_id"])),
            "case_type": "auto_upload",
            "entity_id": str(row["event_id"]),
            "document_id": str(row["event_id"]),
            "vendor": "Auto Upload",
            "document_label": str(row["original_name"]),
            "date": str(row["completed_at"] or ""),
            "amount": "0.00",
            "issue_code": "auto_upload_failed" if failed else "auto_upload_needs_review",
            "problem": "Automatic upload failed" if failed else "Automatic upload needs review",
            "explanation": explanation or "The workbook could not be imported safely.",
            "recommendation": (
                "Review the exact workbook and row errors, resolve missing dependencies, "
                "then retry the preserved original."
            ),
            "severity": "Critical" if failed else "Warning",
            "status": str(row["status"]),
            "issue_count": issue_count,
            "issue_types": str(row["detected_type"]),
            "issues": " || ".join(errors) or str(row["summary"] or ""),
            "source_path": str(row["archived_path"] or ""),
            "duplicate_family": [],
            "eligible_actions": ["open", "retry_upload"],
            "recommended_action": "retry_upload",
            "batch_key": f"auto_upload:{row['detected_type']}:{row['status']}",
            "evidence": {
                "event_id": int(row["event_id"]),
                "workbook": str(row["original_name"]),
                "detected_type": str(row["detected_type"]),
                "classification_confidence": float(row["classification_confidence"] or 0),
                "upload_status": str(row["status"]),
                "summary": str(row["summary"] or ""),
                "errors": errors[:100],
                "archived_path": str(row["archived_path"] or ""),
                "details": details,
            },
        }

    def _require_review_access(self) -> None:
        user = self.controls.current_user
        if user is None:
            raise PermissionDenied("Sign in to continue.")
        if not any(user.can(permission) for permission in (
            "reviews.center", "invoices.review", "receiving.verify", "exceptions.view", "exceptions.manage"
        )):
            raise PermissionDenied(f"{user.role} does not have access to CostPilot Review Center.")

    def list_cases(self) -> list[dict[str, Any]]:
        self._require_review_access()
        invoice_rows = self._invoice_review_rows()
        upload_rows = self._auto_upload_review_rows()
        upload_by_invoice: dict[str, sqlite3.Row] = {}
        for upload_row in upload_rows:
            for invoice_id in self._linked_invoice_ids(upload_row):
                upload_by_invoice[invoice_id] = upload_row
        active_invoice_ids = {str(row["invoice_id"]) for row in invoice_rows}
        cases = [
            self._invoice_case(row, upload_by_invoice.get(str(row["invoice_id"])))
            for row in invoice_rows
        ]
        cases.extend(self._receiving_case(row) for row in self._receiving_review_rows())
        cases.extend(
            self._auto_upload_case(row)
            for row in upload_rows
            if not (self._linked_invoice_ids(row) & active_invoice_ids)
        )
        severity_rank = {"Critical": 0, "Warning": 1, "Info": 2}
        cases.sort(key=lambda case: (severity_rank.get(case["severity"], 9), case["batch_key"], case["document_label"]))
        return cases

    def get_case(self, case_id: str) -> dict[str, Any]:
        for case in self.list_cases():
            if case["case_id"] == case_id:
                return case
        raise OperationalControlsError(f"Review case is no longer open: {case_id}")

    def summary(self) -> dict[str, Any]:
        cases = self.list_cases()
        by_type = Counter(case["case_type"] for case in cases)
        by_issue = Counter(case["issue_code"] for case in cases)
        return {
            "open": len(cases),
            "invoice_cases": by_type["invoice"],
            "receiving_cases": by_type["receiving"],
            "auto_upload_cases": by_type["auto_upload"],
            "critical": sum(1 for case in cases if case["severity"] == "Critical"),
            "by_issue": dict(by_issue),
        }

    def queue_introduction(self) -> str:
        summary = self.summary()
        if not summary["open"]:
            return "The review queue is clear. CostPilot found no invoice, receiving, or Auto Upload exceptions requiring manager attention."
        groups = []
        labels = {
            "unreadable_document": "unreadable documents",
            "duplicate_document": "possible duplicates",
            "missing_header": "missing invoice headers",
            "arithmetic_mismatch": "arithmetic mismatches",
            "receiving_shortage": "receiving shortages",
            "receiving_damage_or_rejection": "damaged or rejected delivery records",
            "receiving_substitution": "substitutions",
            "auto_upload_needs_review": "Auto Upload files needing review",
            "auto_upload_failed": "failed Auto Upload files",
        }
        for code, count in sorted(summary["by_issue"].items(), key=lambda pair: (-pair[1], pair[0])):
            groups.append(f"{count} {labels.get(code, code.replace('_', ' '))}")
        return (
            f"I found {summary['open']} review case(s): {summary['invoice_cases']} invoice case(s) and "
            f"{summary['receiving_cases']} receiving case(s), and "
            f"{summary['auto_upload_cases']} Auto Upload case(s). " + "; ".join(groups) + ". "
            "Select a case for a plain-language explanation, or type a batch command such as “approve all eligible”, "
            "“reject unreadable and duplicates”, or “fix selected”."
        )

    def explain_case(self, case_id: str) -> str:
        case = self.get_case(case_id)
        evidence = case["evidence"]
        lines = [
            f"{case['problem']} — {case['document_label']}",
            case["explanation"],
            f"Recommended action: {case['recommendation']}",
        ]
        if case["case_type"] == "invoice":
            lines.append(
                f"Evidence: extraction method {evidence.get('extraction_method') or 'unknown'}, "
                f"confidence {float(evidence.get('extraction_confidence') or 0):.0%}, "
                f"{evidence.get('line_count', 0)} extracted line(s), "
                f"duplicate family {evidence.get('duplicate_family_count', 1)} stored record(s)."
            )
            if case.get("issues"):
                lines.append(f"Open findings: {case['issues']}")
        elif case["case_type"] == "receiving":
            lines.append(
                f"Evidence: {evidence.get('discrepancy_count', 0)} discrepancy line(s), "
                f"invoice value ${money(evidence.get('expected_value')):,.2f}, "
                f"received value ${money(evidence.get('received_value')):,.2f}, "
                f"currently recorded expected credit ${money(evidence.get('existing_expected_credit')):,.2f}."
            )
            if case.get("issues"):
                lines.append(f"Line details: {case['issues']}")
        else:
            lines.append(
                f"Evidence: workbook {evidence.get('workbook') or case['document_label']}, "
                f"detected as {evidence.get('detected_type') or 'unknown'}, "
                f"classification confidence {float(evidence.get('classification_confidence') or 0):.0%}, "
                f"status {evidence.get('upload_status') or case['status']}."
            )
            if evidence.get("errors"):
                lines.append("Workbook/row errors: " + " | ".join(evidence["errors"][:10]))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Command parsing and previews
    # ------------------------------------------------------------------
    def parse_command(self, text: str, selected_case_ids: Iterable[str] = ()) -> ReviewCommand | None:
        q = re.sub(r"\s+", " ", str(text or "").strip().lower())
        selected = [str(value) for value in selected_case_ids]
        if not q:
            return None
        if q in {"summary", "what needs review", "review summary", "show summary"}:
            return ReviewCommand("summary", [], "", "", self.queue_introduction())
        if q in {"next", "next case", "explain next"}:
            return ReviewCommand("next", selected, "", "", "")
        if "explain" in q or "what is wrong" in q or "what's wrong" in q:
            return ReviewCommand("explain", selected, "", "", "")
        if "retry" in q and any(term in q for term in ("upload", "workbook", "file", "selected")):
            return ReviewCommand(
                "retry_upload", selected, "Retry selected Auto Upload files",
                "MarginMise will copy each preserved unresolved workbook back to its restaurant upload folder and run deterministic classification and validation again. Continue?",
            )
        if ("approve all" in q) or ("approve everything eligible" in q):
            return ReviewCommand(
                "approve_all_eligible", [], "Approve all eligible invoices",
                "CostPilot will reread every open invoice and approve only those that pass all required fields, line arithmetic, header arithmetic, confidence, and duplicate checks. Receiving discrepancies will not be erased. Continue?",
            )
        if "approve" in q and "selected" in q:
            return ReviewCommand(
                "recover_and_approve", selected, "Approve selected eligible invoices",
                "CostPilot will reread and approve only selected invoice cases that pass every deterministic validation. Continue?",
            )
        if "reject unreadable" in q or ("reject" in q and "duplicate" in q):
            return ReviewCommand(
                "reject_unreadable_duplicates", selected, "Reject unreadable and duplicate documents",
                "CostPilot will reject unreadable or duplicate invoice review copies while preserving approved originals and all audit records. Continue?",
            )
        if "reject all" in q:
            return ReviewCommand(
                "reject_all_documents", [], "Reject all invoice documents in review",
                "This rejects every invoice currently in the review queue. Receiving discrepancy records are preserved. This cannot be undone from the review panel. Continue?",
            )
        if "reject" in q and "selected" in q:
            return ReviewCommand(
                "reject_selected", selected, "Reject selected invoice documents",
                "This rejects the selected invoice review documents and resolves their open invoice findings. Receiving discrepancy records are preserved. Continue?",
            )
        if "shortage" in q or "receiving" in q and any(word in q for word in ("resolve", "fix", "record")):
            target_ids = selected
            if not target_ids and "shortage" in q:
                target_ids = [
                    case["case_id"] for case in self.list_cases()
                    if case["issue_code"] == "receiving_shortage"
                ]
            return ReviewCommand(
                "resolve_receiving", target_ids, "Resolve receiving review cases",
                "CostPilot will preserve all delivered quantities and discrepancy statuses, calculate missing expected credits where possible, and close only the manager-review layer. Continue?",
            )
        if any(phrase in q for phrase in ("fix selected", "process selected", "apply recommended")):
            return ReviewCommand(
                "apply_recommended", selected, "Apply recommended actions",
                "CostPilot will apply the safe recommended action to each selected case. Arithmetic mismatches and product-setup issues will remain open for manual correction. Continue?",
            )
        if any(phrase in q for phrase in ("fix all", "process all", "apply all recommended")):
            return ReviewCommand(
                "apply_all_recommended", [], "Apply all safe recommended actions",
                "CostPilot will process recoverable invoices, reject unreadable or duplicate review copies, and log receiving discrepancies without changing what was actually delivered. Manual-only cases will remain open. Continue?",
            )
        return None

    def preview(self, action: str, case_ids: Iterable[str] | None = None) -> dict[str, Any]:
        cases = self.list_cases()
        by_id = {case["case_id"]: case for case in cases}
        requested = list(by_id) if case_ids is None else [str(value) for value in case_ids]
        selected = [by_id[value] for value in requested if value in by_id]
        if action == "approve_all_eligible":
            selected = [case for case in cases if case["case_type"] == "invoice"]
        elif action == "reject_all_documents":
            selected = [case for case in cases if case["case_type"] == "invoice"]
        elif action == "apply_all_recommended":
            selected = list(cases)
        requested = [case["case_id"] for case in selected]
        eligible = []
        skipped = []
        user = self.controls.current_user
        can_invoice = bool(user and user.can("invoices.review"))
        can_receiving = bool(user and user.can("receiving.verify"))
        can_upload = bool(user and (
            user.can("invoices.process")
            or user.can("pos.import")
            or user.can("settings.manage")
        ))
        for case in selected:
            allowed = action in case["eligible_actions"]
            if action in {"approve_all_eligible", "recover_and_approve"}:
                allowed = case["case_type"] == "invoice" and case["issue_code"] not in {"unreadable_document", "duplicate_document", "arithmetic_mismatch", "new_item"}
            elif action == "reject_all_documents":
                allowed = case["case_type"] == "invoice"
            elif action == "reject_selected":
                allowed = case["case_type"] == "invoice"
            elif action == "reject_unreadable_duplicates":
                allowed = case["case_type"] == "invoice" and case["issue_code"] in {"unreadable_document", "duplicate_document"}
            elif action == "resolve_receiving":
                allowed = case["case_type"] == "receiving"
            elif action == "retry_upload":
                allowed = case["case_type"] == "auto_upload"
            elif action in {"apply_recommended", "apply_all_recommended"}:
                allowed = case["recommended_action"] in {
                    "recover_and_approve", "reject_unreadable_duplicates",
                    "resolve_receiving", "retry_upload",
                }
            if case["case_type"] == "invoice" and not can_invoice:
                allowed = False
            if case["case_type"] == "receiving" and not can_receiving:
                allowed = False
            if case["case_type"] == "auto_upload" and not can_upload:
                allowed = False
            (eligible if allowed else skipped).append(case)
        return {
            "action": action,
            "requested": requested,
            "eligible": eligible,
            "skipped": skipped,
            "eligible_count": len(eligible),
            "skipped_count": len(skipped),
        }

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------
    def _actor(self, actor: AuthenticatedUser | None = None) -> AuthenticatedUser | None:
        return actor or self.controls.current_user

    def _record_action(
        self,
        action_code: str,
        requested: list[str],
        affected: list[str],
        skipped: list[str],
        status: str,
        summary: str,
        details: dict[str, Any],
        actor: AuthenticatedUser | None,
    ) -> str:
        user = self._actor(actor)
        action_id = f"CPRA-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"
        with self.workspace.connect() as conn:
            conn.execute(
                """INSERT INTO costpilot_review_actions(
                       action_id,created_at,created_by,created_by_role,action_code,
                       action_scope,case_count,requested_case_ids_json,
                       affected_case_ids_json,skipped_case_ids_json,result_status,
                       summary,details_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id, now_iso(), user.username if user else "system",
                    user.role if user else "System", action_code,
                    "batch" if len(requested) != 1 else "single", len(affected),
                    json.dumps(requested), json.dumps(affected), json.dumps(skipped),
                    status, summary, json.dumps(json_safe(details)),
                ),
            )
        self.controls.audit(
            f"costpilot.review.{action_code}", "review_batch", action_id, summary,
            details={"requested": requested, "affected": affected, "skipped": skipped, **details},
            actor=user,
        )
        return action_id

    def _reject_invoice_ids(self, invoice_ids: Iterable[str], reason: str, actor: AuthenticatedUser | None) -> list[str]:
        self.controls.require_permission("invoices.review", actor)
        affected = []
        for invoice_id in dict.fromkeys(str(value) for value in invoice_ids):
            row = self.pipeline.get_invoice(invoice_id)
            if not row or row["status"] != "Needs Review":
                continue
            self.pipeline.reject_review(invoice_id, reason)
            affected.append(self._case_id("invoice", invoice_id))
            self.controls.audit(
                "invoice.reject.costpilot", "invoice", invoice_id,
                f"CostPilot-assisted rejection: {reason}", actor=actor,
            )
        return affected

    def _duplicate_review_ids(self, case: dict[str, Any]) -> list[str]:
        ids = []
        for row in case.get("duplicate_family") or []:
            if row.get("status") == "Needs Review":
                ids.append(str(row.get("invoice_id")))
        if case["entity_id"] not in ids:
            ids.append(case["entity_id"])
        return list(dict.fromkeys(ids))

    def _estimate_receiving_credit(self, line: dict[str, Any]) -> Decimal:
        existing = money(line.get("credit_expected"))
        if existing > 0:
            return existing
        status = str(line.get("line_status") or "")
        expected = max(Decimal("0"), dec(line.get("expected_quantity")))
        received = max(Decimal("0"), dec(line.get("received_quantity")))
        price = max(Decimal("0"), dec(line.get("unit_price")))
        if status in {"Short", "Not Received"}:
            return money(max(Decimal("0"), expected - received) * price)
        if status in {"Damaged", "Rejected"}:
            return money((received if received > 0 else expected) * price)
        return Decimal("0.00")

    def _resolve_receiving_case(self, case: dict[str, Any], actor: AuthenticatedUser | None) -> dict[str, Any]:
        self.controls.require_permission("receiving.verify", actor)
        session_id = case["entity_id"]
        session, rows = self.pipeline.get_receiving(session_id)
        lines = [dict(row) for row in rows]
        total_credit = Decimal("0.00")
        changed_lines = []
        with self.workspace.connect() as conn:
            for line in lines:
                if str(line.get("line_status") or "Received") == "Received":
                    continue
                estimated = self._estimate_receiving_credit(line)
                total_credit += estimated
                if money(line.get("credit_expected")) <= 0 and estimated > 0:
                    conn.execute(
                        "UPDATE receiving_lines SET credit_expected=? WHERE receiving_line_id=?",
                        (f"{estimated:.2f}", line["receiving_line_id"]),
                    )
                    changed_lines.append(int(line["receiving_line_id"]))
            pending = "Credit Pending" if total_credit > 0 else (
                "Replacement Pending" if case["issue_code"] == "receiving_damage_or_rejection" else "Resolved"
            )
            note = (
                f"CostPilot manager review completed. {case['problem']}. "
                f"Expected vendor credit ${money(total_credit):.2f}. Original quantities and discrepancy statuses preserved."
            )
            existing_notes = str(session["notes"] or "").strip()
            if note not in existing_notes:
                combined = f"{existing_notes}\n{note}".strip()
                conn.execute(
                    "UPDATE receiving_sessions SET notes=?,updated_at=? WHERE session_id=?",
                    (combined, now_iso(), session_id),
                )
            user = self._actor(actor)
            conn.execute(
                """INSERT INTO costpilot_review_resolutions(
                       case_type,case_id,resolution_status,resolution_code,resolution_note,
                       estimated_value,resolved_by,resolved_by_role,resolved_at,updated_at)
                   VALUES('receiving',?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(case_type,case_id) DO UPDATE SET
                       resolution_status=excluded.resolution_status,
                       resolution_code=excluded.resolution_code,
                       resolution_note=excluded.resolution_note,
                       estimated_value=excluded.estimated_value,
                       resolved_by=excluded.resolved_by,
                       resolved_by_role=excluded.resolved_by_role,
                       resolved_at=excluded.resolved_at,
                       updated_at=excluded.updated_at""",
                (
                    session_id, pending, case["issue_code"], note,
                    f"{money(total_credit):.2f}", user.username if user else "system",
                    user.role if user else "System", now_iso(), now_iso(),
                ),
            )
        try:
            self.pipeline.margin_memory.capture_receiving_discrepancies(session_id)
        except Exception:
            pass
        self.controls.audit(
            "receiving.resolve.costpilot", "receiving", session_id,
            f"CostPilot-assisted receiving review: {case['problem']}",
            after={"review_status": pending, "expected_credit": f"{money(total_credit):.2f}"},
            details={"changed_credit_lines": changed_lines, "original_discrepancies_preserved": True},
            actor=actor,
        )
        return {
            "case_id": case["case_id"], "session_id": session_id,
            "resolution_status": pending, "expected_credit": f"{money(total_credit):.2f}",
            "changed_credit_lines": changed_lines,
        }

    def execute(
        self,
        action: str,
        case_ids: Iterable[str] | None = None,
        *,
        reason: str = "CostPilot-assisted manager review",
        actor: AuthenticatedUser | None = None,
    ) -> dict[str, Any]:
        preview = self.preview(action, case_ids)
        requested = preview["requested"]
        eligible = preview["eligible"]
        skipped_cases = preview["skipped"]
        affected: list[str] = []
        details: dict[str, Any] = {"results": []}

        if action in {"approve_all_eligible", "recover_and_approve"}:
            self.controls.require_permission("invoices.review", actor)
            invoice_ids = [case["entity_id"] for case in eligible]
            if invoice_ids:
                summary = self.pipeline.batch_process_reviews(
                    invoice_ids, approve_eligible=True, explicit_approval=True
                )
            else:
                summary = {"requested": 0, "approved": 0, "needs_review": 0, "duplicates": 0, "failed": 0, "results": []}
            details["invoice_batch"] = summary
            for result in summary.get("results", []):
                if result.get("status") == "Approved":
                    affected.append(self._case_id("invoice", str(result.get("invoice_id"))))
        elif action in {"reject_selected", "reject_all_documents"}:
            invoice_ids = [case["entity_id"] for case in eligible]
            affected.extend(self._reject_invoice_ids(invoice_ids, reason, actor))
        elif action == "reject_unreadable_duplicates":
            invoice_ids = []
            for case in eligible:
                invoice_ids.extend(self._duplicate_review_ids(case))
            affected.extend(self._reject_invoice_ids(
                invoice_ids,
                "Unreadable or duplicate document rejected through CostPilot review. Approved originals were preserved.",
                actor,
            ))
        elif action == "resolve_receiving":
            for case in eligible:
                result = self._resolve_receiving_case(case, actor)
                details["results"].append(result)
                affected.append(case["case_id"])
        elif action == "retry_upload":
            user = actor or self.controls.current_user
            if user is None or not (
                user.can("invoices.process")
                or user.can("pos.import")
                or user.can("settings.manage")
            ):
                raise PermissionDenied("You do not have permission to retry Auto Upload files.")
            from auto_upload import AutoUploadRouter
            router = AutoUploadRouter(self.workspace)
            for case in eligible:
                retry = router.retry_event(int(case["entity_id"]))
                details["results"].append(retry)
                affected.append(case["case_id"])
        elif action in {"apply_recommended", "apply_all_recommended"}:
            grouped: dict[str, list[str]] = defaultdict(list)
            for case in eligible:
                grouped[case["recommended_action"]].append(case["case_id"])
            nested = []
            for recommended_action, ids in grouped.items():
                result = self.execute(recommended_action, ids, reason=reason, actor=actor)
                nested.append(result)
                affected.extend(result.get("affected_case_ids", []))
            details["nested_results"] = nested
        else:
            raise OperationalControlsError(f"Unsupported CostPilot review action: {action}")

        affected = list(dict.fromkeys(affected))
        skipped_ids = [case["case_id"] for case in skipped_cases]
        status = "Completed" if affected else "No Change"
        summary = (
            f"CostPilot review action {action.replace('_', ' ')} affected {len(affected)} case(s); "
            f"{len(skipped_ids)} case(s) were left open because the action was not safe or applicable."
        )
        action_id = self._record_action(
            action, requested, affected, skipped_ids, status, summary, details, actor
        )
        return {
            "action_id": action_id,
            "action": action,
            "requested_count": len(requested),
            "affected_count": len(affected),
            "skipped_count": len(skipped_ids),
            "affected_case_ids": affected,
            "skipped_case_ids": skipped_ids,
            "status": status,
            "summary": summary,
            "details": details,
        }

    def list_actions(self, limit: int = 100) -> list[sqlite3.Row]:
        self.controls.require_permission("audit.view")
        with self.workspace.connect() as conn:
            return conn.execute(
                "SELECT * FROM costpilot_review_actions ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
