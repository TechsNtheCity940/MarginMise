#!/usr/bin/env python3
"""Weekly Invoice Log Generator

Generates a weekly PDF bundle of all delivery invoices from the past 7 days.
Includes a cover log page with invoice details and a sign-off section for manager review.

Output: {workspace}/reports/weekly_invoice_log_YYYY-MM-DD.pdf
"""
from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    Image as RLImage, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT


@dataclass
class WeeklyInvoiceRow:
    invoice_id: str
    invoice_number: str
    vendor: str
    vendor_key: str
    invoice_date: str
    total: str
    source_path: str | None
    source_archive_path: str | None
    source_name: str


def _get_week_range(reference: date | None = None) -> tuple[date, date]:
    """Return the start and end dates for the most recent complete week (Mon-Sun).
    Falls back to the current week if incomplete."""
    ref = reference or date.today()
    monday = ref - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _get_week_invoices(conn: sqlite3.Connection, week_start: date, week_end: date) -> list[WeeklyInvoiceRow]:
    """Fetch all processed invoices within the given date range."""
    rows = conn.execute(
        """
        SELECT invoice_id, invoice_number, vendor, invoice_date, total,
               source_original_path, source_archive_path, source_name
          FROM invoices
         WHERE status IN ('Processed', 'Approved', 'Reviewed')
           AND invoice_date >= ?
           AND invoice_date <= ?
         ORDER BY invoice_date ASC, vendor ASC
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()

    result = []
    for row in rows:
        result.append(WeeklyInvoiceRow(
            invoice_id=row["invoice_id"],
            invoice_number=row["invoice_number"] or "",
            vendor=row["vendor"] or "",
            vendor_key="",
            invoice_date=row["invoice_date"] or "",
            total=row["total"] or "0.00",
            source_path=row["source_original_path"],
            source_archive_path=row["source_archive_path"],
            source_name=row["source_name"] or "",
        ))

    # Enrich with vendor key from items if available
    for row in result:
        if row.vendor:
            vendor_row = conn.execute(
                "SELECT vendor_key FROM vendors WHERE vendor_name = ? LIMIT 1",
                (row.vendor,),
            ).fetchone()
            if vendor_row:
                row.vendor_key = vendor_row["vendor_key"] or ""

    return result


def _embed_image(path: str | Path | None, max_width: float, max_height: float) -> RLImage | None:
    """Safely embed an image file into a ReportLab image object."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        img = RLImage(str(p), width=max_width, height=max_height)
        img.hAlign = "CENTER"
        return img
    except Exception:
        return None


def generate_weekly_invoice_log(
    workspace,
    week_start: date | None = None,
    output_dir: Path | None = None,
) -> Path | None:
    """Generate the weekly invoice log PDF.

    Args:
        workspace: RestaurantWorkspace instance with a connect() method.
        week_start: Monday of the week to report. Defaults to last Monday.
        output_dir: Override output directory. Defaults to workspace/reports/.

    Returns:
        Path to the generated PDF, or None if no invoices found.
    """
    week_start, week_end = _get_week_range(week_start)
    conn = workspace.connect()

    try:
        invoices = _get_week_invoices(conn, week_start, week_end)
    finally:
        conn.close()

    if not invoices:
        return None

    week_str = week_start.strftime("%Y-%m-%d")
    out_dir = output_dir or (workspace.root / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"weekly_invoice_log_{week_str}.pdf"

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="BrandTitle", fontName="Helvetica-Bold", fontSize=22,
        leading=26, alignment=TA_CENTER, textColor=colors.HexColor("#0B1F33"),
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="BrandSubtitle", fontName="Helvetica", fontSize=12,
        leading=16, alignment=TA_CENTER, textColor=colors.HexColor("#0F6B78"),
        spaceAfter=18,
    ))
    styles.add(ParagraphStyle(
        name="SectionHeader", fontName="Helvetica-Bold", fontSize=13,
        leading=16, textColor=colors.HexColor("#7A1F3D"), spaceAfter=6, spaceBefore=12,
    ))
    styles.add(ParagraphStyle(
        name="Small", fontName="Helvetica", fontSize=8, leading=10,
        textColor=colors.HexColor("#475569"),
    ))
    styles.add(ParagraphStyle(
        name="Signoff", fontName="Helvetica-Bold", fontSize=11,
        leading=14, alignment=TA_LEFT, textColor=colors.HexColor("#0B1F33"),
        spaceAfter=4,
    ))

    story: list = []

    # ---- COVER PAGE ----
    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph("MarginMise", styles["BrandTitle"]))
    story.append(Paragraph("Weekly Delivery Invoice Log", styles["BrandSubtitle"]))
    story.append(Spacer(1, 0.15 * inch))

    # Date range box
    date_data = [
        ["Week Starting (Monday)", week_start.strftime("%B %d, %Y")],
        ["Week Ending (Sunday)", week_end.strftime("%B %d, %Y")],
        ["Report Generated", datetime.now().strftime("%B %d, %Y at %I:%M %p")],
        ["Total Invoices", str(len(invoices))],
    ]
    date_table = Table(date_data, colWidths=[2.6 * inch, 2.6 * inch])
    date_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F8FAFC")),
        ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#FFFFFF")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#D8E0E8")),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#D8E0E8")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(date_table)
    story.append(Spacer(1, 0.2 * inch))

    # ---- INVOICE LOG TABLE ----
    story.append(Paragraph("Invoice Log Summary", styles["SectionHeader"]))
    story.append(Spacer(1, 0.05 * inch))

    total_amount = 0.0
    table_data = [
        ["#", "Invoice #", "Vendor ID", "Vendor Name", "Invoice Date", "Total Cost"],
    ]
    for idx, inv in enumerate(invoices, start=1):
        try:
            total_amount += float(inv.total)
        except (ValueError, TypeError):
            pass
        table_data.append([
            str(idx),
            inv.invoice_number or "—",
            inv.vendor_key or "—",
            inv.vendor or "—",
            inv.invoice_date or "—",
            f"${float(inv.total):,.2f}" if inv.total else "$0.00",
        ])

    table_data.append([
        "", "", "", "", "WEEKLY TOTAL", f"${total_amount:,.2f}",
    ])

    col_widths = [0.3 * inch, 1.2 * inch, 0.8 * inch, 1.6 * inch, 1.0 * inch, 1.0 * inch]
    inv_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    inv_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B1F33")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BACKGROUND", (0, 1), (-1, -2), colors.HexColor("#F8FAFC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F8FAFC")]),
        ("FONTNAME", (0, 1), (-1, -2), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -2), 8),
        ("ALIGN", (5, 1), (5, -1), "RIGHT"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -2), 0.25, colors.HexColor("#D8E0E8")),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#7A1F3D")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFF7ED")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 9),
        ("ALIGN", (5, -1), (5, -1), "RIGHT"),
        ("BOX", (0, -1), (-1, -1), 1, colors.HexColor("#F97316")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
    ]))
    story.append(inv_table)
    story.append(Spacer(1, 0.25 * inch))

    # ---- SIGN-OFF SECTION ----
    story.append(Paragraph("Manager Sign-Off", styles["SectionHeader"]))
    story.append(Paragraph(
        "By signing below, I certify that I have reviewed all invoices listed above and confirm "
        "they are accurate, complete, and ready for submission to accounting.",
        ParagraphStyle(name="Body", fontName="Helvetica", fontSize=10, leading=14,
                       textColor=colors.HexColor("#1E293B"), spaceAfter=12),
    ))

    sign_data = [
        ["Manager Name", "Signature", "Date", "Time"],
        ["", "", "", ""],
        ["", "", "", ""],
    ]
    sign_table = Table(sign_data, colWidths=[1.8 * inch, 2.2 * inch, 1.2 * inch, 1.0 * inch])
    sign_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (0, 1), (-1, -1), "LEFT"),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#0B1F33")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, colors.HexColor("#0B1F33")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F6B78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))
    story.append(sign_table)
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        "<b>Accounting Contact:</b> CurbsideCare940@gmail.com | 940-612-9836",
        ParagraphStyle(name="Contact", fontName="Helvetica", fontSize=9, leading=12,
                       textColor=colors.HexColor("#475569"), alignment=TA_CENTER),
    ))

    # ---- INVOICE SCANS (one per page) ----
    story.append(PageBreak())
    story.append(Paragraph("Invoice Scans", styles["SectionHeader"]))
    story.append(Spacer(1, 0.1 * inch))

    page_width, page_height = letter
    margin = 0.6 * inch
    img_max_width = page_width - 2 * margin
    img_max_height = page_height - 2 * margin - 1.0 * inch  # leave room for header

    for idx, inv in enumerate(invoices, start=1):
        # Header for this invoice
        header_data = [
            [f"Invoice {idx} of {len(invoices)}", ""],
            [f"#{inv.invoice_number or 'N/A'}  |  {inv.vendor or 'Unknown Vendor'}  |  {inv.invoice_date or 'No Date'}  |  ${float(inv.total or 0):,.2f}", ""],
        ]
        header_table = Table(header_data, colWidths=[img_max_width, 0.5 * inch])
        header_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (0, 0), 10),
            ("TEXTCOLOR", (0, 0), (0, 0), colors.HexColor("#0B1F33")),
            ("FONTNAME", (0, 1), (0, 1), "Helvetica"),
            ("FONTSIZE", (0, 1), (0, 1), 9),
            ("TEXTCOLOR", (0, 1), (0, 1), colors.HexColor("#475569")),
            ("LINEBELOW", (0, 0), (0, 1), 0.5, colors.HexColor("#D8E0E8")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(KeepTogether([
            header_table,
            Spacer(1, 0.05 * inch),
        ]))

        # Try to embed the source image/PDF
        source_path = inv.source_archive_path or inv.source_path or inv.source_name
        embedded = _embed_image(source_path, img_max_width, img_max_height) if source_path else None

        if embedded is None:
            missing_text = (
                f"<i>Source file not available for this invoice "
                f"({inv.source_name or inv.invoice_id}).</i>"
            )
            story.append(Paragraph(missing_text, styles["Small"]))
        else:
            story.append(embedded)

        if idx < len(invoices):
            story.append(PageBreak())

    # Build PDF
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        rightMargin=margin,
        leftMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=f"MarginMise Weekly Invoice Log — {week_str}",
        author="MarginMise",
        subject="Weekly Delivery Invoice Log",
    )
    doc.build(story)
    return out_path
