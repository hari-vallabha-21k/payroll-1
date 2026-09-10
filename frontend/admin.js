/* Admin console for the payroll prototype: plain fetch + DOM, no build step. */

const state = { token: localStorage.getItem('token'), user: null, shifts: [], run: null };

async function api(path, { method = 'GET', body, raw = false } = {}) {
  const headers = {};
  if (body) headers['Content-Type'] = 'application/json';
  if (state.token) headers.Authorization = `Bearer ${state.token}`;

  const response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (response.status === 401) {
    signOut();
    throw new Error('Session expired — please sign in again.');
  }
  if (raw) {
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return response.blob();
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function $(id) { return document.getElementById(id); }

function show(el, kind, text) {
  el.textContent = text;
  el.className = 'status ' + (kind || '');
}

function table(el, columns, rows, renderRow) {
  el.innerHTML =
    '<thead><tr>' + columns.map((c) => `<th>${c}</th>`).join('') + '</tr></thead>' +
    '<tbody>' + (rows.length
      ? rows.map(renderRow).join('')
      : `<tr><td colspan="${columns.length}" class="muted">Nothing to show.</td></tr>`) +
    '</tbody>';
}

function pill(value) {
  return `<span class="pill ${value}">${String(value).replace(/_/g, ' ')}</span>`;
}

function hhmm(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
}

function duration(total) {
  return `${Math.floor(total / 60)}h ${total % 60}m`;
}

function money(value) {
  return '₹' + Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2 });
}

function today() { return new Date().toISOString().slice(0, 10); }

// --- session ---------------------------------------------------------------
function signOut() {
  state.token = null;
  state.user = null;
  localStorage.removeItem('token');
  $('app-view').classList.add('hidden');
  $('session').classList.add('hidden');
  $('login-view').classList.remove('hidden');
}

async function signIn() {
  const status = $('login-status');
  status.classList.remove('hidden');
  show(status, 'busy', 'Signing in…');
  try {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: { email: $('email').value.trim(), password: $('password').value },
    });
    state.token = data.access_token;
    state.user = data.user;
    localStorage.setItem('token', data.access_token);
    status.classList.add('hidden');
    await startApp();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function startApp() {
  state.user = state.user || (await api('/api/auth/me'));
  $('login-view').classList.add('hidden');
  $('app-view').classList.remove('hidden');
  $('session').classList.remove('hidden');
  $('whoami').textContent = `${state.user.full_name} · ${state.user.role}`;

  $('a-date').value = today();
  $('e-join').value = today();
  $('pr-month').value = today().slice(0, 7);

  state.shifts = await api('/api/shifts');
  $('e-shift').innerHTML =
    '<option value="">No shift</option>' +
    state.shifts.map((s) => `<option value="${s.id}">${s.name}</option>`).join('');

  await Promise.all([loadDashboard(), loadEmployees(), loadAttendance(), loadLeave(), loadRuns(), loadDevices()]);
}

// --- dashboard -------------------------------------------------------------
async function loadDashboard() {
  const data = await api('/api/dashboard');
  const stats = [
    ['Present', data.present],
    ['Absent', data.absent],
    ['Late', data.late],
    ['On leave', data.on_leave],
    ['Employees', data.total_employees],
  ];
  $('stats').innerHTML = stats
    .map(([k, n]) => `<div class="stat"><div class="n">${n}</div><div class="k">${k}</div></div>`)
    .join('');

  $('alerts').innerHTML = data.alerts.length
    ? data.alerts.map((a) => `<div class="alert">⚠ ${a}</div>`).join('')
    : '<p class="muted">Nothing needs attention.</p>';

  const run = data.latest_payroll;
  $('latest-payroll').innerHTML = run
    ? `<strong>${run.period_month}/${run.period_year}</strong> — ${pill(run.status)}<br />
       Gross ${money(run.gross_total)} · Deductions ${money(run.deduction_total)} · Net ${money(run.net_total)}`
    : '<span class="muted">No payroll run yet.</span>';
}

// --- employees -------------------------------------------------------------
async function loadEmployees() {
  const employees = await api('/api/employees');
  table(
    $('employee-table'),
    ['ID', 'Name', 'Department', 'Shift', 'Biometric', 'Status', ''],
    employees,
    (e) => `<tr>
      <td>${e.employee_code}</td>
      <td>${e.full_name}</td>
      <td>${e.department ? e.department.name : '—'}</td>
      <td>${e.shift ? e.shift.name : '—'}</td>
      <td>${e.has_biometric ? '✅ registered' : '—'}</td>
      <td>${pill(e.status)}</td>
      <td><button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
            onclick="issueEnrollment(${e.id})">Enrolment link</button></td>
    </tr>`
  );
}

async function issueEnrollment(employeeId) {
  try {
    const data = await api(`/api/webauthn/enrollment-token?employee_id=${employeeId}`, { method: 'POST' });
    const expires = new Date(data.expires_at + 'Z').toLocaleTimeString();
    window.prompt(
      `Send this single-use link to ${data.employee_code}'s phone (valid until ${expires}):`,
      data.enroll_url
    );
  } catch (err) {
    alert(err.message);
  }
}

