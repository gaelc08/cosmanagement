// ---- bucket panel: view / edit / delete --------------------------------------
function flatten(value, path, rows) {
  if (value !== null && typeof value === 'object') {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    if (!entries.length) rows.push([path, Array.isArray(value) ? '[]' : '{}']);
    for (const [k, v] of entries) flatten(v, path ? path + '.' + k : String(k), rows);
  } else {
    rows.push([path, String(value)]);
  }
  return rows;
}

function findKey(value, key) {
  if (value === null || typeof value !== 'object') return undefined;
  if (!Array.isArray(value) && key in value) return value[key];
  for (const v of Object.values(value)) {
    const found = findKey(v, key);
    if (found !== undefined) return found;
  }
  return undefined;
}

// The response's key names are not documented: look for a numeric quota and the firewall's IP list by shape.
function findMatching(value, keyPattern, accept) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    if (Array.isArray(value)) for (const v of value) { const f = findMatching(v, keyPattern, accept); if (f !== undefined) return f; }
    return undefined;
  }
  for (const [k, v] of Object.entries(value)) if (keyPattern.test(k) && accept(v)) return v;
  for (const v of Object.values(value)) { const f = findMatching(v, keyPattern, accept); if (f !== undefined) return f; }
  return undefined;
}
const isNumber = v => typeof v === 'number';
const isStringList = v => Array.isArray(v) && v.every(x => typeof x === 'string');
function findQuota(details) { return findMatching(details, /^hard_quota$/i, isNumber) ?? findMatching(details, /quota/i, isNumber); }
function findIps(details) {
  return findMatching(details, /^allowed_ips?$/i, isStringList) ?? findMatching(details, /allowed.?ip|whitelist|firewall/i, isStringList);
}

const yesNo = v => v === true ? 'Yes' : v === false ? 'No' : String(v);
function fmtDate(v) { const n = Number(v); return Number.isFinite(n) && n > 0 ? new Date(n).toLocaleString('en-GB') : String(v); }
// [key in the listing, label, formatter]
const BUCKET_FIELDS = [
  ['storage_location', 'Storage location', v => labelFor(String(v))],
  ['creation_time', 'Created', fmtDate],
  ['hard_quota', 'Quota', v => fmtBytes(Number(v))],
  ['bytes_used', 'Usage', v => fmtBytes(Number(v))],
  ['object_count', 'Objects', v => Number(v).toLocaleString('en-GB')],
  ['versioning_state', 'Versioning', String],
  ['bucket_lifecycle_policy', 'Lifecycle policy', String],
  ['notifications_configuration', 'Notifications', String],
  ['static_website_enabled', 'Static website', yesNo],
  ['bucket_replication_policy', 'Replication', yesNo],
  ['object_lock_enabled', 'Object lock', yesNo],
  ['has_active_inventory_policy', 'Active inventory', yesNo],
  ['is_backup_vault', 'Backup vault', yesNo],
  ['is_legacy', 'Legacy', yesNo],
];

function closePanel(box) { box.textContent = ''; box.hidden = true; }

function panelHeader(title, box) {
  box.textContent = '';
  if (title) box.append(el('strong', title));
  box.hidden = false;
  return box;
}

// ---- usage: objects and size against the quota, from the bucket listing's entry ------------------------
const num = v => v === undefined || v === null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v);
function bar(p) {
  const b = el('span', undefined, 'bar' + (p !== null && p >= 100 ? ' crit' : p !== null && p >= 80 ? ' warn' : ''));
  const i = el('i');
  i.style.width = Math.max(0, Math.min(100, p === null ? 0 : p)) + '%';
  b.append(i);
  return b;
}
function usageSummary(entry) {
  const box = el('div', undefined, 'usage');
  if (!entry) { box.append(el('span', 'Objects and usage unavailable (bucket missing from the listing).', 'empty')); return box; }
  const objects = num(entry.object_count), used = num(entry.bytes_used), quota = num(entry.hard_quota);
  const p = pct(used, quota);
  box.append(el('strong', objects === null ? 'n/a' : objects.toLocaleString('en-GB')), ' object(s) \u00b7 ',
             el('strong', fmtBytes(used)), ' used of ', el('strong', fmtBytes(quota)), ' quota (' + fmtPct(p) + ')', bar(p));
  return box;
}

