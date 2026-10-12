const $ = id => document.getElementById(id);
let data = {};
let lastSearch = null;   // {kind: 'tenant' | 'account' | 'bucket', value}
let info = {};           // account -> bucket -> the listing's entry (quota, usage, location, settings...)
let tenantLocations = {};   // tenant -> storage locations, from config.json
let allLocations = [];
let locationLabels = {};   // name suffix -> label, from config.json
let tenants = [];
let tenantSource = 'accounts';
let replicationConfigured = false;   // config.json has a "cos2pag" section

// sa-<tenant>-<number>; mirrors tenant_of() on the server
function tenantOf(account) {
  const m = /^sa-(.+)-\d+$/.exec(account);
  if (m) return m[1];
  const rest = account.slice(3);
  return rest.includes('-') ? rest.slice(0, rest.lastIndexOf('-')) : rest;
}
// A tenant's storage locations: those declared for it in config.json, else the known container vaults
// named cv-<tenant> or cv-<tenant>-<number> (cv-act-01 and cv-act-02 belong to "act", cv-act-geoportal-01 does not).
function locationsOfTenant(tenant) {
  if (tenantLocations[tenant]) return tenantLocations[tenant];
  const escaped = tenant.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('^cv-' + escaped + '(-\\d+)?$');
  return allLocations.filter(n => re.test(n)).sort();
}
function labelFor(name) {
  for (const [suffix, label] of Object.entries(locationLabels)) if (name.endsWith(suffix)) return name + ' (' + label + ')';
  return name;
}
function setLocationSuggestions(account) {
  const own = locationsOfTenant(tenantOf(account));
  const list = $('locations-list');
  list.textContent = '';
  for (const name of own.length ? own : allLocations) { const o = el('option'); o.value = name; list.append(o); }
  return own;
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function setStatus(text, isError) { $('status').textContent = text; $('status').className = isError ? 'error' : ''; }
function showLog(log) { $('logbox').hidden = !log; $('log').textContent = log || ''; }

async function post(path, body) {
  let out;
  try {
    const res = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    try { out = await res.json(); } catch (e) { out = {error: 'Invalid response (HTTP ' + res.status + ')'}; }
  } catch (err) {
    out = {error: 'Error: ' + err};
  }
  return out;
}

function table(headers, rows) {
  const t = el('table');
  const head = el('tr');
  for (const h of headers) head.append(el('th', h));
  t.append(head);
  for (const row of rows) {
    const tr = el('tr');
    for (const cell of row) {
      tr.append(el('td', typeof cell === 'object' ? cell.text : cell, typeof cell === 'object' ? cell.cls : undefined));
    }
    t.append(tr);
  }
  return t;
}

// ---- tabs ---------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  for (const t of ['search', 'modify', 'create']) $('tab-' + t).hidden = t !== name;
}
document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
