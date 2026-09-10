from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .models import (
    AttendanceStatus,
    ComponentType,
    DeviceStatus,
    EmployeeStatus,
    EmploymentType,
    EventSource,
    EventType,
    LeaveStatus,
    PayrollStatus,
    Role,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserOut(ORMModel):
    id: int
    tenant_id: int
    email: str
    full_name: str
    role: Role
    employee_id: int | None = None


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)
    role: Role = Role.EMPLOYEE
    employee_id: int | None = None


# --- org --------------------------------------------------------------------
class NamedCreate(BaseModel):
    name: str


class NamedOut(ORMModel):
    id: int
    name: str


class TenantOut(ORMModel):
    id: int
    code: str
    name: str
    currency: str
    timezone: str


# --- shifts -----------------------------------------------------------------
class ShiftCreate(BaseModel):
    name: str
    start_time: time
    end_time: time
    break_minutes: int = 60
    grace_minutes: int = 10
    half_day_hours: float = 4.0
    full_day_hours: float = 8.0
    overtime_after_minutes: int = 0


class ShiftOut(ORMModel):
    id: int
    name: str
    start_time: time
    end_time: time
    break_minutes: int
    grace_minutes: int
    half_day_hours: float
    full_day_hours: float
    overtime_after_minutes: int
    is_active: bool


# --- employees --------------------------------------------------------------
class EmployeeCreate(BaseModel):
    employee_code: str
    first_name: str
    last_name: str = ""
    email: EmailStr | None = None
    phone: str | None = None
    date_of_birth: date | None = None
    date_of_joining: date
    department_id: int | None = None
    designation_id: int | None = None
    manager_id: int | None = None
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    shift_id: int | None = None
    bank_account: str | None = None
    status: EmployeeStatus = EmployeeStatus.ACTIVE


class EmployeeUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    date_of_birth: date | None = None
    date_of_joining: date | None = None
    department_id: int | None = None
    designation_id: int | None = None
    manager_id: int | None = None
    employment_type: EmploymentType | None = None
    shift_id: int | None = None
    bank_account: str | None = None
    status: EmployeeStatus | None = None


class EmployeeOut(ORMModel):
    id: int
    employee_code: str
    first_name: str
    last_name: str
    full_name: str
    email: str | None
    phone: str | None
    date_of_joining: date
    employment_type: EmploymentType
    status: EmployeeStatus
    department: NamedOut | None = None
    designation: NamedOut | None = None
    shift: ShiftOut | None = None
    has_biometric: bool = False


# --- webauthn ---------------------------------------------------------------
class WebAuthnOptionsRequest(BaseModel):
    employee_code: str
    tenant_code: str | None = None


class WebAuthnRegisterOptionsRequest(BaseModel):
    """Registration is driven by a single-use enrollment token, so an employee
    can only ever enrol the phone they were invited on."""

    token: str


class WebAuthnRegisterVerify(BaseModel):
    token: str
    credential: dict[str, Any]
    device_label: str | None = None


class WebAuthnAuthVerify(BaseModel):
    employee_code: str
    tenant_code: str | None = None
    credential: dict[str, Any]
    event_type: EventType | None = None


class EnrollmentTokenOut(BaseModel):
    token: str
    employee_id: int
    employee_code: str
    expires_at: datetime
    enroll_url: str


class KioskEmployeeOut(BaseModel):
    employee_code: str
    employee_name: str
    has_biometric: bool
    next_action: EventType


class CredentialOut(ORMModel):
    id: int
    employee_id: int
    credential_id: str
    device_label: str | None
    created_at: datetime
    last_used_at: datetime | None
    is_active: bool


# --- attendance -------------------------------------------------------------
class AttendanceEventOut(ORMModel):
    id: int
    employee_id: int
    event_type: EventType
    event_time: datetime
    source: EventSource
    device_id: int | None
    note: str | None


class PunchResult(BaseModel):
    employee_code: str
    employee_name: str
    event_type: EventType
    event_time: datetime
    message: str
    daily: DailyAttendanceOut | None = None


class DailyAttendanceOut(ORMModel):
    id: int
    employee_id: int
    work_date: date
    first_in: datetime | None
    last_out: datetime | None
    worked_minutes: int
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int
    status: AttendanceStatus
    payable_day_fraction: float
    missing_checkout: bool
    is_manual_override: bool
    remarks: str | None


class AttendanceCorrection(BaseModel):
    work_date: date
    first_in: datetime | None = None
    last_out: datetime | None = None
    status: AttendanceStatus | None = None
    payable_day_fraction: float | None = None
    overtime_minutes: int | None = None
    reason: str


