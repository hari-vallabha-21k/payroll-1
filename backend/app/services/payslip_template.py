"""Payslip template engine.

Presentation only. It receives a finished payroll result and turns it into
HTML or PDF; it never computes a figure, so a template change can never move
someone's salary. The reverse holds too: a formula change cannot alter a
template.

A template is a JSON document of ordered sections. Text fields may contain
``{{variable}}`` placeholders, and the earnings/deductions tables are rendered
from whatever components the employee actually has.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}", re.IGNORECASE)

SECTION_TYPES = (
    "company_header",
    "title",
    "employee_details",
    "attendance",
    "earnings_deductions",
    "summary",
    "notes",
    "footer",
)

DEFAULT_TEMPLATE: dict[str, Any] = {
    "page": {"size": "A4", "accent": "#1f2937", "currency_symbol": "₹"},
    "sections": [
        {
            "type": "company_header",
            "enabled": True,
            "show_logo": True,
            "lines": [
                "{{company_name}}",
                "{{company_address}}",
                "{{company_phone}}  {{company_email}}",
                "GSTIN: {{company_gst}}",
            ],
        },
        {"type": "title", "enabled": True, "text": "Salary Slip — {{pay_period}}"},
        {
            "type": "employee_details",
            "enabled": True,
            "fields": [
                {"label": "Employee", "value": "{{employee_name}}"},
                {"label": "Employee ID", "value": "{{employee_id}}"},
                {"label": "Designation", "value": "{{designation}}"},
                {"label": "Department", "value": "{{department}}"},
                {"label": "Date of Joining", "value": "{{date_of_joining}}"},
                {"label": "Bank A/C", "value": "{{bank_account}}"},
            ],
        },
        {
            "type": "attendance",
            "enabled": True,
            "fields": [
                {"label": "Working Days", "value": "{{working_days}}"},
                {"label": "Payable Days", "value": "{{payable_days}}"},
                {"label": "Paid Leave", "value": "{{paid_leave}}"},
                {"label": "LOP Days", "value": "{{lop_days}}"},
                {"label": "Overtime Hours", "value": "{{overtime_hours}}"},
            ],
        },
        {
            "type": "earnings_deductions",
            "enabled": True,
            "earnings_title": "Earnings",
            "deductions_title": "Deductions",
            # Empty components are hidden unless a template asks otherwise.
            "hide_zero_rows": True,
        },
        {
            "type": "summary",
            "enabled": True,
            "show_in_words": True,
        },
        {
            "type": "footer",
            "enabled": True,
            "signatory": "Authorised Signatory",
            "note": "This is a computer generated document and needs no signature.",
        },
    ],
}

ONES = (
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _two_digits(number: int) -> str:
    if number < 20:
        return ONES[number]
    tens, ones = divmod(number, 10)
    return TENS[tens] + (f" {ONES[ones]}" if ones else "")


def amount_in_words(amount: Decimal | float | str, currency: str = "Rupees") -> str:
    """Indian numbering: lakh and crore, with paise."""
    value = Decimal(str(amount))
    whole = int(value)
    paise = int((value - whole) * 100)

    if whole == 0:
        words = "Zero"
    else:
        groups = [
            (whole // 10_000_000, "Crore"),
            ((whole // 100_000) % 100, "Lakh"),
            ((whole // 1000) % 100, "Thousand"),
            ((whole // 100) % 10, "Hundred"),
        ]
        parts = [f"{_two_digits(count)} {label}" for count, label in groups if count]
        remainder = whole % 100
        if remainder:
            parts.append(_two_digits(remainder))
        words = " ".join(parts)

    text = f"{currency} {words}"
    if paise:
        text += f" and {_two_digits(paise)} Paise"
    return text + " Only"


@dataclass
class RenderContext:
    variables: dict[str, str]
    earnings: list[dict]
    deductions: list[dict]
    currency_symbol: str = "₹"


def money(value, symbol: str = "₹") -> str:
    try:
        return f"{symbol}{Decimal(str(value)):,.2f}"
    except Exception:
        return f"{symbol}0.00"


def build_context(snapshot: dict, currency_symbol: str = "₹") -> RenderContext:
    """Flatten a payslip snapshot into template variables.

    The snapshot is the frozen record stored with the payslip, so rendering an
    old payslip uses the company, employee and figures as they were then.
    """
    tenant = snapshot.get("tenant", {})
    employee = snapshot.get("employee", {})
    attendance = snapshot.get("attendance", {})
    totals = snapshot.get("totals", {})
    overtime_minutes = int(attendance.get("overtime_minutes") or 0)

    variables = {
        "company_name": tenant.get("name", ""),
        "company_address": tenant.get("address", ""),
        "company_phone": tenant.get("phone", ""),
        "company_email": tenant.get("email", ""),
        "company_gst": tenant.get("gst_number", ""),
        "company_registration": tenant.get("registration_number", ""),
        "company_logo": tenant.get("logo_url", ""),
        "employee_name": employee.get("name", ""),
        "employee_id": employee.get("code", ""),
        "designation": employee.get("designation") or "-",
        "department": employee.get("department") or "-",
        "branch": employee.get("branch") or "-",
        "date_of_joining": employee.get("date_of_joining", ""),
        "bank_account": employee.get("bank_account") or "-",
        "pan": employee.get("pan") or "-",
        "pf_number": employee.get("pf_number") or "-",
        "esi_number": employee.get("esi_number") or "-",
        "pay_period": snapshot.get("period", {}).get("label", ""),
        "working_days": str(attendance.get("working_days", "")),
        "payable_days": str(attendance.get("payable_days", "")),
        "present_days": str(attendance.get("present_days", attendance.get("payable_days", ""))),
        "paid_leave": str(attendance.get("paid_leave_days", "")),
        "unpaid_leave": str(attendance.get("unpaid_leave_days", "")),
        "lop_days": str(attendance.get("lop_days", "")),
        "overtime_hours": f"{overtime_minutes // 60}h {overtime_minutes % 60}m",
        "gross_salary": money(totals.get("gross", 0), currency_symbol),
        "total_deductions": money(totals.get("deductions", 0), currency_symbol),
        "net_salary": money(totals.get("net", 0), currency_symbol),
        "net_salary_in_words": amount_in_words(totals.get("net", 0)),
        "payslip_number": snapshot.get("payslip_number", ""),
        "generated_on": snapshot.get("generated_on", ""),
    }
    return RenderContext(
        variables=variables,
        earnings=snapshot.get("earnings", []),
        deductions=snapshot.get("deductions", []),
        currency_symbol=currency_symbol,
    )


def substitute(text: str, variables: dict[str, str]) -> str:
    """Replace {{placeholders}}; an unknown one renders empty, never raises."""
    if not text:
        return ""
    return PLACEHOLDER.sub(lambda match: str(variables.get(match.group(1).lower(), "")), text)


def _rows(components: list[dict], hide_zero: bool, symbol: str) -> list[tuple[str, str]]:
    rows = []
    for component in components:
        amount = Decimal(str(component.get("amount", 0)))
        if hide_zero and amount == 0:
            continue
        rows.append((component.get("label", ""), money(amount, symbol)))
    return rows


def render_html(template_data: dict, snapshot: dict) -> str:
    """Render a payslip as standalone HTML - used for preview and for PDF."""
    page = template_data.get("page", {})
    symbol = page.get("currency_symbol", "₹")
    accent = page.get("accent", "#1f2937")
    context = build_context(snapshot, symbol)
    variables = context.variables

    def esc(text: str) -> str:
        return html.escape(substitute(text, variables))

    blocks: list[str] = []

    for section in template_data.get("sections", []):
        if not section.get("enabled", True):
            continue
        kind = section.get("type")

        if kind == "company_header":
            lines = [esc(line) for line in section.get("lines", [])]
            lines = [line for line in lines if line.strip()]
            logo = variables.get("company_logo")
            logo_html = (
                f'<img class="logo" src="{html.escape(logo)}" alt="" />'
                if section.get("show_logo") and logo
                else ""
            )
            body = "".join(f"<div>{line}</div>" for line in lines[1:])
            blocks.append(
                f'<header class="company">{logo_html}'
                f'<h1>{lines[0] if lines else ""}</h1>'
                f'<div class="meta">{body}</div></header>'
            )

        elif kind == "title":
            blocks.append(f'<h2 class="title">{esc(section.get("text", ""))}</h2>')

        elif kind in ("employee_details", "attendance"):
            cells = "".join(
                f'<div class="cell"><span class="k">{html.escape(field.get("label", ""))}</span>'
                f'<span class="v">{esc(field.get("value", ""))}</span></div>'
                for field in section.get("fields", [])
            )
            heading = f'<h3>{html.escape(section["title"])}</h3>' if section.get("title") else ""
            blocks.append(f'{heading}<section class="details">{cells}</section>')

        elif kind == "earnings_deductions":
            hide_zero = section.get("hide_zero_rows", True)
            earnings = _rows(context.earnings, hide_zero, symbol)
            deductions = _rows(context.deductions, hide_zero, symbol)
            height = max(len(earnings), len(deductions))
            body = ""
            for index in range(height):
                earning = earnings[index] if index < len(earnings) else ("", "")
                deduction = deductions[index] if index < len(deductions) else ("", "")
                body += (
                    f"<tr><td>{html.escape(earning[0])}</td><td class='n'>{earning[1]}</td>"
                    f"<td>{html.escape(deduction[0])}</td><td class='n'>{deduction[1]}</td></tr>"
                )
            blocks.append(
                '<table class="components"><thead><tr>'
                f'<th>{html.escape(section.get("earnings_title", "Earnings"))}</th>'
                '<th class="n">Amount</th>'
                f'<th>{html.escape(section.get("deductions_title", "Deductions"))}</th>'
                '<th class="n">Amount</th>'
                f"</tr></thead><tbody>{body}</tbody><tfoot><tr>"
                f'<td>Gross Salary</td><td class="n">{variables["gross_salary"]}</td>'
                f'<td>Total Deductions</td><td class="n">{variables["total_deductions"]}</td>'
                "</tr></tfoot></table>"
            )

        elif kind == "summary":
            words = (
                f'<p class="words">{html.escape(variables["net_salary_in_words"])}</p>'
                if section.get("show_in_words", True)
                else ""
            )
            blocks.append(
                f'<section class="net"><span>NET SALARY</span>'
                f'<strong>{variables["net_salary"]}</strong></section>{words}'
            )

        elif kind == "notes":
            blocks.append(f'<p class="note">{esc(section.get("text", ""))}</p>')

        elif kind == "footer":
            signatory = esc(section.get("signatory", ""))
            note = esc(section.get("note", ""))
            blocks.append(
                f'<footer><div class="sign">{signatory}</div><p class="note">{note}</p></footer>'
            )

    styles = f"""
    body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
            color: #111827; margin: 0; padding: 24px; background: #f8fafc; }}
    .slip {{ max-width: 760px; margin: 0 auto; background: #fff; padding: 28px;
             border: 1px solid #e5e7eb; border-radius: 10px; }}
    .company {{ text-align: center; border-bottom: 2px solid {accent}; padding-bottom: 12px; }}
    .company h1 {{ margin: 4px 0; font-size: 20px; }}
    .company .meta {{ color: #6b7280; font-size: 12px; }}
    .logo {{ max-height: 56px; }}
    .title {{ text-align: center; font-size: 15px; letter-spacing: .05em;
              text-transform: uppercase; color: {accent}; margin: 16px 0; }}
    .details {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 6px 18px; margin-bottom: 16px; }}
    .cell {{ display: flex; justify-content: space-between; border-bottom: 1px dotted #e5e7eb;
             padding: 4px 0; font-size: 13px; }}
    .cell .k {{ color: #6b7280; }}
    .cell .v {{ font-weight: 600; }}
    table.components {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }}
    table.components th {{ background: {accent}; color: #fff; text-align: left; padding: 8px; }}
    table.components td {{ padding: 7px 8px; border-bottom: 1px solid #f1f5f9; }}
    table.components tfoot td {{ font-weight: 700; background: #f3f4f6; }}
    .n {{ text-align: right; }}
    .net {{ display: flex; justify-content: space-between; align-items: center;
            background: {accent}; color: #fff; padding: 12px 16px; border-radius: 8px;
            font-weight: 700; margin-top: 10px; }}
    .words {{ font-size: 12px; color: #6b7280; font-style: italic; }}
    footer {{ margin-top: 28px; display: flex; justify-content: space-between;
              align-items: flex-end; border-top: 1px solid #e5e7eb; padding-top: 12px; }}
    .sign {{ font-size: 12px; font-weight: 600; }}
    .note {{ font-size: 11px; color: #6b7280; }}
    """
    title = html.escape(f"Payslip {variables['employee_id']} {variables['pay_period']}".strip())
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{styles}</style></head><body><div class='slip'>"
        + "".join(blocks)
        + "</div></body></html>"
    )


def validate_template(template_data: dict) -> list[str]:
    """Problems that would make a template render badly. Empty list means fine."""
    problems: list[str] = []
    if not isinstance(template_data, dict):
        return ["Template must be a JSON object"]

    sections = template_data.get("sections")
    if not isinstance(sections, list) or not sections:
        return ["Template must define at least one section"]

    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            problems.append(f"Section {index} is not an object")
            continue
        kind = section.get("type")
        if kind not in SECTION_TYPES:
            problems.append(
                f"Section {index}: unknown type '{kind}'. Allowed: {', '.join(SECTION_TYPES)}"
            )

    if not any(s.get("type") == "earnings_deductions" for s in sections if isinstance(s, dict)):
        problems.append("A payslip needs an 'earnings_deductions' section")
    return problems


def dumps(template_data: dict) -> str:
    return json.dumps(template_data, ensure_ascii=False)
