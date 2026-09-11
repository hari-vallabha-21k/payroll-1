"""Payslip snapshot + PDF rendering."""

from __future__ import annotations

import calendar
import json
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..models import Employee, PayrollItem, PayrollRun, Payslip, Tenant
from . import payslip_template


def _setting(tenant: Tenant, key: str) -> str:
    """Company details live in tenant settings; missing ones render blank."""
    from sqlalchemy import inspect as sa_inspect

    from ..models import TenantSetting

    session = sa_inspect(tenant).session
    if session is None:
        return ""
    from sqlalchemy import select

    row = session.execute(
        select(TenantSetting).where(TenantSetting.tenant_id == tenant.id, TenantSetting.key == key)
    ).scalar_one_or_none()
    return row.value if row else ""


def build_snapshot(tenant: Tenant, run: PayrollRun, item: PayrollItem, employee: Employee) -> dict:
    breakdown = json.loads(item.breakdown_json or "{}")
    return {
        "tenant": {
            "code": tenant.code,
            "name": tenant.name,
            "currency": tenant.currency,
            "address": _setting(tenant, "company_address"),
            "phone": _setting(tenant, "company_phone"),
            "email": _setting(tenant, "company_email"),
            "gst_number": _setting(tenant, "company_gst"),
            "registration_number": _setting(tenant, "company_registration"),
            "logo_url": _setting(tenant, "company_logo"),
        },
        "period": {
            "year": run.period_year,
            "month": run.period_month,
            "label": f"{calendar.month_name[run.period_month]} {run.period_year}",
        },
        "employee": {
            "id": employee.id,
            "code": employee.employee_code,
            "name": employee.full_name,
            "department": employee.department.name if employee.department else None,
            "designation": employee.designation.name if employee.designation else None,
            "date_of_joining": str(employee.date_of_joining),
            "bank_account": employee.bank_account,
            "bank_ifsc": employee.bank_ifsc,
            "branch": employee.branch,
            "pan": employee.pan,
            "pf_number": employee.pf_number,
            "esi_number": employee.esi_number,
            "uan": employee.uan,
        },
        "attendance": {
            "working_days": item.working_days,
            "payable_days": item.payable_days,
            "lop_days": item.lop_days,
            "paid_leave_days": item.paid_leave_days,
            "overtime_minutes": item.overtime_minutes,
            "present_days": item.payable_days,
            "unpaid_leave_days": item.lop_days,
        },
        "earnings": breakdown.get("earnings", []),
        "deductions": breakdown.get("deductions", []),
        "totals": {
            "gross": str(item.gross),
            "deductions": str(item.deductions),
            "net": str(item.net),
        },
    }