async function openBucket(account, bucket, mode) {
  setStatus('Reading bucket ' + bucket + '...', false);
  const out = await post('/api/bucket/get', {bucket});
  showLog(out.log);
  const ok = !out.error && out.ok;
  setStatus(ok ? '' : (out.error || out.message), !ok);
  const box = panelHeader(mode === 'edit' ? 'Edit the quota and the allowed IPs' : 'Bucket ' + bucket + ' (' + account + ')', $(mode === 'edit' ? 'm-edit' : 'bucket-panel'));

  if (mode === 'view') {
    const entry = (info[account] || {})[bucket];
    const details = ok ? out.details : null;
    box.append(usageSummary(entry));
    if (!ok) box.append(el('div', 'Bucket details (GET) unavailable: ' + (out.error || out.message) + '.', 'empty'));
    if (entry) {
      const pick = key => entry[key] !== undefined ? entry[key] : findKey(details, key);
      const used = pick('bytes_used'), quota = pick('hard_quota');
      const rows = [];
      for (const [key, label, fmt] of BUCKET_FIELDS) {
        const v = pick(key);
        if (v === undefined || v === null) continue;
        rows.push([label, key === 'bytes_used' ? fmtBytes(Number(v)) + (Number(quota) > 0 ? ' (' + fmtPct(pct(Number(v), Number(quota))) + ' of the quota)' : '') : fmt(v)]);
      }
      const ipList = findIps(details);
      if (ipList !== undefined) rows.push(['Allowed IPs (firewall)', ipList.length ? ipList.join(', ') : 'none']);
      const known = new Set(BUCKET_FIELDS.map(f => f[0]).concat(['name']));
      const extra = flatten(Object.fromEntries(Object.entries(entry).filter(([k]) => !known.has(k))), '', []);
      if (rows.length) box.append(table(['Information', 'Value'], rows));
      if (extra.length) {
        const more = el('details');
        more.append(el('summary', 'Other listing fields (' + extra.length + ')'), table(['Field', 'Value'], extra));
        box.append(more);
      }
      const rawEntry = el('details');
      rawEntry.append(el('summary', 'Raw listing entry'), el('pre', JSON.stringify(entry, null, 2)));
      box.append(rawEntry);
    } else if (ok) {
      const rows = details !== null && typeof details === 'object' ? flatten(details, '', []) : [];
      if (rows.length) box.append(table(['Field', 'Value'], rows));
    }
    if (ok) {
      const raw = el('details');
      raw.append(el('summary', 'Raw response of GET /container/' + bucket), el('pre', out.raw));
      box.append(raw);
    }
    const close = el('button', 'Close', 'small secondary');
    close.addEventListener('click', () => closePanel(box));
    box.append(close);
    return;
  }

  // edit: a PATCH changes only what it carries, so only the fields you changed are sent.
  const details = ok ? out.details : null;
  const quota = findQuota(details);
  const ips = findIps(details);
  box.append(el('div', 'Only the fields you change are sent (PATCH). The storage location cannot be changed here.', 'empty'));
  if (!ok) box.append(el('div', 'Details unreadable: the fields stay empty, fill in what you want to change.', 'empty'));
  else {
    const unread = [];
    if (quota === undefined) unread.push('the quota');
    if (ips === undefined) unread.push('the allowed IPs');
    if (unread.length) box.append(el('div', 'Not read from the API response: ' + unread.join(' and ') + '. Open "View" in "Search" to find them in the raw response.', 'empty'));
  }

  const form = el('form', undefined, 'grid-form');
  form.autocomplete = 'off';
  const fQuota = el('input'); fQuota.type = 'text'; fQuota.inputMode = 'numeric';
  fQuota.value = typeof quota === 'number' ? String(Math.round(quota / 1e9 * 100) / 100) : '';
  const fIps = el('textarea'); fIps.rows = 4; fIps.value = Array.isArray(ips) ? ips.join('\n') : '';
  form.append(el('label', 'Quota (GB)'), fQuota, el('label', 'Allowed IPs'), fIps);
  const ipList = v => v.split(/[\s,;]+/).filter(Boolean).join(',');
  const initial = {quota: fQuota.value.trim(), ips: ipList(fIps.value)};
  const buttons = el('div', undefined, 'buttons');
  const save = el('button', 'Save'); save.type = 'submit';
  buttons.append(save);
  form.append(buttons);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const quotaText = fQuota.value.trim();
    const newIps = ipList(fIps.value);
    // A quota shown as 53.69 (not a whole number of GB) is never sent unless you retype it.
    const body = {
      bucket,
      quota_gb: quotaText && quotaText !== initial.quota ? quotaText : '',
      allowed_ips: newIps && newIps !== initial.ips ? fIps.value : '',
    };
    if (!body.quota_gb && !body.allowed_ips) { setStatus('Nothing to send.', false); return; }
    const summary = 'Edit bucket "' + bucket + '"\n' +
      (body.quota_gb ? '  quota: ' + body.quota_gb + ' GB\n' : '') +
      (body.allowed_ips ? '  allowed IPs: ' + body.allowed_ips.split(/[\s,;]+/).filter(Boolean).length + '\n' : '');
    if (!confirm(summary)) return;
    save.disabled = true;
    setStatus('Updating...', false);
    const res = await post('/api/bucket/update', body);
    save.disabled = false;
    if (res.log) showLog(res.log);
    const done = !res.error && res.ok;
    setStatus(res.error || res.message, !done);
    if (done) {
      await refreshSearch();
      renderSelected();
      await openBucket(account, bucket, 'edit');
      setStatus(res.message, false);
    }
  });
  box.append(form);
}
