// ---- search ---------------------------------------------------------------
let selected = null;   // {account, bucket}: the bucket the "Edit" tab works on

function cell(content, cls) {
  const td = el('td', typeof content === 'string' ? content : undefined, cls);
  if (typeof content !== 'string') td.append(content);
  return td;
}

function render() {
  const q = $('filter').value.trim().toLowerCase();
  const box = $('results');
  box.textContent = '';
  for (const [account, buckets] of Object.entries(data)) {
    const list = buckets.filter(b => b.toLowerCase().includes(q));
    if (q && !list.length) continue;
    const card = el('div', undefined, 'account');
    const h = el('h2', account);
    h.append(el('span', list.length + (q ? ' / ' + buckets.length : '') + ' bucket(s)'));
    card.append(h);
    if (list.length) {
      const t = el('table');
      const head = el('tr');
      for (const [title, cls] of [['Bucket', ''], ['Objects', 'num'], ['Used', 'num'], ['Quota', 'num'], ['% of quota', 'num'], ['', '']]) head.append(el('th', title, cls));
      t.append(head);
      for (const b of list) {
        const e = (info[account] || {})[b];
        const used = e ? num(e.bytes_used) : null, quota = e ? num(e.hard_quota) : null, objects = e ? num(e.object_count) : null;
        const p = pct(used, quota);
        const tr = el('tr');
        const pctCell = el('td', undefined, pctClass(p));
        pctCell.append(fmtPct(p) + ' ', bar(p));
        const actions = el('td');
        actions.style.whiteSpace = 'nowrap';
        for (const [act, label, cls] of [['view', 'View', 'secondary'], ['edit', 'Edit', '']]) {
          const btn = el('button', label, 'small ' + cls);
          btn.dataset.act = act; btn.dataset.bucket = b; btn.dataset.account = account;
          actions.append(btn, ' ');
        }
        tr.append(cell(b, 'mono'), cell(objects === null ? 'n/a' : objects.toLocaleString('en-GB'), 'num'), cell(fmtBytes(used), 'num'),
                  cell(fmtBytes(quota), 'num'), pctCell, actions);
        t.append(tr);
      }
      card.append(t);
    } else {
      card.append(el('div', 'No bucket', 'empty'));
    }
    const actions = el('div', undefined, 'actions');
    for (const [mode, label] of [['get', 'Get the credentials'], ['create', 'Generate new credentials']]) {
      const b = el('button', label, 'small secondary');
      b.dataset.account = account;
      b.dataset.cred = mode;
      actions.append(b);
    }
    card.append(actions);
    box.append(card);
  }
}

$('filter').addEventListener('input', render);

async function credentials(account, mode, btn) {
  if (mode === 'create' && !confirm('Generate a new credentials pair for ' + account + '?')) return;
  btn.disabled = true;
  setStatus(mode === 'get' ? 'Fetching the credentials...' : 'Generating the credentials...', false);
  const out = await post('/api/creds/' + mode, {account});
  btn.disabled = false;
  showLog(out.log);
  if (out.error || !out.ok) { setStatus(out.error || out.message, true); return; }
  setStatus(out.message, false);
  showCreds(out.credentials);
}

$('results').addEventListener('click', async e => {
  const act = e.target.closest('button[data-act]');
  if (act) {
    const {account, bucket} = act.dataset;
    if (act.dataset.act === 'edit') selectBucket(account, bucket);
    else await openBucket(account, bucket, 'view');
    return;
  }
  const b = e.target.closest('button[data-cred]');
  if (b) await credentials(b.dataset.account, b.dataset.cred, b);
});

// ---- the "Edit" tab: works on the bucket picked in the results ----------------------------------------
function renderSelected() {
  $('m-empty').hidden = !!selected;
  $('m-main').hidden = !selected;
  if (!selected) return;
  const {account, bucket} = selected;
  $('m-title').textContent = bucket + ' (' + account + ')';
  $('m-summary').textContent = '';
  $('m-summary').append(usageSummary((info[account] || {})[bucket]));
}

function selectBucket(account, bucket) {
  selected = {account, bucket};
  renderSelected();
  const actions = $('m-actions');
  actions.textContent = '';
  const add = (label, cls, handler) => { const b = el('button', label, 'small ' + cls); b.type = 'button'; b.addEventListener('click', () => handler(b)); actions.append(b); };
  add('Replicate to PAG', 'secondary', () => openReplication(account, bucket));
  add('Get the account credentials', 'secondary', b => credentials(account, 'get', b));
  add('Generate new credentials', 'secondary', b => credentials(account, 'create', b));
  add('Delete the bucket', 'danger', () => deleteBucket(account, bucket));
  closePanel($('m-repl'));
  showTab('modify');
  return openBucket(account, bucket, 'edit');
}

$('copy').addEventListener('click', () => {
  const q = $('filter').value.trim().toLowerCase();
  const names = Object.values(data).flat().filter(b => b.toLowerCase().includes(q));
  copyText(names.join('\n'), names.length + ' bucket(s)');
});