class ManualPunch(BaseModel):
    employee_code: str
    event_type: EventType
    event_time: datetime
    note: str | None = None


# --- leave ------------------------------------------------------------------
class LeaveTypeCreate(BaseModel):
    name: str
    is_paid: bool = True
    annual_quota_days: float = 12.0


class LeaveTypeOut(ORMModel):
    id: int
    name: str
    is_paid: bool
    annual_quota_days: float


class LeaveRequestCreate(BaseModel):
    employee_id: int | None = None
    leave_type_id: int
    start_date: date
    end_date: date
    reason: str | None = None


class LeaveDecision(BaseModel):
    status: LeaveStatus
    note: str | None = None


class LeaveRequestOut(ORMModel):
    id: int
    employee_id: int
    leave_type_id: int
    start_date: date
    end_date: date
    reason: str | None
    status: LeaveStatus
    decision_note: str | None
    created_at: datetime


# --- salary -----------------------------------------------------------------
class SalaryComponentIn(BaseModel):
    name: str
    component_type: ComponentType
    amount: Decimal = Decimal("0")
    percent_of_basic: float | None = None
    prorated: bool = True
    is_basic: bool = False
    sort_order: int = 0


class SalaryStructureCreate(BaseModel):
    name: str
    overtime_rate_per_hour: Decimal = Decimal("0")
    components: list[SalaryComponentIn] = []


class SalaryComponentOut(ORMModel):
    id: int
    name: str
    component_type: ComponentType
    amount: Decimal
    percent_of_basic: float | None
    prorated: bool
    is_basic: bool
    sort_order: int


class SalaryStructureOut(ORMModel):
    id: int
    name: str
    overtime_rate_per_hour: Decimal
    is_active: bool
    components: list[SalaryComponentOut] = []


class EmployeeSalaryAssign(BaseModel):
    structure_id: int
    effective_from: date
    monthly_ctc: Decimal | None = None
    overtime_rate_per_hour: Decimal | None = None


class EmployeeSalaryOut(ORMModel):
    id: int
    employee_id: int
    structure_id: int
    monthly_ctc: Decimal | None
    overtime_rate_per_hour: Decimal | None
    effective_from: date
    effective_to: date | None


# --- payroll ----------------------------------------------------------------
class PayrollRunCreate(BaseModel):
    period_year: int = Field(ge=2000, le=2100)
    period_month: int = Field(ge=1, le=12)


class PayrollItemOut(ORMModel):
    id: int
    employee_id: int
    working_days: float
    payable_days: float
    lop_days: float
    paid_leave_days: float
    overtime_minutes: int
    overtime_amount: Decimal
    gross: Decimal
    deductions: Decimal
    net: Decimal


class PayrollRunOut(ORMModel):
    id: int
    period_year: int
    period_month: int
    status: PayrollStatus
    gross_total: Decimal
    deduction_total: Decimal
    net_total: Decimal
    calculated_at: datetime | None
    approved_at: datetime | None


class PayrollRunDetail(PayrollRunOut):
    items: list[PayrollItemOut] = []


class AdjustmentCreate(BaseModel):
    employee_id: int
    label: str
    component_type: ComponentType
    amount: Decimal
    reason: str | None = None


# --- devices ----------------------------------------------------------------
class DeviceCreate(BaseModel):
    device_code: str
    name: str
    brand: str | None = None
    model: str | None = None
    ip_address: str | None = None
    port: int | None = None
    location: str | None = None


class DeviceOut(ORMModel):
    id: int
    device_code: str
    name: str
    brand: str | None
    model: str | None
    ip_address: str | None
    port: int | None
    location: str | None
    status: DeviceStatus
    last_sync_at: datetime | None
    is_active: bool


class DeviceEnrollment(BaseModel):
    employee_id: int
    device_user_id: str


class DeviceEventIn(BaseModel):
    """One punch as reported by a device connector."""

    device_user_id: str | None = None
    employee_code: str | None = None
    event_type: EventType
    event_time: datetime
    external_ref: str | None = None


class DeviceSyncRequest(BaseModel):
    events: list[DeviceEventIn]


class DeviceSyncResult(BaseModel):
    received: int
    accepted: int
    duplicates: int
    failed: int
    errors: list[str] = []


# --- dashboard / reports ----------------------------------------------------
class DashboardOut(BaseModel):
    work_date: date
    present: int
    absent: int
    late: int
    on_leave: int
    missing_checkout: int
    total_employees: int
    pending_leave_requests: int
    latest_payroll: PayrollRunOut | None = None
    alerts: list[str] = []


TokenResponse.model_rebuild()
PunchResult.model_rebuild()
