# Payroll & Attendance Management System

Restaurant payroll and attendance on one platform. Employees record attendance
from a web link using their **phone's own fingerprint sensor via WebAuthn**; the
same API accepts punches from a physical biometric machine through a connector,
so the payroll engine never has to know where a punch came from.

Built to the MVP scope in the product brief: authentication and roles, employee
records, WebAuthn attendance, shifts, leave, overtime, salary structures,
payroll runs, payslips (PDF), reports, device integration and audit logging —
multi-tenant from the first table.

Three engines are kept deliberately independent, so a client can differ in one
without disturbing the others:

```
ATTENDANCE ENGINE   biometric → authentication → standardized events
        ↓
PAYROLL ENGINE      rules → calculation → payroll result
        ↓
PAYSLIP ENGINE      payroll result → template → HTML / PDF
```

---

## Quick start

Requires **Python 3.10 or newer**. On Windows, use `py -3.11` (or `py -3.10`)
and `.venv\Scripts\` in place of `.venv/bin/`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                 # edit JWT_SECRET before anything real
.venv/bin/python -m uvicorn backend.app.main:app --reload
```

| URL | What it is |
| --- | --- |
| http://localhost:8000/admin | Admin / HR console |
| http://localhost:8000/attendance?tenant=REST001 | Employee attendance kiosk |
| http://localhost:8000/enroll?token=… | Single-use biometric enrolment page |
| http://localhost:8000/docs | OpenAPI browser |
| http://localhost:8000/api/health | Health check — reports `app`, and whether the frontend files were found |

### Port 8000 already in use

Windows in particular will let a second server bind a port that is already
taken, so both apps start cleanly and the browser silently reaches the wrong
one. `GET /api/health` returns `"app": "payroll-attendance"` — any other value
means a different service holds the port. Run elsewhere with
`--port 8001`, or stop the other app:

```powershell
netstat -ano | findstr :8000     # last column is the PID
taskkill /PID <pid> /F
```

### Something not working?

```bash
python check_setup.py
```

One report covering the Python version, the checked-out commit, whether the
frontend files are present, whether dependencies import, and — if a server is
running — whether it is serving current code. It names the fix for anything it
finds.

If a page returns **503** saying a frontend file was not found, the `frontend/`
folder is missing from the checkout. `GET /api/health` names the exact
directory that was searched; either restore the folder next to `backend/`, or
set `FRONTEND_DIR` in `.env` to wherever it lives.

The first boot seeds a demo restaurant (`REST001` — ABC Restaurant) with three
employees, two shifts, a ₹30,000 salary structure and an admin login:

```
admin@abcrestaurant.in / admin12345
```

Set `SEED_DEMO_DATA=false` for a clean database.

### Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

CI runs these on Python 3.10, 3.11 and 3.12, so a version-specific construct
cannot land unnoticed.

131 tests: the attendance rules (against the worked examples in the brief), the
payroll engine, the REST API end to end, the biometric lifecycle and its access
control, formula safety and evaluation, rule priority and versioning, template
rendering and version pinning, migrations from a pre-Alembic database, tenant
isolation and RBAC, and a **real browser WebAuthn ceremony** driven through
Chromium's virtual authenticator — enrolment, HR verification, check-in and
check-out. The browser test skips itself when Playwright or
Chromium is unavailable.

---

## Trying the phone-fingerprint flow

WebAuthn requires a secure context. The browser accepts `http://localhost`, but
a phone needs **HTTPS on a real hostname** (a tunnel such as `ngrok` or
`cloudflared` is the easiest route for a demo).

1. Point `WEBAUTHN_RP_ID` at the bare domain (`payroll.example.com`, no scheme
   or port) and `WEBAUTHN_ORIGIN` at the full origin (`https://payroll.example.com`).
   A mismatch here is the single most common cause of "verification failed".
2. In the admin console → **Employees** → **Enrolment link**, issue a link and
   open it on the employee's phone (valid 30 minutes, single use).
3. Register with the fingerprint. Only a credential id, public key and signature
   counter are stored.
4. Open `/attendance?tenant=REST001` on the phone, enter the employee ID and
   touch the sensor. The first punch is a check-in, the next a check-out.

