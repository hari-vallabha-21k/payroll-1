/* Admin console for the payroll prototype: plain fetch + DOM, no build step. */

const state = {
  token: localStorage.getItem('token'),
  user: null,
  shifts: [],
  run: null,
  ruleSet: null,
  template: null,
  templateData: null,
};

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

  $('rs-from').value = $('rule-from').value = $('tpl-from').value = today();

  await Promise.all([
    loadDashboard(),
    loadEmployees(),
    loadAttendance(),
    loadLeave(),
    loadRuns(),
    loadDevices(),
    loadRuleSets(),
    loadTemplates(),
    loadCompany(),
  ]);
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
      <td>${pill(e.biometric_status)}</td>
      <td>${pill(e.status)}</td>
      <td><button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
            onclick="openBiometric(${e.id})">Biometric</button></td>
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
        branch: $('e-branch').value.trim() || null,
        pan: $('e-pan').value.trim().toUpperCase() || null,
        pf_number: $('e-pf').value.trim() || null,
        esi_number: $('e-esi').value.trim() || null,
        bank_account: $('e-bank').value.trim() || null,
      },
    });
    show(status, 'ok', 'Employee added.');
    ['e-code', 'e-first', 'e-last', 'e-branch', 'e-pan', 'e-pf', 'e-esi', 'e-bank'].forEach(
      (id) => {
        $(id).value = '';
      }
    );
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
$('pr-preview').addEventListener('click', previewPayroll);
$('rs-create').addEventListener('click', () => createRuleSet(false));
$('rs-starter').addEventListener('click', () => createRuleSet(true));
$('rule-save').addEventListener('click', saveRule);
$('rule-formula').addEventListener('input', validateFormula);
$('tpl-create').addEventListener('click', createTemplate);
$('tpl-refresh').addEventListener('click', refreshTemplatePreview);
$('tpl-pdf').addEventListener('click', previewTemplatePdf);
$('tpl-save').addEventListener('click', saveTemplate);
$('tpl-accent').addEventListener('change', refreshTemplatePreview);
$('co-save').addEventListener('click', saveCompany);

if (state.token) {
  startApp().catch(signOut);
}


// --- biometric authentication ---------------------------------------------
async function openBiometric(employeeId) {
  const panel = $('biometric-panel');
  panel.classList.remove('hidden');
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const data = await api(`/api/employees/${employeeId}/biometric`);
    renderBiometric(data);
  } catch (err) {
    $('biometric-body').innerHTML = `<div class="status bad">${err.message}</div>`;
  }
}