async function addEmployee() {
  const status = $('e-status');
  status.classList.remove('hidden');
  try {
    await api('/api/employees', {
      method: 'POST',
      body: {
        employee_code: $('e-code').value.trim().toUpperCase(),
        first_name: $('e-first').value.trim(),
        last_name: $('e-last').value.trim(),
        date_of_joining: $('e-join').value,
        shift_id: $('e-shift').value ? Number($('e-shift').value) : null,
      },
    });
    show(status, 'ok', 'Employee added.');
    $('e-code').value = $('e-first').value = $('e-last').value = '';
    await loadEmployees();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

// --- attendance ------------------------------------------------------------
async function loadAttendance() {
  const day = $('a-date').value || today();
  const [records, employees] = await Promise.all([
    api(`/api/attendance?start=${day}&end=${day}`),
    api('/api/employees'),
  ]);
  const names = Object.fromEntries(employees.map((e) => [e.id, `${e.employee_code} · ${e.full_name}`]));
  table(
    $('attendance-table'),
    ['Employee', 'First in', 'Last out', 'Worked', 'Late', 'Overtime', 'Status', 'Remarks'],
    records,
    (r) => `<tr>
      <td>${names[r.employee_id] || r.employee_id}</td>
      <td>${hhmm(r.first_in)}</td>
      <td>${hhmm(r.last_out)}</td>
      <td>${duration(r.worked_minutes)}</td>
      <td>${r.late_minutes ? r.late_minutes + 'm' : '—'}</td>
      <td>${r.overtime_minutes ? duration(r.overtime_minutes) : '—'}</td>
      <td>${pill(r.status)}</td>
      <td>${r.remarks || ''}</td>
    </tr>`
  );
}

async function reprocessDay() {
  const day = $('a-date').value || today();
  await api(`/api/attendance/process?work_date=${day}`, { method: 'POST' });
  await Promise.all([loadAttendance(), loadDashboard()]);
}

async function exportCsv() {
  const day = $('a-date').value || today();
  const blob = await api(`/api/reports/attendance.csv?start=${day}&end=${day}`, { raw: true });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `attendance-${day}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

async function manualPunch() {
  const status = $('p-status');
  status.classList.remove('hidden');
  try {
    const result = await api('/api/attendance/punch', {
      method: 'POST',
      body: {
        employee_code: $('p-code').value.trim().toUpperCase(),
        event_type: $('p-type').value,
        event_time: $('p-time').value,
        note: $('p-note').value.trim() || null,
      },
    });
    show(status, 'ok', `${result.employee_name}: ${result.event_type} recorded.`);
    await Promise.all([loadAttendance(), loadDashboard()]);
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

// --- leave -----------------------------------------------------------------
async function loadLeave() {
  const [requests, types, employees] = await Promise.all([
    api('/api/leave-requests'),
    api('/api/leave-types'),
    api('/api/employees'),
  ]);
  const typeNames = Object.fromEntries(types.map((t) => [t.id, t.name]));
  const names = Object.fromEntries(employees.map((e) => [e.id, e.employee_code]));
  table(
    $('leave-table'),
    ['Employee', 'Type', 'From', 'To', 'Reason', 'Status', ''],
    requests,
    (r) => `<tr>
      <td>${names[r.employee_id] || r.employee_id}</td>
      <td>${typeNames[r.leave_type_id] || r.leave_type_id}</td>
      <td>${r.start_date}</td>
      <td>${r.end_date}</td>
      <td>${r.reason || ''}</td>
      <td>${pill(r.status)}</td>
      <td>${r.status === 'PENDING'
        ? `<button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
             onclick="decideLeave(${r.id},'APPROVED')">Approve</button>
           <button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
             onclick="decideLeave(${r.id},'REJECTED')">Reject</button>`
        : ''}</td>
    </tr>`
  );
}

async function decideLeave(id, decision) {
  try {
    await api(`/api/leave-requests/${id}/decision`, { method: 'POST', body: { status: decision } });
    await Promise.all([loadLeave(), loadAttendance(), loadDashboard()]);
  } catch (err) {
    alert(err.message);
  }
}

// --- payroll ---------------------------------------------------------------
function selectedPeriod() {
  const [year, month] = ($('pr-month').value || today().slice(0, 7)).split('-');
  return { period_year: Number(year), period_month: Number(month) };
}

async function calculatePayroll() {
  const status = $('pr-status');
  status.classList.remove('hidden');
  show(status, 'busy', 'Calculating…');
  try {
    state.run = await api('/api/payroll/calculate', { method: 'POST', body: selectedPeriod() });
    show(status, 'ok', `Calculated ${state.run.items.length} employee(s). Net ${money(state.run.net_total)}.`);
    await renderRun();
    await loadRuns();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function renderRun() {
  if (!state.run) return;
  const employees = await api('/api/employees');
  const names = Object.fromEntries(employees.map((e) => [e.id, `${e.employee_code} · ${e.full_name}`]));
  table(
    $('payroll-table'),
    ['Employee', 'Payable days', 'LOP', 'Overtime', 'Gross', 'Deductions', 'Net'],
    state.run.items,
    (i) => `<tr>
      <td>${names[i.employee_id] || i.employee_id}</td>
      <td>${i.payable_days} / ${i.working_days}</td>
      <td>${i.lop_days}</td>
      <td>${duration(i.overtime_minutes)}</td>
      <td>${money(i.gross)}</td>
      <td>${money(i.deductions)}</td>
      <td><strong>${money(i.net)}</strong></td>
    </tr>`
  );
}

async function approvePayroll() {
  const status = $('pr-status');
  status.classList.remove('hidden');
  try {
    if (!state.run) throw new Error('Calculate the payroll first.');
    const run = await api(`/api/payroll/${state.run.id}/approve`, { method: 'POST' });
    show(status, 'ok', `Payroll approved — net ${money(run.net_total)}.`);
    await loadRuns();
    await loadDashboard();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function generatePayslips() {
  const status = $('pr-status');
  status.classList.remove('hidden');
  try {
    if (!state.run) throw new Error('Calculate the payroll first.');
    const result = await api(`/api/payroll/${state.run.id}/payslips`, { method: 'POST' });
    show(status, 'ok', `${result.total} payslip(s) ready. Open the run below to download.`);
    await loadRuns();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function loadRuns() {
  const runs = await api('/api/payroll');
  table(
    $('runs-table'),
    ['Period', 'Status', 'Gross', 'Deductions', 'Net', ''],
    runs,
    (r) => `<tr>
      <td>${String(r.period_month).padStart(2, '0')}/${r.period_year}</td>
      <td>${pill(r.status)}</td>
      <td>${money(r.gross_total)}</td>
      <td>${money(r.deduction_total)}</td>
      <td><strong>${money(r.net_total)}</strong></td>
      <td><button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
            onclick="openRun(${r.id})">Payslips</button></td>
    </tr>`
  );
}

async function openRun(runId) {
  const payslips = await api(`/api/payslips?run_id=${runId}`);
  if (!payslips.length) {
    alert('No payslips generated for this run yet.');
    return;
  }
  const lines = payslips
    .map((p) => `${p.snapshot.employee.code} — ${p.snapshot.employee.name} — net ₹${p.snapshot.totals.net}`)
    .join('\n');
  const pick = window.prompt(`${lines}\n\nEnter an employee ID to download their payslip PDF:`, payslips[0].snapshot.employee.code);
  if (!pick) return;
  const match = payslips.find((p) => p.snapshot.employee.code === pick.trim().toUpperCase());
  if (!match) {
    alert('No payslip for that employee.');
    return;
  }
  const blob = await api(`/api/payslips/${match.id}/pdf`, { raw: true });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${match.payslip_number}.pdf`;
  link.click();
  URL.revokeObjectURL(url);
}

// --- devices ---------------------------------------------------------------
async function loadDevices() {
  const devices = await api('/api/devices');
  table(
    $('device-table'),
    ['Code', 'Name', 'Brand', 'Address', 'Location', 'Status', 'Last sync'],
    devices,
    (d) => `<tr>
      <td>${d.device_code}</td>
      <td>${d.name}</td>
      <td>${d.brand || '—'}</td>
      <td>${d.ip_address ? d.ip_address + ':' + (d.port || '') : '—'}</td>
      <td>${d.location || '—'}</td>
      <td>${pill(d.status)}</td>
      <td>${d.last_sync_at ? new Date(d.last_sync_at + 'Z').toLocaleString() : '—'}</td>
    </tr>`
  );
}

async function addDevice() {
  const status = $('d-status');
  status.classList.remove('hidden');
  try {
    const result = await api('/api/devices', {
      method: 'POST',
      body: {
        device_code: $('d-code').value.trim().toUpperCase(),
        name: $('d-name').value.trim(),
        brand: $('d-brand').value.trim() || null,
        ip_address: $('d-ip').value.trim() || null,
        port: $('d-port').value ? Number($('d-port').value) : null,
      },
    });
    show(status, 'ok', `Device registered. API key (shown once):\n${result.api_key}`);
    await loadDevices();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

// --- wiring ----------------------------------------------------------------
document.querySelectorAll('nav.tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('nav.tabs button').forEach((b) => b.classList.remove('active'));
    button.classList.add('active');
    document.querySelectorAll('.tab').forEach((section) => section.classList.add('hidden'));
    $(`tab-${button.dataset.tab}`).classList.remove('hidden');
  });
});

$('login').addEventListener('click', signIn);
$('password').addEventListener('keydown', (e) => { if (e.key === 'Enter') signIn(); });
$('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (err) { /* token already gone */ }
  signOut();
});
$('e-save').addEventListener('click', addEmployee);
$('a-load').addEventListener('click', loadAttendance);
$('a-process').addEventListener('click', reprocessDay);
$('a-csv').addEventListener('click', exportCsv);
$('p-save').addEventListener('click', manualPunch);
$('pr-calc').addEventListener('click', calculatePayroll);
$('pr-approve').addEventListener('click', approvePayroll);
$('pr-slips').addEventListener('click', generatePayslips);
$('d-save').addEventListener('click', addDevice);

if (state.token) {
  startApp().catch(signOut);
}