**No fingerprint image or template ever reaches the server.** The phone's
operating system does the biometric match locally and signs a one-time
challenge; the server verifies the signature against the stored public key.

---

## Architecture

```
                    React-free static frontend (admin, kiosk, enrol)
                                     │  HTTPS / JSON
                                     ▼
                            FastAPI application
              ┌──────────────┬───────────────┬──────────────┐
              ▼              ▼               ▼              ▼
         Auth service   Attendance      Payroll        Device sync
         (JWT + RBAC)    processing      engine        (connector API)
              └──────────────┴───────────────┴──────────────┘
                                     ▼
                        SQLAlchemy → SQLite / PostgreSQL
```

The load-bearing decision: **biometric capture is kept separate from payroll.**
Every source produces the same standardized event —

```
employee_id · event_time · event_type · source · device_id
```

— so WebAuthn, a mock device and a real ZKTeco machine are interchangeable from
the payroll engine's point of view.

### Biometric verification

Biometric status is tracked separately from employment status, and attendance
needs both gates open:

```
NOT_REGISTERED → PENDING_VERIFICATION → VERIFIED → DISABLED
                        ↓ (rejected)                  ↓ (re-enabled)
                  NOT_REGISTERED                   VERIFIED
```

A new employee starts ACTIVE but NOT_REGISTERED. Registering a device moves
them to PENDING_VERIFICATION — **registration alone never enables attendance**.
An admin or HR user verifies, and only then does the kiosk let the employee
authenticate. An unverified employee is refused before any WebAuthn challenge
is issued, so their device is never even prompted.

Managed at **Employees → (employee) → Biometric**, or via
`/api/employees/{id}/biometric`.

### Raw vs processed attendance

`attendance_events` rows are **never edited**. `daily_attendance` is derived
from them and can be rebuilt at any time, which is what makes a payslip
auditable back to the punches behind it:

```
IN 09:02 · OUT 12:30 · IN 13:15 · OUT 18:05
        → worked 8h18m · first in 09:02 · last out 18:05 · PRESENT
```

An HR correction sets `is_manual_override` on the processed row, so
reprocessing leaves it alone until the override is explicitly cleared. Both the
correction and the clearing are written to `audit_logs`.

### Payroll rules

Payroll is configuration, not code. A tenant's rule set defines the components,
their formulas and the order they run in, so a different restaurant can pay
differently without a source change. A tenant with no rule set falls back to
the original salary-structure calculation, so existing installations are
unaffected until they opt in.

```
Employee → Salary Structure → Attendance → Payroll Rules → Engine → Result
```

Formulas are parsed into an AST and walked against a whitelist — no `eval`, no
imports, no attribute access, no calls beyond `MIN`, `MAX`, `ROUND`, `ABS`,
`IF`, `FLOOR`, `CEIL`. They are validated when saved, not when payroll runs.

```
HRA        = BASIC * 0.40              priority 20
OVERTIME   = OT_HOURS * OT_RATE        priority 40
PF         = MIN(BASIC * 0.12, 1800)   priority 60
LOP        = LOP_DAYS * DAILY_SALARY   priority 75
```

Rules execute in priority order and each result is fed back into scope, so
later rules build on earlier ones. `GROSS`, `TOTAL_DEDUCTIONS` and `NET` update
as the run proceeds. A rule that fails is reported on the preview and audit
trail rather than aborting the run.

Rules are versioned by effective date. Superseding a rule closes the previous
version the day before the new one starts, so **payroll already run keeps the
formula it was run with** — changing a rule never recalculates history.

**Preview** (`GET /api/payroll/preview`) calculates without saving and shows
how every figure was reached:

```
 20 HRA   EARNING   7200.00   BASIC * 0.40  (with BASIC = 18000.00)
 75 LOP   DEDUCTION 1000.00   LOP_DAYS * DAILY_SALARY  (with LOP_DAYS = 1, DAILY_SALARY = 1000)
```

Manual adjustments are included in the run and written to the audit log — never
applied silently.

### The built-in payroll fallback

Without rules, earnings are paid in full and unpaid days come off as an
explicit **Loss of Pay** line, so a payslip shows *why* the net differs from
the gross rather than silently pro-rating each component. Overtime minutes come
from processed attendance and are paid at the employee's hourly rate.

