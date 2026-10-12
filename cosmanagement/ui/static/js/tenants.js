// ---- tenants -------------------------------------------------------------------
const picked = new Set();   // tenants ticked in the panel, kept while the filter changes

function searchTenants(names) {
  $('kind').value = 'tenant'; syncKind();
  $('query').value = names.join(',');
  runSearch('tenant', names.join(','), false);
}

function renderTenants(filter) {
  const box = $('tenants-panel');
  box.textContent = '';
  box.append(el('strong', tenants.length + ' tenant(s) ' + (tenantSource === 'vaults'
    ? '(deduced from the container vaults cv-<tenant>-<number>)' : '(deduced from the accounts sa-<tenant>-<number>)')));
  const input = el('input'); input.type = 'text'; input.placeholder = 'Filter...'; input.value = filter || '';
  input.addEventListener('input', () => renderTenants(input.value)); box.append(el('br'), input);
  const shown = tenants.filter(t => !filter || t.name.toLowerCase().includes(filter.toLowerCase()));
  const list = el('div', undefined, 'chips');
  const go = el('button', '', 'small');
  const updateGo = () => { go.textContent = 'Show the buckets (' + picked.size + ' tenant(s))'; go.disabled = !picked.size; };
  for (const t of shown) {
    const items = t.vaults || t.accounts;
    const label = el('label');
    label.style.cssText = 'display:inline-flex;gap:4px;align-items:center;border:1px solid var(--line);border-radius:6px;padding:2px 8px;background:var(--bg)';
    label.title = items.join(', ');
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = picked.has(t.name);
    cb.addEventListener('change', () => { cb.checked ? picked.add(t.name) : picked.delete(t.name); updateGo(); });
    label.append(cb, t.name + ' (' + items.length + ')');
    list.append(label);
  }
  go.addEventListener('click', () => searchTenants([...picked].sort()));
  const all = el('button', 'Tick all' + (filter ? ' (filtered)' : ''), 'small secondary');
  all.addEventListener('click', () => { shown.forEach(t => picked.add(t.name)); renderTenants(filter); });
  const none = el('button', 'Untick all', 'small secondary');
  none.addEventListener('click', () => { picked.clear(); renderTenants(filter); });
  const close = el('button', 'Close', 'small secondary');
  close.addEventListener('click', () => { box.textContent = ''; box.hidden = true; });
  const bar = el('div', undefined, 'row-buttons');
  bar.append(go, all, none, close);
  box.append(list, bar);
  updateGo();
  box.hidden = false;
  if (filter) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
}

$('tenants-go').addEventListener('click', async () => {
  const btn = $('tenants-go');
  btn.disabled = true;
  setStatus('Reading the tenants...', false);
  try {
    const res = await fetch('/api/tenants');
    const out = await res.json();
    showLog(out.log);
    if (!res.ok || out.error) { setStatus(out.error || ('HTTP error ' + res.status), true); return; }
    tenants = out.tenants;
    tenantSource = out.source;
    const list = $('tenants-datalist');
    list.textContent = '';
    for (const t of tenants) { const o = el('option'); o.value = t.name; list.append(o); }
    setStatus(out.message || (tenants.length + ' tenant(s) found.'), out.has_error);
    if (tenants.length) renderTenants(''); else { $('tenants-panel').hidden = true; }
  } catch (err) {
    setStatus('Error: ' + err, true);
  } finally {
    btn.disabled = false;
  }
});
