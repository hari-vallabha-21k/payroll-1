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


def build_snapshot(tenant: Tenant, run: PayrollRun, item: PayrollItem, employee: Employee) -> dict:
    breakdown = json.loads(item.breakdown_json or "{}")
    return {
        "tenant": {"code": tenant.code, "name": tenant.name, "currency": tenant.currency},
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
        },
        "attendance": {
            "working_days": item.working_days,
            "payable_days": item.payable_days,
            "lop_days": item.lop_days,
            "paid_leave_days": item.paid_leave_days,
            "overtime_minutes": item.overtime_minutes,
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


def render_pdf(payslip: Payslip) -> bytes:
    data = json.loads(payslip.snapshot_json)
    currency = data["tenant"].get("currency", "INR")
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=f"Payslip {data['employee']['code']} {data['period']['label']}",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    heading = ParagraphStyle("heading", parent=styles["Title"], fontSize=16, spaceAfter=2)
    subtle = ParagraphStyle("subtle", parent=styles["Normal"], textColor=colors.HexColor("#555555"))

    story = [
        Paragraph(data["tenant"]["name"], heading),
        Paragraph(f"Salary Slip &mdash; {data['period']['label']}", subtle),
        Spacer(1, 8 * mm),
    ]

    employee = data["employee"]
    attendance = data["attendance"]
    details = [
        ["Employee", employee["name"], "Employee ID", employee["code"]],
        [
            "Department",
            employee.get("department") or "-",
            "Designation",
            employee.get("designation") or "-",
        ],
        [
            "Payable Days",
            f"{attendance['payable_days']} / {attendance['working_days']}",
            "Overtime",
            f"{attendance['overtime_minutes'] // 60}h {attendance['overtime_minutes'] % 60}m",
        ],
        [
            "Loss of Pay",
            f"{attendance['lop_days']} day(s)",
            "Bank A/C",
            employee.get("bank_account") or "-",
        ],
    ]
    detail_table = Table(details, colWidths=[30 * mm, 50 * mm, 30 * mm, 50 * mm])
    detail_table.setStyle(
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
    story += [detail_table, Spacer(1, 8 * mm)]

    earnings = data["earnings"]
    deductions = data["deductions"]
    rows = [["Earnings", "Amount", "Deductions", "Amount"]]
    for index in range(max(len(earnings), len(deductions))):
        earning = earnings[index] if index < len(earnings) else None
        deduction = deductions[index] if index < len(deductions) else None
        rows.append(
            [
                earning["label"] if earning else "",
                _money(currency, earning["amount"]) if earning else "",
                deduction["label"] if deduction else "",
                _money(currency, deduction["amount"]) if deduction else "",
            ]
        )
    rows.append(
        [
            "Gross Salary",
            _money(currency, data["totals"]["gross"]),
            "Total Deductions",
            _money(currency, data["totals"]["deductions"]),
        ]
    )

    table = Table(rows, colWidths=[45 * mm, 35 * mm, 45 * mm, 35 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
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
    story += [table, Spacer(1, 6 * mm)]

    net = Table(
        [["NET SALARY", _money(currency, data["totals"]["net"])]], colWidths=[125 * mm, 35 * mm]
    )
    net.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story += [
        net,
        Spacer(1, 6 * mm),
        Paragraph(
            f"Payslip {payslip.payslip_number} &middot; computer generated, no signature required.",
            subtle,
        ),
    ]

    doc.build(story)
    return buffer.getvalue()