A run moves `DRAFT → CALCULATED → UNDER_REVIEW → APPROVED → PROCESSED`.
Approved payroll cannot be recalculated — an admin must `reopen` it with a
reason, which is audited. Payslips are only issued from approved runs and are
stored as an immutable JSON snapshot, rendered to PDF on demand.

### Payslip templates

Templates are presentation only: editing one can never move a salary figure,
and changing a formula never touches a template.

A template is an ordered list of sections (company header, employee details,
attendance, earnings/deductions, summary, footer) whose text fields accept
`{{placeholders}}`. Earnings and deductions render from whatever components the
employee actually has — no empty HRA row for someone without HRA, unless the
template asks for one.

Company details for the header — address, phone, email, GST, registration
number and an uploaded logo — are set under **Settings → Payroll → Company
profile**. The logo is stored inline as a data URI, so rendering a payslip
years later never depends on fetching an external file. Employee identifiers
the template can print (`{{pan}}`, `{{pf_number}}`, `{{esi_number}}`,
`{{branch}}`, `{{uan}}`, `{{bank_ifsc}}`) come from the employee record, are
all optional, and are editable on an existing employee — not only at creation.

The default template prints them, and a detail field marked
`"hide_when_empty": true` disappears when it has no value, so staff outside
PF/ESI get no hollow `ESI Number: -` row. Drop the flag to force a row to
print regardless. The HTML preview and the PDF follow the same rule.

Templates are versioned, and **every payslip stores the template it was issued
with**. Redesigning a template leaves issued payslips exactly as they were;
so do changes to salary, rules, designation or company details. Moving an old
payslip onto the current design is a deliberate, audited action
(`POST /api/payslips/{id}/regenerate?use_current_template=true`).

Built at **Settings → Payroll → Payslip Templates**, with a live preview in
HTML and PDF before saving.

### Device integration

`connector/mock_connector.py` is a working reference for the on-premise
connector: it reads punches (mocked here, a vendor SDK in production), maps
device user ids to employees, posts them to `/api/devices/{id}/sync` with the
device's own API key, spools failures to disk and retries with backoff.

Every punch carries a stable `external_ref` and the endpoint is idempotent on
it, so a retry after a network failure cannot double-punch:

```bash
python connector/mock_connector.py --device-id 1 --api-key <key> \
    --punches EMP001:09:02:IN EMP001:12:30:OUT EMP001:13:15:IN EMP001:18:05:OUT
```

Sync results are written to `sync_logs` and surfaced at
`GET /api/devices/{id}/status`.

---

## Roles

| Area | Admin | HR / Payroll | Manager | Employee |
| --- | --- | --- | --- | --- |
| Employees | Full | Full | Team | Own |
| Attendance | Full | Full | Team | Own |
| Corrections | Yes | Yes | — | — |
| Leave | Full | Full | Approve | Apply |
| Payroll | Full | Calculate | View | Own |
| Approve / reopen payroll | Yes | — | — | — |
| Devices | Full | Manage | — | — |
| Settings | Full | Limited | — | — |

---

## API

