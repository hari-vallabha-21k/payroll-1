"""Database models.

Every business record carries a ``tenant_id`` so one deployment can serve many
restaurants without data ever crossing tenants.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    HR = "HR"
    MANAGER = "MANAGER"
    EMPLOYEE = "EMPLOYEE"


class EmploymentType(str, enum.Enum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    CONTRACT = "CONTRACT"


class EmployeeStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    TERMINATED = "TERMINATED"


class EventType(str, enum.Enum):
    CHECK_IN = "CHECK_IN"
    CHECK_OUT = "CHECK_OUT"


class EventSource(str, enum.Enum):
    """Where a raw attendance event came from.

    The payroll engine never inspects this - all sources produce the same
    standardized event shape.
    """

    WEBAUTHN = "WEBAUTHN"
    BIOMETRIC_DEVICE = "BIOMETRIC_DEVICE"
    MOCK_DEVICE = "MOCK_DEVICE"
    MANUAL = "MANUAL"


class AttendanceStatus(str, enum.Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    HALF_DAY = "HALF_DAY"
    ON_LEAVE = "ON_LEAVE"
    HOLIDAY = "HOLIDAY"
    WEEKLY_OFF = "WEEKLY_OFF"


class LeaveStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PayrollStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    CALCULATED = "CALCULATED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    PROCESSED = "PROCESSED"


class ComponentType(str, enum.Enum):
    EARNING = "EARNING"
    DEDUCTION = "DEDUCTION"


class DeviceStatus(str, enum.Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(160), index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.EMPLOYEE)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    employee: Mapped[Employee | None] = relationship(foreign_keys=[employee_id])


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_dept_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))


class Designation(Base):
    __tablename__ = "designations"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_desig_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_shift_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    break_minutes: Mapped[int] = mapped_column(Integer, default=60)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=10)
    half_day_hours: Mapped[float] = mapped_column(Float, default=4.0)
    full_day_hours: Mapped[float] = mapped_column(Float, default=8.0)
    overtime_after_minutes: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("tenant_id", "employee_code", name="uq_emp_tenant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_code: Mapped[str] = mapped_column(String(32), index=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), default="")
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_of_joining: Mapped[date] = mapped_column(Date)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    designation_id: Mapped[int | None] = mapped_column(ForeignKey("designations.id"), nullable=True)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    employment_type: Mapped[EmploymentType] = mapped_column(
        Enum(EmploymentType), default=EmploymentType.FULL_TIME
    )
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[EmployeeStatus] = mapped_column(
        Enum(EmployeeStatus), default=EmployeeStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    department: Mapped[Department | None] = relationship()
    designation: Mapped[Designation | None] = relationship()
    shift: Mapped[Shift | None] = relationship()

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmployeeShift(Base):
    """Shift assignment history - the shift in effect on a given date."""

    __tablename__ = "employee_shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class WebAuthnCredential(Base):
    """A public-key credential registered on an employee's phone.

    Only the credential id, public key and signature counter are stored -
    never a fingerprint image or template. Biometric verification happens
    entirely on the employee's device.
    """

    __tablename__ = "webauthn_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    credential_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    credential_type: Mapped[str] = mapped_column(String(32), default="public-key")
    transports: Mapped[str | None] = mapped_column(String(160), nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebAuthnChallenge(Base):
    """Short-lived challenge issued for a registration or authentication ceremony."""

    __tablename__ = "webauthn_challenges"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    purpose: Mapped[str] = mapped_column(String(32))  # REGISTER | AUTHENTICATE
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BiometricDevice(Base):
    __tablename__ = "biometric_devices"
    __table_args__ = (UniqueConstraint("tenant_id", "device_code", name="uq_device_tenant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    device_code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus), default=DeviceStatus.DISCONNECTED
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BiometricUser(Base):
    """Maps a device-local enrolment id to an employee."""

    __tablename__ = "biometric_users"
    __table_args__ = (
        UniqueConstraint("device_id", "device_user_id", name="uq_biouser_device_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("biometric_devices.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    device_user_id: Mapped[str] = mapped_column(String(64))


class AttendanceEvent(Base):
    """Raw, immutable punch. Never edited - corrections are new records."""

    __tablename__ = "attendance_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_ref", name="uq_event_tenant_external_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType))
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[EventSource] = mapped_column(Enum(EventSource))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("biometric_devices.id"), nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DailyAttendance(Base):
    """Processed attendance for one employee-day, derived from raw events."""

    __tablename__ = "daily_attendance"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "work_date", name="uq_daily_emp_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"), nullable=True)
    first_in: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worked_minutes: Mapped[int] = mapped_column(Integer, default=0)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0)
    early_leave_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus), default=AttendanceStatus.ABSENT
    )
    payable_day_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    missing_checkout: Mapped[bool] = mapped_column(Boolean, default=False)
    is_manual_override: Mapped[bool] = mapped_column(Boolean, default=False)
    remarks: Mapped[str | None] = mapped_column(String(255), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("tenant_id", "holiday_date", name="uq_holiday_tenant_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    holiday_date: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(120))


class LeaveType(Base):
    __tablename__ = "leave_types"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_leavetype_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True)
    annual_quota_days: Mapped[float] = mapped_column(Float, default=12.0)


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type_id: Mapped[int] = mapped_column(ForeignKey("leave_types.id"))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[LeaveStatus] = mapped_column(Enum(LeaveStatus), default=LeaveStatus.PENDING)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    leave_type: Mapped[LeaveType] = relationship()


class SalaryStructure(Base):
    __tablename__ = "salary_structures"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_struct_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    overtime_rate_per_hour: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    components: Mapped[list[SalaryComponent]] = relationship(
        back_populates="structure", cascade="all, delete-orphan"
    )


class SalaryComponent(Base):
    __tablename__ = "salary_components"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    structure_id: Mapped[int] = mapped_column(ForeignKey("salary_structures.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    component_type: Mapped[ComponentType] = mapped_column(Enum(ComponentType))
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    percent_of_basic: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Earnings are pro-rated against payable days unless this is False
    prorated: Mapped[bool] = mapped_column(Boolean, default=True)
    is_basic: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    structure: Mapped[SalaryStructure] = relationship(back_populates="components")


class EmployeeSalary(Base):
    """Salary structure assignment history for an employee."""

    __tablename__ = "employee_salary"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    structure_id: Mapped[int] = mapped_column(ForeignKey("salary_structures.id"))
    monthly_ctc: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    overtime_rate_per_hour: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    structure: Mapped[SalaryStructure] = relationship()


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_year", "period_month", name="uq_run_tenant_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    period_year: Mapped[int] = mapped_column(Integer)
    period_month: Mapped[int] = mapped_column(Integer)
    status: Mapped[PayrollStatus] = mapped_column(Enum(PayrollStatus), default=PayrollStatus.DRAFT)
    gross_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    deduction_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    net_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list[PayrollItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class PayrollItem(Base):
    __tablename__ = "payroll_items"
    __table_args__ = (UniqueConstraint("run_id", "employee_id", name="uq_item_run_employee"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    working_days: Mapped[float] = mapped_column(Float, default=0)
    payable_days: Mapped[float] = mapped_column(Float, default=0)
    lop_days: Mapped[float] = mapped_column(Float, default=0)
    paid_leave_days: Mapped[float] = mapped_column(Float, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    gross: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    deductions: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    net: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    breakdown_json: Mapped[str] = mapped_column(Text, default="{}")

    run: Mapped[PayrollRun] = relationship(back_populates="items")
    employee: Mapped[Employee] = relationship()


class PayrollAdjustment(Base):
    """Manual correction applied on top of a calculated payroll item."""

    __tablename__ = "payroll_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    component_type: Mapped[ComponentType] = mapped_column(Enum(ComponentType))
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payslip(Base):
    __tablename__ = "payslips"
    __table_args__ = (UniqueConstraint("run_id", "employee_id", name="uq_payslip_run_employee"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    payslip_number: Mapped[str] = mapped_column(String(64), unique=True)
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), index=True, nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(160), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("biometric_devices.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="SUCCESS")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class TenantSetting(Base):
    __tablename__ = "tenant_settings"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_setting_tenant_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(Text, default="")


class EnrollmentToken(Base):
    """Single-use link an admin hands to an employee so they can register the
    biometric credential on their own phone."""

    __tablename__ = "enrollment_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