function renderBiometric(data) {
  const when = (value) => (value ? new Date(value + 'Z').toLocaleString() : '—');
  const rows = data.credentials
    .map(
      (c) => `<tr>
        <td>${c.device_name || c.device_label || 'Unnamed device'}</td>
        <td>${pill(c.status)}</td>
        <td>${when(c.created_at)}</td>
        <td>${when(c.last_used_at)}</td>
        <td>${c.status === 'ACTIVE'
          ? `<button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
               onclick="revokeCredential(${c.id}, ${data.employee_id})">Revoke</button>`
          : ''}</td>
      </tr>`
    )
    .join('');

  const pending = data.biometric_status === 'PENDING_VERIFICATION';
  const verified = data.biometric_status === 'VERIFIED';
  const disabled = data.biometric_status === 'DISABLED';

  $('biometric-body').innerHTML = `
    <div class="grid" style="margin-bottom:14px">
      <div class="stat"><div class="k">Employee</div><div class="n" style="font-size:16px">${data.employee_name}</div>
        <div class="muted">${data.employee_code}</div></div>
      <div class="stat"><div class="k">Biometric status</div><div style="margin-top:6px">${pill(data.biometric_status)}</div></div>
      <div class="stat"><div class="k">Employment status</div><div style="margin-top:6px">${pill(data.employment_status)}</div></div>
      <div class="stat"><div class="k">Attendance</div><div style="margin-top:6px">${
        data.attendance_enabled ? '✅ enabled' : '⛔ blocked'
      }</div></div>
    </div>
    <div class="grid" style="margin-bottom:14px">
      <div class="stat"><div class="k">Authentication method</div><div>${data.authentication_method}</div></div>
      <div class="stat"><div class="k">Registered on</div><div>${when(data.registered_on)}</div></div>
      <div class="stat"><div class="k">Verified on</div><div>${when(data.verified_on)}</div></div>
      <div class="stat"><div class="k">Last used</div><div>${when(data.last_used)}</div></div>
    </div>
    ${data.blocked_reason ? `<div class="alert">⚠ ${data.blocked_reason}</div>` : ''}
    ${data.note ? `<p class="muted">Note: ${data.note}</p>` : ''}
    <div class="row" style="margin:12px 0">
      <button class="secondary" style="width:auto;margin:0"
        onclick="issueEnrollment(${data.employee_id})">Register biometric</button>
      ${pending ? `<button style="width:auto;margin:0"
        onclick="biometricAction(${data.employee_id},'verify')">Verify</button>` : ''}
      ${pending ? `<button class="secondary" style="width:auto;margin:0"
        onclick="biometricAction(${data.employee_id},'reject')">Reject</button>` : ''}
      ${verified ? `<button class="secondary" style="width:auto;margin:0"
        onclick="biometricAction(${data.employee_id},'disable')">Disable</button>` : ''}
      ${disabled ? `<button style="width:auto;margin:0"
        onclick="biometricAction(${data.employee_id},'enable')">Re-enable</button>` : ''}
    </div>
    <h3 style="font-size:14px;margin:16px 0 6px">Credentials</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>Device</th><th>Status</th><th>Registered</th><th>Last used</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="muted">No credentials registered.</td></tr>'}</tbody>
    </table></div>`;
}

async function biometricAction(employeeId, action) {
  const needsReason = action === 'reject' || action === 'disable';
  const reason = needsReason
    ? window.prompt(`Reason for ${action}:`, action === 'reject' ? 'Identity could not be confirmed' : 'Temporarily suspended')
    : window.prompt('Note (optional):', 'Verified in person');
  if (needsReason && !reason) return;
  try {
    const data = await api(`/api/employees/${employeeId}/biometric/${action}`, {
      method: 'POST',
      body: { reason: reason || null },
    });
    renderBiometric(data);
    await loadEmployees();
  } catch (err) {
    alert(err.message);
  }
}

async function revokeCredential(credentialId, employeeId) {
  if (!window.confirm('Revoke this device? The employee will need to register again.')) return;
  try {
    await api(`/api/webauthn/credentials/${credentialId}`, { method: 'DELETE' });
    await openBiometric(employeeId);
    await loadEmployees();
  } catch (err) {
    alert(err.message);
  }
}

// --- payroll rules ---------------------------------------------------------
async function loadRuleSets() {
  const sets = await api('/api/payroll-rules/sets');
  table(
    $('rule-set-table'),
    ['Name', 'Version', 'Effective from', 'Effective to', 'Active', ''],
    sets,
    (r) => `<tr>
      <td>${r.name}</td>
      <td>v${r.version}</td>
      <td>${r.effective_from}</td>
      <td>${r.effective_to || '—'}</td>
      <td>${r.is_active ? '✅' : '—'}</td>
      <td><button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
            onclick="selectRuleSet(${r.id}, '${r.name.replace(/'/g, "\\'")}')">Open</button></td>
    </tr>`
  );
  if (!state.ruleSet && sets.length) selectRuleSet(sets[0].id, sets[0].name);
}

async function selectRuleSet(id, name) {
  state.ruleSet = id;
  $('rule-set-label').textContent = `— ${name}`;
  await Promise.all([loadRules(), loadVariables()]);
}