```
POST   /api/auth/login | /api/auth/logout | /api/auth/users      GET /api/auth/me
GET    /api/employees            POST /api/employees
GET    /api/employees/{id}       PUT  /api/employees/{id}        DELETE /api/employees/{id}

POST   /api/webauthn/lookup
POST   /api/webauthn/enrollment-token?employee_id=…
POST   /api/webauthn/register/options      | /register/verify
POST   /api/webauthn/authenticate/options  | /authenticate/verify
GET    /api/webauthn/credentials/{employee_id}   DELETE /api/webauthn/credentials/{id}

GET    /api/attendance           GET  /api/attendance/events
GET    /api/attendance/{employee_id}
POST   /api/attendance/punch     POST /api/attendance/process?work_date=…
POST   /api/attendance/{employee_id}/correction

GET/POST /api/shifts   /api/holidays   /api/leave-types   /api/leave-requests
POST   /api/leave-requests/{id}/decision
GET/POST /api/salary-structures        /api/employees/{id}/salary

GET    /api/employees/{id}/biometric
POST   /api/employees/{id}/biometric/verify | /reject | /disable | /enable
GET    /api/employees/biometric/pending

GET    /api/payroll-rules/variables | /functions
POST   /api/payroll-rules/validate
GET/POST /api/payroll-rules/sets       POST /api/payroll-rules/sets/starter
GET/POST /api/payroll-rules/sets/{id}/rules
PUT    /api/payroll-rules/rules/{id}   POST /api/payroll-rules/rules/{id}/versions

GET/POST /api/payslip-templates        GET /api/payslip-templates/default-definition
PUT    /api/payslip-templates/{id}     POST /api/payslip-templates/{id}/versions
POST   /api/payslip-templates/preview

GET    /api/payroll/preview
POST   /api/payroll/calculate
GET    /api/payroll   /api/payroll/{id}   /api/payroll/{id}/items/{employee_id}
POST   /api/payroll/{id}/adjustments | /review | /approve | /reopen | /process | /payslips
GET    /api/payslips   /api/payslips/{id}/pdf   /api/payslips/{id}/html
POST   /api/payslips/{id}/regenerate

GET/POST /api/devices            PUT  /api/devices/{id}
POST   /api/devices/{id}/enrollments
POST   /api/devices/{id}/sync    GET  /api/devices/{id}/status

GET    /api/dashboard   /api/reports/attendance-summary   /api/reports/attendance.csv
GET    /api/audit-logs  /api/settings   PUT /api/settings/{key}
GET/PUT  /api/settings/company
POST/DELETE /api/settings/company/logo
```

---

## Security

* Passwords hashed with bcrypt; JWT bearer sessions with role checks on every
  route, and tenant id checked on every query.
* WebAuthn requires user verification, rejects a signature counter that fails to
  advance (clone detection), and stores no biometric data.
* Enrolment is invite-only: an admin issues a single-use, 30-minute token, so
  the public pages cannot be used to enrol an unknown device.
* Attendance punches are timestamped by the **server**, never the client, and a
  repeat of the same punch type inside a 60-second window is refused.
* Device connectors authenticate with a per-device API key (stored hashed,
  shown once at registration) rather than staff credentials.
* The unauthenticated kiosk endpoints are rate limited per IP.
* Payroll approval, reopening, salary changes, attendance corrections, manual
  punches, leave decisions, biometric verification/rejection/disabling,
  credential revocation, payroll-rule and payslip-template changes are all
  written to `audit_logs` with the actor and the before/after detail.

Known limits of the prototype, worth closing before production: the rate
limiter is in-process (move it to Redis or the edge), and sessions are
stateless JWTs with no revocation list.

## Database migrations

The schema is owned by Alembic and upgraded automatically at startup.

A database created by the older `create_all` startup is **stamped** at the
baseline revision and then upgraded, never rebuilt, so live data survives. The
upgrade also backfills: an employee who already held an active credential is
marked `VERIFIED`, because they were checking in before the verification
workflow existed and must not be locked out by the upgrade.

```bash
.venv/bin/alembic current      # where this database stands
.venv/bin/alembic upgrade head # apply by hand, if you prefer
```

---

## Layout

```
backend/app/
  main.py            application, static pages, middleware
  models.py          all tables, every business row tenant-scoped
  schemas.py         request/response models
  security.py        bcrypt + JWT
  deps.py            auth dependencies, role guards, per-employee visibility
  audit.py           audit-log helper
  ratelimit.py       kiosk rate limiting
  seed.py            demo restaurant
  migrate.py         Alembic runner, stamps pre-Alembic databases
  services/
    attendance.py    raw events → processed daily attendance
    punch.py         recording a standardized event from any source
    biometric.py     biometric lifecycle and the attendance gate
    formula.py       safe formula parsing and evaluation
    payroll_rules.py rule resolution, ordering and execution
    payroll.py       the payroll engine (rules, with structure fallback)
    payslip.py       snapshot + PDF rendering
    payslip_template.py   template rendering, variables, amount-in-words
    webauthn_service.py   registration and authentication ceremonies
  routers/           one module per API area
backend/migrations/  Alembic revisions
backend/tests/       unit, API and browser end-to-end tests
frontend/            admin console, attendance kiosk, enrolment page
connector/           reference biometric-device connector
```

### Not in this MVP

Fingerprint-template storage, multiple biometric vendors, statutory tax
automation, bank payment files, a native mobile app and accounting
integrations are all deliberately out of scope.
