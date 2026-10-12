// ---- bulk creation from a JSON file --------------------------------------------
let plannedKey = null;
let plannedCount = 0;

function bulkRequest() {
  const text = $('b-doc').value.trim();
  if (!text) throw new Error('The configuration file is empty.');
  let document_;
  try { document_ = JSON.parse(text); } catch (e) { throw new Error('Invalid JSON: ' + e.message); }
  return {document: document_, credentials: document.querySelector('input[name=b-creds]:checked').value};
}

function invalidatePlan() {
  plannedKey = null;
  $('b-run').disabled = true;
  $('b-plan').textContent = '';
}

$('b-doc').addEventListener('input', invalidatePlan);
document.querySelectorAll('input[name=b-creds]').forEach(r => r.addEventListener('change', invalidatePlan));

$('b-load').addEventListener('click', () => $('b-file').click());
$('b-file').addEventListener('change', () => {
  const file = $('b-file').files[0];
  if (!file) return;
  if (file.size > 1024 * 1024) { setStatus('File too large (1 MB maximum).', true); return; }
  const reader = new FileReader();
  reader.onload = () => {
    $('b-doc').value = String(reader.result);
    invalidatePlan();
    setStatus('File "' + file.name + '" loaded. Preview before running.', false);
  };
  reader.onerror = () => setStatus('Cannot read the file.', true);
  reader.readAsText(file);
  $('b-file').value = '';
});

$('b-example').addEventListener('click', () => {
  $('b-doc').value = JSON.stringify({
    defaults: {storage_location: 'cv-dev-01', allowed_ips: ['10.0.0.1', '10.0.0.2'], quota_gb: 60, account_meta_name: 'dev'},
    accounts: [{id: 'sa-fina-0001'}],
    buckets: [
      {account: 'sa-fina-0001', name: 'fina-docs'},
      {account: 'sa-fina-0001', name: 'fina-archive', quota_gb: 200},
    ],
  }, null, 2);
  invalidatePlan();
});

async function loadInfo() {
  try {
    const info = await (await fetch('/api/info')).json();
    tenantLocations = info.tenants || {};
    locationLabels = info.labels || {};
    replicationConfigured = !!(info.replication && info.replication.configured);
    allLocations = info.storage_locations || [];
    for (const name of allLocations) { const o = el('option'); o.value = name; $('locations-list').append(o); }
    const envs = info.environments || [];
    $('b-legacy').hidden = !envs.length;
    const box = $('b-envs');
    box.textContent = '';
    for (const env of envs) {
      const label = el('label');
      const cb = el('input');
      cb.type = 'checkbox'; cb.value = env; cb.checked = env !== 'prd';
      label.append(cb, ' ' + env);
      box.append(label);
    }
  } catch (err) { setStatus('Cannot read the configuration: ' + err, true); }
}

$('b-expand').addEventListener('click', async () => {
  const environments = [...document.querySelectorAll('#b-envs input:checked')].map(i => i.value);
  const out = await post('/api/bulk/expand', {environments});
  if (out.error) { setStatus(out.error, true); return; }
  $('b-doc').value = JSON.stringify(out.document, null, 2);
  invalidatePlan();
  setStatus('File generated from config.json.' + (out.warnings.length ? ' ' + out.warnings.join(' ') : ''), out.warnings.length > 0);
});

$('b-preview').addEventListener('click', async () => {
  invalidatePlan();
  $('b-result').textContent = '';
  let req;
  try { req = bulkRequest(); } catch (err) { setStatus(err.message, true); return; }
  setStatus('Computing the plan...', false);
  const out = await post('/api/bulk/plan', req);
  if (out.error) { setStatus(out.error, true); return; }
  const plan = out.plan, box = $('b-plan');
  const total = plan.accounts.length + plan.buckets.length + plan.credentials.length;
  setStatus(plan.errors.length
    ? plan.errors.length + ' error(s) in the file: fix them to be able to run.'
    : total + ' operation(s) planned.', plan.errors.length > 0);
  if (plan.errors.length) {
    const list = el('div', undefined, 'err-list');
    for (const e of plan.errors) list.append(el('div', e));
    box.append(list);
  }
  if (plan.accounts.length) {
    box.append(el('h3', 'Storage accounts (' + plan.accounts.length + ')'));
    box.append(table(['Account', 'x-Account-Meta-name'], plan.accounts.map(a => [a.account_id, a.account_meta_name || ''])));
  }
  if (plan.buckets.length) {
    box.append(el('h3', 'Buckets (' + plan.buckets.length + ')'));
    box.append(table(['Account', 'Bucket', 'Quota (GB)', 'Storage location', 'IP'],
      plan.buckets.map(b => [b.account_id, b.bucket_name, String(b.quota_gb), b.storage_location, String(b.ip_count)])));
  }
  if (plan.credentials.length) {
    box.append(el('h3', (req.credentials === 'create' ? 'New credentials' : 'Existing credentials') + ' (' + plan.credentials.length + ')'));
    box.append(table(['Account'], plan.credentials.map(c => [c.account_id])));
  }
  if (total && !plan.errors.length) { plannedKey = JSON.stringify(req); plannedCount = total; $('b-run').disabled = false; }
});

$('b-run').addEventListener('click', async () => {
  let req;
  try { req = bulkRequest(); } catch (err) { setStatus(err.message, true); return; }
  if (JSON.stringify(req) !== plannedKey) { invalidatePlan(); setStatus('The file has changed: preview again.', true); return; }
  const n = String(plannedCount);
  if (!confirm('Run ' + n + ' operation(s) from this file?')) return;
  if (prompt('Type ' + n + ' (the number of operations) to confirm.') !== n) {
    setStatus('Run cancelled.', false);
    return;
  }
  $('b-run').disabled = true; $('b-preview').disabled = true;
  setStatus('Running (one API call per operation)...', false);
  const out = await post('/api/bulk/run', req);
  $('b-preview').disabled = false;
  if (out.log) showLog(out.log);
  if (out.error) { setStatus(out.error, true); return; }
  const failed = out.results.filter(r => !r.ok).length;
  setStatus(out.results.length + ' operation(s), ' + failed + ' failure(s).', failed > 0);
  const box = $('b-result');
  box.textContent = '';
  box.append(el('h3', 'Result'));
  box.append(table(['Step', 'Target', 'Result'],
    out.results.map(r => [r.kind, r.target, {text: r.message, cls: r.ok ? 'ok' : 'fail'}])));
  if (out.credentials.length) showCreds(out.credentials);
  invalidatePlan();
});