async function loadRules() {
  if (!state.ruleSet) return;
  const rules = await api(`/api/payroll-rules/sets/${state.ruleSet}/rules`);
  table(
    $('rule-table'),
    ['Priority', 'Code', 'Name', 'Type', 'Formula', 'Effective', 'v', ''],
    rules,
    (r) => `<tr>
      <td>${r.priority}</td>
      <td><code>${r.code}</code></td>
      <td>${r.name}</td>
      <td>${pill(r.rule_type)}</td>
      <td><code>${r.formula}</code></td>
      <td>${r.effective_from}${r.effective_to ? ' → ' + r.effective_to : ''}</td>
      <td>v${r.version}</td>
      <td><button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
            onclick="newRuleVersion(${r.id}, '${r.code}')">New version</button></td>
    </tr>`
  );
}

async function loadVariables() {
  const variables = await api(
    `/api/payroll-rules/variables${state.ruleSet ? '?rule_set_id=' + state.ruleSet : ''}`
  );
  $('variable-palette').innerHTML = variables
    .map(
      (v) => `<button class="secondary" title="${v.description}"
        style="width:auto;margin:0;padding:5px 9px;font-size:12px"
        onclick="insertVariable('${v.code}')">${v.code}</button>`
    )
    .join('');
}

function insertVariable(code) {
  const input = $('rule-formula');
  const at = input.selectionStart || input.value.length;
  input.value = input.value.slice(0, at) + code + input.value.slice(at);
  input.focus();
  validateFormula();
}

let validateTimer = null;
async function validateFormula() {
  const status = $('rule-status');
  const formula = $('rule-formula').value.trim();
  if (!formula) {
    status.classList.add('hidden');
    return;
  }
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    status.classList.remove('hidden');
    try {
      const result = await api('/api/payroll-rules/validate', {
        method: 'POST',
        body: { formula, rule_set_id: state.ruleSet },
      });
      if (result.ok) {
        show(status, 'ok', `Valid. Uses: ${result.variables_used.join(', ') || 'constants only'}`);
      } else {
        show(status, 'bad', result.error);
      }
    } catch (err) {
      show(status, 'bad', err.message);
    }
  }, 250);
}

function ruleBody() {
  return {
    code: $('rule-code').value.trim().toUpperCase(),
    name: $('rule-name').value.trim(),
    rule_type: $('rule-type').value,
    formula: $('rule-formula').value.trim(),
    priority: Number($('rule-priority').value || 100),
    effective_from: $('rule-from').value || today(),
  };
}