def _money(currency: str, value) -> str:
    symbol = {"INR": "₹", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{symbol}{float(value):,.2f}"


def render_html(payslip: Payslip) -> str:
    """Render using the template captured with the payslip, not the live one."""
    snapshot = json.loads(payslip.snapshot_json)
    snapshot.setdefault("payslip_number", payslip.payslip_number)
    snapshot.setdefault("generated_on", str(payslip.generated_at))
    template_data = (
        json.loads(payslip.template_snapshot_json)
        if payslip.template_snapshot_json
        else payslip_template.DEFAULT_TEMPLATE
    )
    return payslip_template.render_html(template_data, snapshot)


def render_pdf(payslip: Payslip) -> bytes:
    """Render the PDF from the template captured with this payslip.

    Same sections, same order and same hidden-when-zero behaviour as the HTML
    preview, so what an administrator previews is what employees receive.
    """
    snapshot = json.loads(payslip.snapshot_json)
    snapshot.setdefault("payslip_number", payslip.payslip_number)
    template_data = (
        json.loads(payslip.template_snapshot_json)
        if payslip.template_snapshot_json
        else payslip_template.DEFAULT_TEMPLATE
    )
    page = template_data.get("page", {})
    symbol = page.get("currency_symbol", "\u20b9")
    accent = colors.HexColor(page.get("accent", "#1f2937"))
    context = payslip_template.build_context(snapshot, symbol)
    variables = context.variables

    def text(value: str) -> str:
        return payslip_template.substitute(value, variables)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=f"Payslip {variables['employee_id']} {variables['pay_period']}".strip(),
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    heading = ParagraphStyle("heading", parent=styles["Title"], fontSize=16, spaceAfter=2)
    centered = ParagraphStyle(
        "centered", parent=styles["Normal"], alignment=1, textColor=colors.HexColor("#555555")
    )
    subtle = ParagraphStyle("subtle", parent=styles["Normal"], textColor=colors.HexColor("#555555"))
    story = []

    def field_table(fields: list[dict]) -> Table:
        rows, pair = [], []
        for field in fields:
            pair += [field.get("label", ""), text(field.get("value", ""))]
            if len(pair) == 4:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair + [""] * (4 - len(pair)))
        table = Table(rows or [["", "", "", ""]], colWidths=[32 * mm, 48 * mm, 32 * mm, 48 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
                ]
            )
        )
        return table

    for section in template_data.get("sections", []):
        if not section.get("enabled", True):
            continue
        kind = section.get("type")

        if kind == "company_header":
            lines = [text(line).strip() for line in section.get("lines", [])]
            lines = [line for line in lines if line]
            if lines:
                story.append(Paragraph(lines[0], heading))
            for line in lines[1:]:
                story.append(Paragraph(line, centered))
            story.append(Spacer(1, 6 * mm))

        elif kind == "title":
            story += [Paragraph(text(section.get("text", "")), centered), Spacer(1, 5 * mm)]

        elif kind in ("employee_details", "attendance"):
            story += [field_table(section.get("fields", [])), Spacer(1, 5 * mm)]

        elif kind == "earnings_deductions":
            hide_zero = section.get("hide_zero_rows", True)
            earnings = payslip_template._rows(context.earnings, hide_zero, symbol)
            deductions = payslip_template._rows(context.deductions, hide_zero, symbol)
            rows = [
                [
                    section.get("earnings_title", "Earnings"),
                    "Amount",
                    section.get("deductions_title", "Deductions"),
                    "Amount",
                ]
            ]
            for index in range(max(len(earnings), len(deductions))):
                earning = earnings[index] if index < len(earnings) else ("", "")
                deduction = deductions[index] if index < len(deductions) else ("", "")
                rows.append([earning[0], earning[1], deduction[0], deduction[1]])
            rows.append(
                [
                    "Gross Salary",
                    variables["gross_salary"],
                    "Total Deductions",
                    variables["total_deductions"],
                ]
            )
            table = Table(rows, colWidths=[45 * mm, 35 * mm, 45 * mm, 35 * mm])
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), accent),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
                        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f3f4f6")),
                    ]
                )
            )
            story += [table, Spacer(1, 5 * mm)]

        elif kind == "summary":
            net = Table([["NET SALARY", variables["net_salary"]]], colWidths=[125 * mm, 35 * mm])
            net.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), accent),
                        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
                        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                        ("TOPPADDING", (0, 0), (-1, -1), 8),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.append(net)
            if section.get("show_in_words", True):
                story += [Spacer(1, 3 * mm), Paragraph(variables["net_salary_in_words"], subtle)]
            story.append(Spacer(1, 5 * mm))

        elif kind == "notes":
            story += [Paragraph(text(section.get("text", "")), subtle), Spacer(1, 4 * mm)]

        elif kind == "footer":
            story += [
                Spacer(1, 8 * mm),
                Paragraph(text(section.get("signatory", "")), subtle),
                Paragraph(text(section.get("note", "")), subtle),
            ]

    story.append(Paragraph(f"Payslip {payslip.payslip_number}", subtle))
    doc.build(story)
    return buffer.getvalue()