function resultsCsv() {
  const q = $('filter').value.trim().toLowerCase();
  const quote = v => '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
  const iso = ms => { const d = new Date(Number(ms)); return ms && Number.isFinite(d.getTime()) ? d.toISOString() : ''; };
  const lines = [['tenant', 'account', 'bucket', 'storage_location', 'quota_bytes', 'used_bytes', 'used_pct', 'objects', 'created_at']];
  for (const [account, buckets] of Object.entries(data)) for (const b of buckets) {
    if (!b.toLowerCase().includes(q)) continue;
    const e = (info[account] || {})[b] || {};
    const used = num(e.bytes_used), quota = num(e.hard_quota), p = pct(used, quota);
    lines.push([tenantOf(account), account, b, e.storage_location, quota, used, p === null ? '' : Math.round(p * 10) / 10, num(e.object_count), iso(e.creation_time)]);
  }
  return '﻿' + lines.map(l => l.map(quote).join(';')).join('\r\n') + '\r\n';
}

$('csv').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([resultsCsv()], {type: 'text/csv;charset=utf-8'}));
  a.download = 'buckets-' + new Date().toISOString().slice(0, 10) + '.csv';
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
});
$('print').addEventListener('click', () => window.print());

function fillAccounts() {
  const list = $('accounts-list');
  list.textContent = '';
  for (const account of Object.keys(data)) { const o = el('option'); o.value = account; list.append(o); }
}

const KIND_HELP = {
  tenant: ['fina or fi*', 'Full tenant name (fina) or pattern with * (fi*, *ina*); several tenants separated by commas (fina,act). "List tenants" lets you tick several tenants. One API call per account found.'],
  account: ['sa-fina-0001 or sa-fina-*', 'Full storage account name (sa-fina-0001) or pattern with * (sa-fina-*, *-0001). The sa- prefix may be left out.'],
  bucket: ['fina-docs or *docs*', 'Full bucket name: a single call. With * (*docs*, fina-*): every account is scanned, one API call per account, which can take a while.'],
};
function syncKind() {
  const [placeholder, help] = KIND_HELP[$('kind').value];
  $('query').placeholder = placeholder;
  $('search-help').textContent = help;
  if ($('kind').value === 'tenant') $('query').setAttribute('list', 'tenants-datalist'); else $('query').removeAttribute('list');
}
$('kind').addEventListener('change', syncKind);
syncKind();

async function runSearch(kind, value, keepMessage) {
  const go = $('go');
  lastSearch = {kind, value};
  go.disabled = true;
  if (!keepMessage) {
    const many = kind === 'bucket' && value.includes('*');
    setStatus(many ? 'Scanning every account (one API call per account, this can take a while)...' : 'Searching (one API call per account, this can take a moment)...', false);
  }
  $('results').textContent = '';
  closePanel($('bucket-panel'));
  $('toolbar').style.display = 'none';
  try {
    const res = await fetch('/api/search?kind=' + kind + '&q=' + encodeURIComponent(value));
    const out = await res.json();
    showLog(out.log);
    if (!res.ok || out.error) { setStatus(out.error || ('HTTP error ' + res.status), true); return; }
    data = out.accounts;
    info = out.info || {};
    const n = Object.keys(data).length;
    const total = Object.values(data).reduce((s, b) => s + b.length, 0);
    if (!keepMessage || out.has_error) {
      let msg;
      if (out.message) msg = out.message;
      else if (kind === 'bucket') {
        if (total) msg = total + ' bucket(s) found in ' + n + ' account(s)' + (out.scanned ? ' (' + out.scanned + ' account(s) scanned)' : '') + '.';
        else if (out.found_without_owner) msg = 'Bucket found, but the API response names no owner account (see the details): search with a pattern, for example *' + value + '*.';
        else if (value.includes('*')) msg = 'No bucket matches "' + value + '" in the ' + out.scanned + ' account(s) scanned.';
        else msg = 'No bucket named "' + value + '"' + (out.status ? ' (HTTP ' + out.status + ')' : '') +
                   '. If you know it exists, try a pattern: *' + value + '*.';
      } else {
        const what = kind === 'tenant' ? 'tenant' : 'account';
        msg = n ? n + ' account(s) for the ' + what + ' "' + value + '", ' + total + ' bucket(s).' : 'No ' + what + ' matches "' + value + '".';
        if (out.has_error) msg += ' Errors occurred, see the details.';
      }
      setStatus(msg, out.has_error);
    }
    $('filter').value = '';
    $('toolbar').style.display = total || n ? 'flex' : 'none';
    fillAccounts();
    if (kind === 'tenant' && !/[*,]/.test(value) && (!$('a-id').value.trim() || $('a-id').dataset.prefilled)) {
      $('a-id').value = 'sa-' + value + '-'; $('a-id').dataset.prefilled = '1';
    }
    render();
  } catch (err) {
    setStatus('Error: ' + err, true);
  } finally {
    go.disabled = false;
  }
}

const refreshSearch = () => lastSearch ? runSearch(lastSearch.kind, lastSearch.value, true) : Promise.resolve();

$('search').addEventListener('submit', e => { e.preventDefault(); runSearch($('kind').value, $('query').value.trim(), false); });