async function saveRule() {
  const status = $('rule-status');
  status.classList.remove('hidden');
  if (!state.ruleSet) {
    show(status, 'bad', 'Create or open a rule set first.');
    return;
  }
  try {
    await api(`/api/payroll-rules/sets/${state.ruleSet}/rules`, { method: 'POST', body: ruleBody() });
    show(status, 'ok', 'Rule saved.');
    $('rule-code').value = $('rule-name').value = $('rule-formula').value = '';
    await Promise.all([loadRules(), loadVariables()]);
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function newRuleVersion(ruleId, code) {
  const formula = window.prompt(`New formula for ${code} (the current one is kept for past payroll):`);
  if (!formula) return;
  const from = window.prompt('Effective from (YYYY-MM-DD):', today());
  if (!from) return;
  try {
    await api(`/api/payroll-rules/rules/${ruleId}/versions`, {
      method: 'POST',
      body: {
        code,
        name: code.replace(/_/g, ' '),
        rule_type: 'EARNING',
        formula,
        effective_from: from,
      },
    });
    await loadRules();
  } catch (err) {
    alert(err.message);
  }
}

async function createRuleSet(starter) {
  const status = $('rs-status');
  status.classList.remove('hidden');
  const name = $('rs-name').value.trim() || 'Standard Payroll';
  const from = $('rs-from').value || today();
  try {
    const created = starter
      ? await api(
          `/api/payroll-rules/sets/starter?effective_from=${from}&name=${encodeURIComponent(name)}`,
          { method: 'POST' }
        )
      : await api('/api/payroll-rules/sets', {
          method: 'POST',
          body: { name, effective_from: from },
        });
    show(status, 'ok', `Rule set "${created.name}" created.`);
    state.ruleSet = null;
    await loadRuleSets();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

// --- payroll preview -------------------------------------------------------
async function previewPayroll() {
  const period = selectedPeriod();
  const panel = $('preview-panel');
  panel.classList.remove('hidden');
  $('preview-body').innerHTML = '<p class="muted">Calculating…</p>';
  try {
    const previews = await api(
      `/api/payroll/preview?period_year=${period.period_year}&period_month=${period.period_month}`
    );
    $('preview-body').innerHTML = previews
      .map((p) => {
        const trace = p.trace
          .map(
            (line) => `<tr>
              <td>${line.priority}</td>
              <td><code>${line.code}</code></td>
              <td>${line.type}</td>
              <td class="n">${money(line.amount)}</td>
              <td class="muted">${line.error ? '⚠ ' + line.error : line.explanation}</td>
            </tr>`
          )
          .join('');
        return `<div class="panel" style="background:#f8fafc">
          <h3 style="margin:0 0 4px;font-size:15px">${p.employee_name} <span class="muted">${p.employee_code} · ${p.pay_period}</span></h3>
          <p class="muted">Calculated by ${p.source === 'rules' ? 'payroll rules' : 'salary structure'}
            ${p.rule_set_version ? '(rule set v' + p.rule_set_version + ')' : ''} ·
            payable ${p.payable_days}/${p.total_days} days · LOP ${p.lop_days}</p>
          <div class="table-wrap"><table>
            <thead><tr><th>Order</th><th>Component</th><th>Type</th><th>Amount</th><th>How it was calculated</th></tr></thead>
            <tbody>${trace || '<tr><td colspan="5" class="muted">No rule set configured; the salary structure was used.</td></tr>'}</tbody>
          </table></div>
          <p style="margin-top:8px"><strong>Gross ${money(p.gross)}</strong> ·
            Deductions ${money(p.total_deductions)} ·
            <strong>Net ${money(p.net)}</strong></p>
        </div>`;
      })
      .join('');
  } catch (err) {
    $('preview-body').innerHTML = `<div class="status bad">${err.message}</div>`;
  }
}

// --- payslip templates -----------------------------------------------------
async function loadTemplates() {
  const templates = await api('/api/payslip-templates');
  table(
    $('template-table'),
    ['Name', 'Version', 'Effective from', 'Effective to', 'Default', ''],
    templates,
    (t) => `<tr>
      <td>${t.name}</td>
      <td>v${t.version}</td>
      <td>${t.effective_from}</td>
      <td>${t.effective_to || '—'}</td>
      <td>${t.is_default ? '✅' : '—'}</td>
      <td><button class="secondary" style="width:auto;margin:0;padding:6px 10px;font-size:13px"
            onclick="openTemplate(${t.id})">Open</button></td>
    </tr>`
  );
}

async function openTemplate(id) {
  const template = await api(`/api/payslip-templates/${id}`);
  state.template = id;
  state.templateData = JSON.parse(template.template_data);
  renderSectionToggles();
  await refreshTemplatePreview();
}

async function ensureTemplateData() {
  if (!state.templateData) {
    const definition = await api('/api/payslip-templates/default-definition');
    state.templateData = definition.template_data;
    renderSectionToggles();
  }
  return state.templateData;
}

function renderSectionToggles() {
  const data = state.templateData;
  if (!data) return;
  $('section-toggles').innerHTML = data.sections
    .map(
      (section, index) => `<label style="font-weight:400;display:flex;gap:8px;align-items:center;margin:4px 0">
        <input type="checkbox" style="width:auto" ${section.enabled !== false ? 'checked' : ''}
          onchange="toggleSection(${index}, this.checked)" />
        ${section.type.replace(/_/g, ' ')}
      </label>`
    )
    .join('');
  const accent = (data.page && data.page.accent) || '#1f2937';
  $('tpl-accent').value = accent;
  const title = data.sections.find((s) => s.type === 'title');
  if (title) $('tpl-title').value = title.text || '';
}

function toggleSection(index, enabled) {
  state.templateData.sections[index].enabled = enabled;
  refreshTemplatePreview();
}

function applyBuilderInputs() {
  const data = state.templateData;
  data.page = data.page || {};
  data.page.accent = $('tpl-accent').value;
  const title = data.sections.find((s) => s.type === 'title');
  if (title) title.text = $('tpl-title').value;
  return data;
}

async function refreshTemplatePreview() {
  await ensureTemplateData();
  const data = applyBuilderInputs();
  try {
    const response = await fetch('/api/payslip-templates/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({ template_data: data, format: 'html' }),
    });
    const html = await response.text();
    if (!response.ok) throw new Error('Preview failed');
    $('tpl-preview').srcdoc = html;
  } catch (err) {
    show($('tpl-build-status'), 'bad', err.message);
    $('tpl-build-status').classList.remove('hidden');
  }
}

async function previewTemplatePdf() {
  await ensureTemplateData();
  const data = applyBuilderInputs();
  const response = await fetch('/api/payslip-templates/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
    body: JSON.stringify({ template_data: data, format: 'pdf' }),
  });
  const blob = await response.blob();
  window.open(URL.createObjectURL(blob), '_blank');
}

async function createTemplate() {
  const status = $('tpl-status');
  status.classList.remove('hidden');
  try {
    const created = await api('/api/payslip-templates', {
      method: 'POST',
      body: {
        name: $('tpl-name').value.trim() || 'Standard Payslip',
        effective_from: $('tpl-from').value || today(),
        is_default: $('tpl-default').value === 'true',
      },
    });
    show(status, 'ok', `Template "${created.name}" created.`);
    await loadTemplates();
    await openTemplate(created.id);
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function saveTemplate() {
  const status = $('tpl-build-status');
  status.classList.remove('hidden');
  if (!state.template) {
    show(status, 'bad', 'Open a template first, or create one.');
    return;
  }
  try {
    await api(`/api/payslip-templates/${state.template}`, {
      method: 'PUT',
      body: { template_data: applyBuilderInputs() },
    });
    show(status, 'ok', 'Template saved. Payslips already issued keep their original design.');
    await loadTemplates();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}


// --- company profile -------------------------------------------------------
async function loadCompany() {
  const company = await api('/api/settings/company');
  $('co-name').value = company.name || '';
  $('co-phone').value = company.phone || '';
  $('co-email').value = company.email || '';
  $('co-address').value = company.address || '';
  $('co-gst').value = company.gst_number || '';
  $('co-reg').value = company.registration_number || '';
  $('co-logo-preview').innerHTML = company.logo
    ? `<img src="${company.logo}" alt="Company logo" style="max-height:48px" />
       <button class="secondary" style="width:auto;margin:4px 0 0;padding:4px 8px;font-size:12px"
         onclick="removeLogo()">Remove</button>`
    : '<span class="muted">No logo</span>';
}

async function saveCompany() {
  const status = $('co-status');
  status.classList.remove('hidden');
  try {
    await api('/api/settings/company', {
      method: 'PUT',
      body: {
        name: $('co-name').value.trim(),
        phone: $('co-phone').value.trim(),
        email: $('co-email').value.trim(),
        address: $('co-address').value.trim(),
        gst_number: $('co-gst').value.trim(),
        registration_number: $('co-reg').value.trim(),
      },
    });

    const file = $('co-logo').files[0];
    if (file) {
      const form = new FormData();
      form.append('file', file);
      const response = await fetch('/api/settings/company/logo', {
        method: 'POST',
        headers: { Authorization: `Bearer ${state.token}` },
        body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Logo upload failed');
      $('co-logo').value = '';
    }

    show(status, 'ok', 'Company profile saved. Payslips already issued are unchanged.');
    await loadCompany();
    await refreshTemplatePreview();
  } catch (err) {
    show(status, 'bad', err.message);
  }
}

async function removeLogo() {
  try {
    await api('/api/settings/company/logo', { method: 'DELETE' });
    await loadCompany();
    await refreshTemplatePreview();
  } catch (err) {
    alert(err.message);
  }
}
