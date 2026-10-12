#!/usr/bin/env python3
"""
Local web UI to manage IBM COS storage accounts, buckets and credentials.

Usage:
    python cos_ui.py                        # http://127.0.0.1:8765, opens the browser
    python cos_ui.py --port 9000 --no-browser

Tabs:
  Search      by tenant, storage account or bucket, with a full name or a pattern using * (fina*, *-0001, *docs*).
              Every bucket shows its object count and its usage against its quota right in the results.
  Edit        the bucket selected in the results: quota, allowed IPs, replication to PAG, deletion, and its
              account's credentials
  Create      one storage account or bucket from typed-in values, or many from a JSON file that you load or
              paste in the page (format below), with a preview and a per-item result

Bulk file format (every key is optional, `defaults` fill what a bucket omits):
    {
      "defaults": {"storage_location": "...", "allowed_ips": ["10.0.0.1"],
                   "quota_gb": 60, "account_meta_name": "..."},
      "accounts": [{"id": "sa-fina-0001", "account_meta_name": "..."}],
      "buckets":  [{"account": "sa-fina-0001", "name": "fina-docs", "quota_gb": 100}]
    }

config.json (next to the script) only needs the `api` section (base_url, SSL);
credentials come from .env / environment / secrets.json (see
hive_management.load_auth_header()). The server only listens on 127.0.0.1.
Credentials (access/secret keys) are shown in the page for copying and are
never written to disk by the UI.

Listing the buckets of an account is flagged as resource-intensive by the API
guide, so a search only runs on demand, one account at a time, and the API is
never called by more than one request at once.
"""

import argparse
import contextlib
import io
import ipaddress
import json
import logging
import re
import sys
import threading
import time
import types
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

import hive_management as hm
import pag_teardown

ACCOUNT_RE = re.compile(r'^sa-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')          # names we create
BUCKET_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$')     # names that already exist
LABEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
CV_PREFIX = 'cv-'                                                      # container vault names
LABEL_SUFFIX_RE = re.compile(r'^[-_.A-Za-z0-9]{1,20}$')
VAULT_TENANT_RE = re.compile(r'^cv-(.+?)(?:-\d+)?$')
ACCOUNT_TENANT_RE = re.compile(r'^sa-(.+)-\d+$')
PATTERN_RE = re.compile(r'^[A-Za-z0-9._*,-]{1,2000}$')                   # a name, or a pattern where * is a wildcard
SEARCH_KINDS = ('tenant', 'account', 'bucket')
MAX_QUOTA_GB = 1_000_000
MAX_RETENTION_DAYS = 36500
MAX_BODY = 1024 * 1024
CREATED = (201,)
DONE = (200, 201, 202, 204)
MSG_NO_CREDS = 'Credentials not found (see the details).'
BULK_KEYS = {'defaults', 'accounts', 'buckets'}
DEFAULT_KEYS = {'storage_location', 'allowed_ips', 'quota_gb', 'account_meta_name'}
BUCKET_KEYS = DEFAULT_KEYS | {'account', 'name'}
ACCOUNT_KEYS = {'id', 'account_meta_name'}
_api_lock = threading.Lock()

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>COS management</title>
<style>
  :root { --bg:#fff; --fg:#1b1f24; --muted:#6a737d; --card:#f6f8fa; --line:#d0d7de; --accent:#0969da; --err:#cf222e; --ok:#1a7f37; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --card:#161b22; --line:#30363d; --accent:#58a6ff; --err:#ff7b72; --ok:#3fb950; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }
  main { max-width: 900px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 0 0 12px; }
  h3 { font-size: 15px; margin: 20px 0 6px; }
  .prefix, .mono { font-family: ui-monospace, monospace; }
  .prefix { color:var(--muted); }
  input, button, textarea, select { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:6px;
                                    background:var(--bg); color:var(--fg); }
  button { cursor:pointer; background:var(--accent); border-color:var(--accent); color:#fff; }
  button.secondary { background:var(--bg); color:var(--fg); border-color:var(--line); }
  button.danger { background:var(--bg); color:var(--err); border-color:var(--err); }
  button.small { padding:1px 8px; font-size:12px; }
  button:disabled { opacity:.6; cursor:not-allowed; }
  [hidden] { display:none !important; }

  #tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:16px; }
  #tabs button { background:none; color:var(--muted); border:0; border-bottom:2px solid transparent; border-radius:0; padding:8px 14px; }
  #tabs button.active { color:var(--fg); border-bottom-color:var(--accent); font-weight:600; }

  #search, #find, .inline-form { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  @media print {
    #tabs, #status, #creds, #bucket-panel, #tenants-panel, #logbox, #search, .toolbar, #tab-modify, #tab-create { display:none !important; }
    body { background:#fff; color:#000; } main { max-width:none; padding:0; }
  }
  #search input[type=text] { min-width:260px; flex:1; }
  #search-help { margin:6px 0 0; }
  .bar { height:6px; width:120px; background:var(--line); border-radius:3px; overflow:hidden; display:inline-block; vertical-align:middle; }
  .bar > i { display:block; height:100%; background:var(--accent); }
  .bar.warn > i { background:#9a6700; } .bar.crit > i { background:var(--err); }
  td.num, th.num { text-align:right; white-space:nowrap; }
  td.warn { color:#9a6700; font-weight:600; } td.crit { color:var(--err); font-weight:700; }
  .usage { font-size:15px; margin:4px 0 10px; }
  .usage .bar { width:240px; height:10px; margin-left:8px; }
  #tab-modify h2 { font:600 16px ui-monospace, monospace; margin:0 0 2px; }
  .grid-form .tenant-hint { font-size:12px; }
  .grid-form .tenant-hint.ok { color:var(--ok); }
  .grid-form .tenant-hint.bad { color:var(--err); }
  .tenant-hint button { margin-left:6px; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  #tenants-panel .chips { margin-top:8px; }
  .grid-form .chips { grid-column:2; margin-top:-4px; }
  @media (max-width: 560px) { .grid-form .chips { grid-column:1; } }
  #tenants-panel input[type=text] { margin-top:8px; min-width:220px; }
  #tenants-panel input[type=checkbox] { min-width:0; margin:0; }
  #status { margin:12px 0; color:var(--muted); min-height:1.5em; }
  #status.error { color:var(--err); }
  .toolbar { display:none; gap:8px; margin:8px 0 16px; flex-wrap:wrap; }

  .account { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 14px; margin-bottom:10px; }
  .account h2 { font:600 14px ui-monospace, monospace; margin:0 0 6px; display:flex; justify-content:space-between; gap:8px; }
  .account h2 span { color:var(--muted); font-weight:400; }
  .account ul { margin:0; padding-left:18px; font-family: ui-monospace, monospace; font-size:13px; }
  .account li { margin:3px 0; }
  .account li button { margin-left:6px; font-family:system-ui, sans-serif; }
  .account .actions { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
  .empty { color:var(--muted); font-size:13px; }

  .grid-form { display:grid; grid-template-columns: 200px 1fr; gap:8px 12px; align-items:start; }
  .grid-form label { padding-top:7px; }
  .grid-form small { color:var(--muted); grid-column:2; margin-top:-4px; }
  .grid-form textarea, .grid-form select, .grid-form input[type=text] { width:100%; }
  .grid-form .buttons { grid-column:2; display:flex; gap:8px; }
  .grid-form button { justify-self:start; }
  @media (max-width: 560px) { .grid-form { grid-template-columns:1fr; } .grid-form small, .grid-form .buttons { grid-column:1; } }
  fieldset { border:1px solid var(--line); border-radius:8px; margin:0 0 12px; padding:8px 12px; }
  fieldset label { margin-right:16px; display:inline-block; }
  #b-doc { width:100%; font:13px/1.4 ui-monospace, monospace; white-space:pre; overflow:auto; }
  .row-buttons { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; }

  table { border-collapse:collapse; width:100%; font-size:13px; margin:6px 0 12px; }
  th, td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  td.ok { color:var(--ok); } td.fail { color:var(--err); }
  .err-list { color:var(--err); font-size:13px; margin:6px 0; }

  .panel { background:var(--card); border:1px solid var(--accent); border-radius:8px; padding:10px 14px; margin:12px 0; }
  .panel pre { max-height:260px; overflow:auto; }
  #creds .cred { margin:8px 0 12px; }
  #creds .row { display:flex; gap:8px; align-items:center; margin:2px 0; flex-wrap:wrap; }
  #creds .row .k { width:110px; color:var(--muted); }
  #creds .row .v { font-family:ui-monospace, monospace; word-break:break-all; }

  #tab-bulk { margin-top:24px; color:var(--fg); font-size:15px; } #tab-bulk summary { font-weight:600; cursor:pointer; }
  details { margin-top:16px; color:var(--muted); font-size:13px; }
  pre { white-space:pre-wrap; background:var(--card); border:1px solid var(--line); border-radius:6px; padding:8px; }
</style>
</head>
<body>
<main>
  <h1>COS management</h1>
  <nav id="tabs">
    <button type="button" class="active" data-tab="search">Search</button>
    <button type="button" data-tab="modify">Edit</button>
    <button type="button" data-tab="create">Create</button>
  </nav>
  <div id="status"></div>
  <div id="creds" class="panel" hidden></div>

  <section id="tab-search">
    <form id="search">
      <select id="kind" aria-label="Search by">
        <option value="tenant">Tenant</option>
        <option value="account">Storage account</option>
        <option value="bucket">Bucket</option>
      </select>
      <input type="text" id="query" placeholder="full name or with *" autocomplete="off" autofocus required
             maxlength="2000" pattern="[A-Za-z0-9._*,\-]+" list="tenants-datalist">
      <button type="submit" id="go">Search</button>
      <button type="button" class="secondary" id="tenants-go">List tenants</button>
    </form>
    <datalist id="tenants-datalist"></datalist>
    <p class="empty" id="search-help"></p>
    <div id="tenants-panel" class="panel" hidden></div>
    <div class="toolbar" id="toolbar">
      <input type="text" id="filter" placeholder="Filter the results..." autocomplete="off">
      <button type="button" class="secondary" id="copy">Copy displayed buckets</button>
      <button type="button" class="secondary" id="csv">Export as CSV</button>
      <button type="button" class="secondary" id="print">Print</button>
    </div>
    <div id="bucket-panel" class="panel" hidden></div>
    <div id="results"></div>
  </section>

  <section id="tab-modify" hidden>
    <p class="empty" id="m-empty">No bucket selected: find it in "Search", then click "Edit".</p>
    <div id="m-main" hidden>
      <h2 id="m-title"></h2>
      <div id="m-summary"></div>
      <div class="row-buttons" id="m-actions"></div>
      <div id="m-edit" class="panel"></div>
      <div id="m-repl" class="panel" hidden></div>
    </div>
  </section>

  <section id="tab-create" hidden>
    <h3>Create a storage account</h3>
    <form id="account-form" class="grid-form" autocomplete="off">
      <label for="a-id">Account</label>
      <input type="text" id="a-id" required maxlength="132" placeholder="ex. sa-fina-0001">
      <small>sa-&lt;tenant&gt;-&lt;suffix&gt;</small>
      <label for="a-meta">x-Account-Meta-name</label>
      <input type="text" id="a-meta" placeholder="optional">
      <div class="buttons"><button type="submit" id="a-go">Create the account</button></div>
    </form>

    <h3>Create a bucket</h3>
    <form id="bucket-form" class="grid-form" autocomplete="off">
      <label for="c-account">Account</label>
      <input type="text" id="c-account" required list="accounts-list" maxlength="132" placeholder="ex. sa-fina-0001">
      <datalist id="accounts-list"></datalist>
      <label for="c-bucket">Bucket name</label>
      <input type="text" id="c-bucket" required maxlength="63" placeholder="ex. fina-docs">
      <small>3 to 63 characters: lowercase letters, digits, - and .</small>
      <label for="c-quota">Quota (GB)</label>
      <input type="text" id="c-quota" required inputmode="numeric" placeholder="ex. 60">
      <label for="c-location">Storage location</label>
      <input type="text" id="c-location" required list="locations-list" placeholder="ex. cv-dev-01">
      <datalist id="locations-list"></datalist>
      <div id="c-location-choices" class="chips"></div>
      <label for="c-ips">Allowed IPs</label>
      <textarea id="c-ips" rows="4" required placeholder="One IP (or CIDR) per line, or comma-separated"></textarea>
      <label for="c-meta">x-Account-Meta-name</label>
      <input type="text" id="c-meta" placeholder="optional">
      <div class="buttons"><button type="submit" id="c-go">Create the bucket</button></div>
    </form>

    <details id="tab-bulk"><summary>Bulk creation (JSON file)</summary>
    <p class="empty">Creates several accounts and buckets from a JSON configuration file. Load a file, paste its content, or
    start from the example, then preview before running. Format: <code>defaults</code> (default values for the buckets),
    <code>accounts</code> (accounts to create) and <code>buckets</code> (buckets to create).</p>
    <div class="row-buttons">
      <button type="button" class="secondary" id="b-load">Load a JSON file...</button>
      <input type="file" id="b-file" accept=".json,application/json" hidden>
      <button type="button" class="secondary" id="b-example">Insert an example</button>
    </div>
    <fieldset id="b-legacy" hidden><legend>Generate from config.json (hive_management.py format)</legend>
      <div id="b-envs"></div>
      <button type="button" class="secondary" id="b-expand">Generate the file</button>
    </fieldset>
    <textarea id="b-doc" rows="16" spellcheck="false" placeholder='{"accounts": [...], "buckets": [...]}'></textarea>
    <fieldset><legend>Credentials for the accounts in the file</legend>
      <label><input type="radio" name="b-creds" value="none" checked> none</label>
      <label><input type="radio" name="b-creds" value="get"> fetch the existing one</label>
      <label><input type="radio" name="b-creds" value="create"> generate a new one</label>
    </fieldset>
    <button type="button" id="b-preview">Preview</button>
    <button type="button" id="b-run" disabled>Run the plan</button>
    <div id="b-plan"></div>
    <div id="b-result"></div>
    </details>
  </section>

  <details id="logbox" hidden><summary>API call details</summary><pre id="log"></pre></details>
</main>
<script>
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

  // ---- credentials panel ----------------------------------------------------
  function copyText(text, label) {
    navigator.clipboard.writeText(text).then(
      () => setStatus(label + ' copied.', false),
      () => setStatus('Cannot copy (clipboard access denied).', true));
  }

  function showCreds(list) {
    const box = $('creds');
    box.textContent = '';
    box.append(el('strong', 'Credentials (shown here only, nothing is written to disk)'));
    for (const c of list) {
      const wrap = el('div', undefined, 'cred');
      wrap.append(el('div', c.account, 'mono'));

      const r1 = el('div', undefined, 'row');
      r1.append(el('span', 'Access key ID', 'k'), el('span', c.access_key_id, 'v'));
      const c1 = el('button', 'Copy', 'small secondary');
      c1.addEventListener('click', () => copyText(c.access_key_id, 'Access key ID'));
      r1.append(c1);

      const r2 = el('div', undefined, 'row');
      const secret = el('span', '••••••••••••', 'v');
      let shown = false;
      const reveal = el('button', 'Show', 'small secondary');
      reveal.addEventListener('click', () => {
        shown = !shown;
        secret.textContent = shown ? c.secret_key : '••••••••••••';
        reveal.textContent = shown ? 'Hide' : 'Show';
      });
      const c2 = el('button', 'Copy', 'small secondary');
      c2.addEventListener('click', () => copyText(c.secret_key, 'Secret key'));
      r2.append(el('span', 'Secret key', 'k'), secret, reveal, c2);

      wrap.append(r1, r2);
      box.append(wrap);
    }
    const all = el('button', 'Copy as JSON', 'small secondary');
    all.addEventListener('click', () => copyText(JSON.stringify({credentials: list}, null, 2), 'Credentials (JSON)'));
    const close = el('button', 'Clear', 'small secondary');
    close.addEventListener('click', () => { box.textContent = ''; box.hidden = true; });
    const bar = el('div');
    bar.style.display = 'flex'; bar.style.gap = '6px';
    bar.append(all, close);
    box.append(bar);
    box.hidden = false;
    box.scrollIntoView({block: 'nearest'});
  }

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

  // ---- replication to PAG (cos2pag) -------------------------------------------------
  const STEP_STATE = {
    preview: {CHANGED: 'To do', SKIPPED: 'Already in place', OK: 'OK'},
    run: {CHANGED: 'Done', SKIPPED: 'Already in place', OK: 'OK'},
  };

  const TEARDOWN_STATE = {
    preview: {CHANGED: 'To do', SKIPPED: 'Nothing to do', FAILED: 'Failed', NOT_RUN: 'Not run'},
    run: {CHANGED: 'Done', SKIPPED: 'Nothing to do', FAILED: 'Failed', NOT_RUN: 'Not run'},
  };
  const DAY_NAMES = {sunday: 'Sunday', monday: 'Monday', tuesday: 'Tuesday', wednesday: 'Wednesday', thursday: 'Thursday', friday: 'Friday', saturday: 'Saturday'};

  function openReplication(account, bucket) {
    const box = panelHeader('Replicate to PAG: ' + bucket + ' (' + account + ')', $('m-repl'));
    const close = el('button', 'Close', 'small secondary');
    close.type = 'button';
    close.addEventListener('click', () => closePanel(box));
    box.scrollIntoView({block: 'nearest'});
    if (!replicationConfigured) {
      box.append(el('p', 'Replication to PAG is not configured. Install cos2pag in the same Python environment (pip install <cos2pag folder>) ' +
        'and add to config.json: "cos2pag": {"config": "<path to cos2pag\'s config.yaml>", "env_file": "<path to its .env>"}.', 'empty'), close);
      return;
    }
    box.append(el('p', 'Chains what cos2pag does: COS (ACL, IP whitelist and notifications of the bucket), PAG (partition, repository, buffer, lifecycle), then ' +
      'PDR (replication task). Every step is idempotent. To edit an existing replication, change the retention then simulate: ' +
      'the steps that would change show as "To do". "Simulate" sends no change; "Run" is only possible after a successful simulation.', 'empty'));

    const statusBox = el('div');
    box.append(statusBox);
    const form = el('div', undefined, 'grid-form');
    const tenant = el('input');
    tenant.type = 'text'; tenant.maxLength = 64; tenant.value = tenantOf(account).toUpperCase();
    tenant.setAttribute('list', 'partitions-list');
    const tenantHint = el('small', '', 'tenant-hint');
    const retention = el('input');
    retention.type = 'text'; retention.inputMode = 'numeric'; retention.placeholder = 'cos2pag value (pag.lifecycle)';
    form.append(el('label', 'PAG tenant (partition)'), tenant,
                el('small', 'Exact name of the PAG partition (one per tenant, case matters). Check it: several tenants share a prefix (ME, ME-SR, ME-SRE...).'),
                tenantHint,
                el('label', 'Retention (days)'), retention,
                el('small', 'How long noncurrent versions are kept on PAG (--noncurrent-expiration-days). Empty: keep the value from cos2pag\'s config.yaml.'));
    const buttons = el('div', undefined, 'buttons');
    const simulate = el('button', 'Simulate (dry-run)'); simulate.type = 'button';
    const run = el('button', 'Run', 'secondary'); run.type = 'button'; run.disabled = true;
    buttons.append(simulate, run, close);
    form.append(buttons);
    const result = el('div');
    box.append(form, result);

    const key = () => tenant.value.trim() + '|' + retention.value.trim();
    let simulatedFor = null;
    const lock = () => { simulatedFor = null; run.disabled = true; };
    tenant.addEventListener('input', lock);
    retention.addEventListener('input', lock);

    async function refreshStatus(prefill) {
      statusBox.textContent = '';
      const out = await post('/api/replication/status', {bucket});
      if (out.error || !out.ok) {
        statusBox.append(el('div', 'Replication state unreadable: ' + (out.error || out.message), 'empty'));
        return;
      }
      const rows = [['Replication task (PDR)', out.pdr.found ? 'present (id ' + out.pdr.id + ')' : 'absent: not replicated yet']];
      const sch = out.pdr.schedule;
      if (out.pdr.found && sch) {
        rows.push(['Schedule', (sch.enabled ? 'enabled' : 'disabled') + (sch.days.length ? ' : ' + sch.days.map(d => DAY_NAMES[d]).join(', ') : '') +
                                    (sch.hour !== null && sch.hour !== undefined ? ' at ' + sch.hour + ':00' : '')]);
      }
      const days = out.lifecycle.noncurrent_days;
      rows.push(['Noncurrent version retention (PAG)', days !== null ? days + ' days' : (out.lifecycle.readable ? 'not set' : 'unreadable (no repository on PAG yet?)')]);
      statusBox.append(el('strong', 'Current state'), table(['Item', 'Value'], rows));
      if (prefill && days !== null && !retention.value.trim()) retention.value = String(days);
    }
    refreshStatus(true);

    // ---- suspend or undo --------------------------------------------------------------
    const down = el('fieldset');
    down.append(el('legend', 'Suspend or undo the replication'));
    for (const [value, title, text] of [
      ['suspend', 'Suspend the task in PDR', 'Disables the task\'s schedule and notifications, deleting nothing. Reversible: "Run" above resumes the replication.'],
      ['undo', 'Undo everything, keep the PAG repository', 'Deletes the PDR task and removes from COS the ACL and the PDR IP added for the replication. The PAG repository, its data and its lifecycle are kept.'],
      ['purge', 'Undo everything, including the PAG repository', 'As above, and deletes the PAG repository: the archived copies are lost. Irreversible.'],
    ]) {
      const label = el('label');
      label.style.display = 'block'; label.style.marginBottom = '6px';
      const radio = el('input');
      radio.type = 'radio'; radio.name = 'down-level'; radio.value = value; radio.checked = value === 'suspend';
      label.append(radio, el('strong', ' ' + title), el('div', text, 'empty'));
      down.append(label);
    }
    const downSim = el('button', 'Simulate'); downSim.type = 'button';
    const downRun = el('button', 'Run', 'danger'); downRun.type = 'button'; downRun.disabled = true;
    const downButtons = el('div', undefined, 'row-buttons');
    downButtons.append(downSim, downRun);
    const downResult = el('div');
    down.append(downButtons, downResult);
    box.append(down);

    const level = () => down.querySelector('input[name=down-level]:checked').value;
    const downKey = () => level() + '|' + tenant.value.trim();
    let downSimulated = null;
    const downLock = () => { downSimulated = null; downRun.disabled = true; };
    down.querySelectorAll('input[name=down-level]').forEach(r => r.addEventListener('change', downLock));
    tenant.addEventListener('input', downLock);

    async function callDown(dry) {
      const lv = level(), name = tenant.value.trim();
      if (lv === 'purge' && !name) { setStatus('Enter the PAG tenant to delete the repository.', true); return; }
      let typed;
      if (!dry) {
        if (lv === 'suspend') {
          if (!confirm('Suspend the replication of "' + bucket + '"? The PDR task is not deleted.')) return;
        } else {
          const warning = lv === 'purge' ? 'WARNING: the PAG repository will be deleted along with the archived copies. This is irreversible.\n\n' : '';
          typed = prompt(warning + 'Undo the replication of "' + bucket + '": delete the PDR task and remove the access added on COS.\nType the bucket name to confirm.');
          if (typed !== bucket) { setStatus('Operation cancelled.', false); return; }
        }
      }
      downSim.disabled = true; downRun.disabled = true;
      setStatus(dry ? 'Simulation in progress...' : 'Running...', false);
      downResult.textContent = '';
      const out = await post(dry ? '/api/replication/teardown/preview' : '/api/replication/teardown/run', {bucket, tenant: name, level: lv, confirm: typed});
      downSim.disabled = false;
      showLog(out.log);
      setStatus(out.error || out.message, !!out.error || !out.ok);
      if (out.steps && out.steps.length) {
        const states = TEARDOWN_STATE[dry ? 'preview' : 'run'];
        downResult.append(table(['Step', 'State', 'Detail'], out.steps.map(st => [st.name,
          {text: states[st.status] || st.status, cls: st.status === 'CHANGED' ? 'ok' : st.status === 'FAILED' ? 'fail' : undefined}, st.detail])));
      }
      if (dry && out.ok) { downSimulated = downKey(); downRun.disabled = false; }
      else { downSimulated = null; downRun.disabled = true; }
      if (!dry && out.steps && out.steps.length) refreshStatus(false);
    }
    downSim.addEventListener('click', () => callDown(true));
    downRun.addEventListener('click', () => callDown(false));

    async function call(dry) {
      const name = tenant.value.trim();
      if (!name) { setStatus('Enter the PAG tenant.', true); return; }
      const days = retention.value.trim();
      if (!dry && !confirm('Set up the replication of "' + bucket + '" to the PAG partition "' + name + '"?' +
          (days ? '\nNoncurrent version retention: ' + days + ' days.' : '') + '\n\n' +
          'This changes the bucket on the COS side (ACL, IP whitelist, notifications) and creates or updates objects in PAG and PDR.')) return;
      simulate.disabled = true; run.disabled = true;
      setStatus(dry ? 'Simulation in progress...' : 'Setting up the replication...', false);
      result.textContent = '';
      const out = await post(dry ? '/api/replication/preview' : '/api/replication/run', {bucket, tenant: name, noncurrent_days: days});
      simulate.disabled = false;
      showLog(out.log);
      setStatus(out.error || out.message, !!out.error || !out.ok);
      if (out.steps && out.steps.length) {
        const states = STEP_STATE[dry ? 'preview' : 'run'];
        result.append(table(['Step', 'State', 'Detail'], out.steps.map(st => [st.name, {text: states[st.status] || (st.status === 'FAILED' ? 'Failed' : st.status), cls: st.status === 'CHANGED' ? 'ok' : st.status === 'FAILED' ? 'fail' : undefined}, st.detail])));
      }
      if (dry && out.ok) { simulatedFor = key(); run.disabled = false; }
      else if (!dry && out.ok) { simulatedFor = null; refreshStatus(false); }
      else { run.disabled = simulatedFor !== key(); }
    }
    simulate.addEventListener('click', () => call(true));
    run.addEventListener('click', () => call(false));

    // ---- the tenant must name an existing partition exactly, or none at all -----------------
    // cos2pag looks the partition up by exact name; PAG refuses to create one whose name differs only by case.
    const partitionList = el('datalist');
    partitionList.id = 'partitions-list';
    box.append(partitionList);
    let partitionNames = null, tenantTouched = false;

    function describeTenant() {
      tenantHint.textContent = '';
      tenantHint.className = 'tenant-hint';
      const name = tenant.value.trim();
      if (partitionNames === null || !name) return;
      const same = partitionNames.filter(n => n.toLowerCase() === name.toLowerCase());
      if (partitionNames.includes(name)) {
        tenantHint.textContent = 'The PAG partition "' + name + '" exists: it will be used, nothing is created.';
        tenantHint.classList.add('ok');
      } else if (same.length) {
        tenantHint.classList.add('bad');
        tenantHint.append(same.length === 1
          ? 'A partition exists under the name "' + same[0] + '" (different case): cos2pag would recreate it and PAG would refuse. '
          : 'Several partitions differ only by case: ' + same.join(', ') + '. ');
        for (const n of same) {
          const use = el('button', 'Use "' + n + '"', 'small secondary');
          use.type = 'button';
          use.addEventListener('click', () => { tenant.value = n; tenantTouched = true; lock(); downLock(); describeTenant(); });
          tenantHint.append(use);
        }
      } else {
        tenantHint.textContent = 'No partition "' + name + '": cos2pag will create it with the new_partition_defaults of its config.yaml.';
      }
    }
    tenant.addEventListener('input', () => { tenantTouched = true; describeTenant(); });

    (async () => {
      const out = await post('/api/replication/partitions', {});
      if (out.error || !out.ok) {
        tenantHint.textContent = 'PAG partitions unreadable: ' + (out.error || out.message);
        tenantHint.classList.add('bad');
        return;
      }
      partitionNames = out.names;
      for (const n of partitionNames) { const o = el('option'); o.value = n; partitionList.append(o); }
      if (!tenantTouched) {   // the upper-cased guess is only a guess: prefer the partition that really exists
        const guess = tenantOf(account).toLowerCase();
        const best = partitionNames.includes(tenant.value) ? tenant.value : partitionNames.find(n => n.toLowerCase() === guess);
        if (best && best !== tenant.value) { tenant.value = best; lock(); downLock(); }
      }
      describeTenant();
    })();
  }

  async function deleteBucket(account, bucket) {
    const typed = prompt('Permanently delete the bucket "' + bucket + '" (account ' + account + ').\n' +
                         'This cannot be undone. Type the bucket name to confirm.');
    if (typed !== bucket) { setStatus('Deletion cancelled.', false); return; }
    setStatus('Deleting...', false);
    const out = await post('/api/bucket/delete', {bucket, confirm: typed});
    if (out.log) showLog(out.log);
    const ok = !out.error && out.ok;
    setStatus(out.error || out.message, !ok);
    if (ok) {
      for (const list of Object.values(data)) { const i = list.indexOf(bucket); if (i >= 0) list.splice(i, 1); }
      if (info[account]) delete info[account][bucket];
      if (lastSearch && lastSearch.kind === 'bucket') for (const acct of Object.keys(data)) if (!data[acct].length) delete data[acct];
      selected = null;
      renderSelected();
      showTab('search');
      render();
    }
  }

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
  $('a-id').addEventListener('input', () => { delete $('a-id').dataset.prefilled; });

  // The bucket form offers the storage locations of the account's tenant as buttons. A single one is filled in;
  // with several (internal / external endpoint) you choose, so nothing is picked silently.
  function syncCreateLocation() {
    const account = $('c-account').value.trim();
    const field = $('c-location');
    const choices = ACCOUNT_OK.test(account) ? setLocationSuggestions(account) : [];
    const box = $('c-location-choices');
    box.textContent = '';
    for (const name of choices) {
      const b = el('button', labelFor(name), 'small secondary');
      b.type = 'button';
      b.addEventListener('click', () => { field.value = name; delete field.dataset.auto; });
      box.append(b);
    }
    if (choices.length === 1 && (!field.value.trim() || field.dataset.auto)) { field.value = choices[0]; field.dataset.auto = '1'; }
    else if (field.dataset.auto && !choices.includes(field.value)) { field.value = ''; delete field.dataset.auto; }
  }
  const ACCOUNT_OK = /^sa-[A-Za-z0-9][A-Za-z0-9_-]*$/;
  $('c-account').addEventListener('input', syncCreateLocation);
  $('c-location').addEventListener('input', () => { delete $('c-location').dataset.auto; });

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

  // ---- create one account / one bucket ---------------------------------------
  async function afterCreate(account) {
    if (lastSearch && lastSearch.kind === 'tenant' && account.startsWith('sa-' + lastSearch.value + '-')) await refreshSearch();
  }

  $('account-form').addEventListener('submit', async e => {
    e.preventDefault();
    const body = {account: $('a-id').value.trim(), account_meta_name: $('a-meta').value.trim()};
    if (!confirm('Create the storage account "' + body.account + '"\n  x-Account-Meta-name: ' + (body.account_meta_name || '(none)'))) return;
    const btn = $('a-go');
    btn.disabled = true;
    setStatus('Creating the account...', false);
    const out = await post('/api/account/create', body);
    btn.disabled = false;
    if (out.log) showLog(out.log);
    setStatus(out.error || out.message, !!out.error || !out.ok);
    if (out.ok) await afterCreate(body.account);
  });

  $('bucket-form').addEventListener('submit', async e => {
    e.preventDefault();
    const body = {
      account: $('c-account').value.trim(),
      bucket: $('c-bucket').value.trim(),
      quota_gb: $('c-quota').value.trim(),
      storage_location: $('c-location').value.trim(),
      allowed_ips: $('c-ips').value,
      account_meta_name: $('c-meta').value.trim(),
    };
    const ips = body.allowed_ips.split(/[\s,;]+/).filter(Boolean).length;
    const summary = 'Create the bucket "' + body.bucket + '"\n' +
      '  account: ' + body.account + '\n' +
      '  quota: ' + body.quota_gb + ' GB\n' +
      '  storage location: ' + body.storage_location + '\n' +
      '  allowed IPs: ' + ips + '\n' +
      '  x-Account-Meta-name: ' + (body.account_meta_name || '(none)');
    if (!confirm(summary)) return;
    const btn = $('c-go');
    btn.disabled = true;
    setStatus('Creating the bucket...', false);
    const out = await post('/api/bucket/create', body);
    btn.disabled = false;
    if (out.log) showLog(out.log);
    setStatus(out.error || out.message, !!out.error || !out.ok);
    if (out.ok) {
      $('c-bucket').value = '';
      await afterCreate(body.account);
    }
  });

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

  // ---- formatting ----------------------------------------------------------------------
  function fmtBytes(n) {
    if (n === null || n === undefined) return 'n/a';
    const units = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
    let v = n, i = 0;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return (i === 0 ? String(v) : (Math.round(v * 100) / 100).toLocaleString('en-GB')) + ' ' + units[i];
  }
  function pct(used, quota) { return quota > 0 && used !== null && used !== undefined ? used / quota * 100 : null; }
  function fmtPct(p) { return p === null ? 'n/a' : (Math.round(p * 10) / 10).toLocaleString('en-GB') + ' %'; }
  function pctClass(p) { return p === null ? 'num' : p >= 100 ? 'num crit' : p >= 80 ? 'num warn' : 'num'; }

  loadInfo();
</script>
</body>
</html>
"""


LOOPBACK_NAMES = {'127.0.0.1', 'localhost', '[::1]'}


def host_allowed(value, allowed_hosts):
    """Is this Host (or Origin host) one we serve? Loopback names with any port, or a name given with --allow-host.

    Any port is fine for loopback: through a port forward (VS Code Remote, ssh -L) the browser's local port
    differs from the server's. DNS rebinding goes through a domain name, which is still refused.
    """
    if value in allowed_hosts:
        return True
    value = value.strip().lower()
    if value.startswith('['):
        name = value[:value.find(']') + 1]
        rest = value[len(name):]
    else:
        name, sep, rest = value.partition(':')
        rest = sep + rest
    return name in LOOPBACK_NAMES and (rest == '' or (rest.startswith(':') and rest[1:].isdigit()))


def build_allowed_hosts(port, extra=()):
    """Host header values accepted by the server: loopback, plus any --allow-host given.

    A name without a port is accepted with and without it, since a proxy or tunnel
    usually presents the default port (no port at all) to the server.
    """
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    for host in extra:
        host = host.strip()
        if host:
            hosts.add(host)
            if ':' not in host:
                hosts.add(f'{host}:{port}')
    return frozenset(hosts)


def call_api(func):
    """Run func() holding the API lock, capturing what hive_management prints.

    hive_management reports API problems by printing and returning what it
    got, so its output is handed to the UI instead of being lost on the server
    console. Returns (value, log, aborted); `aborted` is set when it called
    sys.exit (e.g. load_auth_header() found no credentials).
    """
    log = io.StringIO()
    value, aborted = None, False
    with _api_lock, contextlib.redirect_stdout(log):
        try:
            value = func()
        except SystemExit:
            aborted = True
    return value, log.getvalue().strip(), aborted


def _names_and_info(entries_by_account):
    """{account: [entries]} -> ({account: [names]}, {account: {name: entry}})."""
    names = {account: [e['name'] for e in entries] for account, entries in entries_by_account.items()}
    info = {account: {e['name']: e for e in entries} for account, entries in entries_by_account.items()}
    return names, info


def _find_key(value, key):
    """First value stored under `key` anywhere in a parsed JSON document, or None."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found is not None:
            return found
    return None


def _find_account(value):
    """First string anywhere in a parsed JSON document that looks like an account id (sa-...), or None."""
    if isinstance(value, str):
        return value if ACCOUNT_RE.match(value) else None
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        found = _find_account(child)
        if found:
            return found
    return None


def tenant_of(account_id):
    """Tenant an account belongs to: sa-<tenant>-<number>. Without a numeric suffix, everything before the last dash."""
    match = ACCOUNT_TENANT_RE.match(account_id)
    if match:
        return match.group(1)
    rest = account_id[len('sa-'):]
    return rest.rsplit('-', 1)[0] if '-' in rest else rest


def vault_tenant(vault):
    """Tenant a container vault belongs to: cv-<tenant>-<number> (cv-act-01 and cv-act-02 -> act).
    A vault without a number is its own tenant (cv-claas-lab -> claas-lab)."""
    return VAULT_TENANT_RE.match(vault).group(1)


def list_tenants(config, vaults=()):
    """Tenants for the "Lister les tenants" button.

    With a list of container vaults (storage_locations.txt) or tenants declared in config.json, the tenants are
    read from the vault names, with no API call. Without either, they are deduced from the account ids with one
    GET /accounts. Returns {source, tenants: [{name, vaults | accounts}], log, has_error, message}.
    """
    declared = tenant_locations(config)
    if vaults or declared:
        groups = {}
        for vault in vaults:
            if VAULT_TENANT_RE.match(vault):
                groups.setdefault(vault_tenant(vault), []).append(vault)
        for name, names in declared.items():
            known = groups.setdefault(name, [])
            known.extend(n for n in names if n not in known)
        return {
            'source': 'vaults',
            'tenants': [{'name': name, 'vaults': sorted(names)} for name, names in sorted(groups.items())],
            'log': '', 'has_error': False, 'message': '',
        }
    ids, log, aborted = call_api(lambda: [a['id'] for a in hm.list_accounts(config, prefix='sa-')])
    tenants = {}
    for account_id in ids or []:
        tenants.setdefault(tenant_of(account_id), []).append(account_id)
    return {
        'source': 'accounts',
        'tenants': [{'name': name, 'accounts': sorted(accounts)} for name, accounts in sorted(tenants.items())],
        'log': log,
        'has_error': aborted or any(line.startswith(('Error', 'Network error')) for line in log.splitlines()),
        'message': MSG_NO_CREDS if aborted else '',
    }


def parse_locations(text):
    """Storage location (container vault) names from a pasted list: the first word of each line
    that starts with cv-. Sizes, dates and column headers on the line are ignored."""
    names = []
    for line in text.splitlines():
        words = line.split()
        if words and words[0].startswith(CV_PREFIX) and LABEL_RE.match(words[0]) and words[0] not in names:
            names.append(words[0])
    return names


def load_locations(path):
    """Read the optional local list of storage locations. A missing file simply means none."""
    try:
        with open(path, encoding='utf-8') as f:
            return parse_locations(f.read())
    except FileNotFoundError:
        return []
    except OSError as err:
        print(f'Warning: cannot read {path}: {err}')
        return []


def location_labels(config):
    """Optional config.json "storage_location_labels": {"-01": "interne", ...}, shown next to a location whose name ends so."""
    labels = config.get('storage_location_labels')
    if not isinstance(labels, dict):
        return {}
    return {
        k: v for k, v in labels.items()
        if isinstance(k, str) and isinstance(v, str) and LABEL_SUFFIX_RE.match(k) and 0 < len(v) <= 40
    }


def tenant_locations(config):
    """Storage locations (container vaults) each tenant may use, from config.json's optional
    "tenants": {"<tenant>": {"storage_locations": ["...", ...]}}. Invalid entries are ignored."""
    result = {}
    tenants = config.get('tenants')
    if not isinstance(tenants, dict):
        return result
    for name, entry in tenants.items():
        names = entry.get('storage_locations') if isinstance(entry, dict) else None
        if isinstance(names, list):
            valid = [n for n in names if isinstance(n, str) and LABEL_RE.match(n)]
            if valid:
                result[str(name)] = valid
    return result


def _wildcard_regex(pattern):
    """Case-insensitive regex for a name or a pattern where * stands for any characters."""
    return re.compile('.*'.join(re.escape(part) for part in pattern.split('*')) + r'\Z', re.IGNORECASE)


def _error_in(log):
    return any(line.startswith(('Error', 'Network error')) for line in log.splitlines())


def search(config, kind, query):
    """Search by tenant, storage account or bucket, with a full name or a pattern using * (fina*, *-0001, *docs*).

    tenant   -> the accounts sa-<tenant>-<number> whose tenant matches, with all their buckets
    account  -> the accounts whose id matches (the sa- prefix may be left out), with all their buckets
    bucket   -> the buckets whose name matches, with their account. A full name costs one direct call;
                a pattern lists every account (one API call each).

    The bucket listing already carries each bucket's quota, usage and object count, so nothing more is
    needed to show them. Returns {kind, query, accounts: {id: [names]}, info: {id: {name: entry}}, scanned,
    status, found_without_owner, message, log, has_error}.
    """
    result = {
        'kind': kind, 'query': query, 'accounts': {}, 'info': {}, 'scanned': 0, 'status': None,
        'found_without_owner': False, 'message': '', 'has_error': False,
    }

    def collect(accounts, keep=None):
        for account in accounts:
            entries = [e for e in hm.list_buckets_info(config, account['id']) if keep is None or keep(e['name'])]
            if entries or keep is None:
                result['accounts'][account['id']] = [e['name'] for e in entries]
                result['info'][account['id']] = {e['name']: e for e in entries}
        result['scanned'] = len(accounts)

    def work():
        if kind == 'tenant':
            # one or several tenants (comma-separated), each a full name or a pattern
            patterns = [p.strip() for p in query.split(',') if p.strip()]
            matchers = [_wildcard_regex(p).match for p in patterns]
            prefixes = {p.split('*', 1)[0] for p in patterns}
            prefix = prefixes.pop() if len(prefixes) == 1 else ''
            collect([a for a in hm.list_accounts(config, prefix='sa-' + prefix)
                     if any(m(tenant_of(a['id'])) for m in matchers)])
        elif kind == 'account':
            pattern = query if query.lower().startswith('sa-') or query.startswith('*') else 'sa-' + query
            matches = _wildcard_regex(pattern).match
            collect([a for a in hm.list_accounts(config, prefix=pattern.split('*', 1)[0]) if matches(a['id'])])
        elif '*' in query:
            matches = _wildcard_regex(query).match
            collect(hm.list_accounts(config, prefix='sa-'), keep=matches)
        else:
            find_direct(query)

    def find_direct(name):
        if not BUCKET_NAME_RE.match(name):
            return
        status, text, error = _http(lambda: hm.get_bucket(config, name))
        result['status'] = status
        # Always show what the API answered, so a wrong endpoint or field name is visible.
        print(f'GET /container/{name} -> ' + (error or f'HTTP {status} - {text[:300]}'))
        if error:
            result.update(message=error, has_error=True)
        elif status == 200:
            try:
                details = json.loads(text)
            except json.JSONDecodeError:
                details = None
            owner = _find_key(details, 'service_instance')
            if not (isinstance(owner, str) and ACCOUNT_RE.match(owner)):
                owner = _find_account(details)
            if not owner:
                result['found_without_owner'] = True
                return
            result['accounts'][owner] = [name]
            # the listing of the owner account carries the quota, usage and object count
            collect([{'id': owner}], keep=lambda n: n == name)
            result['accounts'].setdefault(owner, [name])
        elif status != 404:
            result.update(message=f'Failed: HTTP {status} - {text[:500]}', has_error=True)

    _, log, aborted = call_api(work)
    if aborted:
        result.update(message=MSG_NO_CREDS, has_error=True)
    result['has_error'] = result['has_error'] or _error_in(log)
    result['accounts'] = dict(sorted(result['accounts'].items()))
    result['log'] = log
    return result


def _http(thunk):
    """Run a (status, text) API call. Returns (status, text, error); status is None on a network error."""
    try:
        status, text = thunk()
    except requests.RequestException as err:
        return None, '', f'Network error: {err}'
    return status, text, None


def _write_outcome(thunk, ok_message, ok_statuses=CREATED):
    """Run a write call; returns (ok, message)."""
    status, text, error = _http(thunk)
    if error:
        return False, error
    if status in ok_statuses:
        return True, ok_message
    return False, f'Failed: HTTP {status} - {text[:500]}'


def _run_write(thunk, ok_message, ok_statuses=CREATED):
    outcome, log, aborted = call_api(lambda: _write_outcome(thunk, ok_message, ok_statuses))
    ok, message = (False, MSG_NO_CREDS) if aborted else outcome
    return {'ok': ok, 'message': message, 'log': log}


def _text(body, key):
    """A body field as stripped text; a missing field or a JSON null is empty (not the string "None")."""
    value = body.get(key)
    return '' if value is None else str(value).strip()


def _valid_account(body):
    account = _text(body, 'account')
    return account if ACCOUNT_RE.match(account) else None


def _valid_meta(value):
    """Optional x-Account-Meta-name. Returns (value_or_None, error)."""
    meta = str(value or '').strip()
    if meta and not LABEL_RE.match(meta):
        return None, 'Invalid value for the x-Account-Meta-name header.'
    return meta or None, None


def _parse_quota(value):
    """Quota in GB from an int or a numeric string. Returns (int, error)."""
    if isinstance(value, bool):
        return None, 'Invalid quota (whole number of GB).'
    try:
        quota = int(str(value).strip())
    except ValueError:
        return None, 'Invalid quota (whole number of GB).'
    if not 1 <= quota <= MAX_QUOTA_GB:
        return None, f'Invalid quota (between 1 and {MAX_QUOTA_GB} GB).'
    return quota, None


def _parse_ips(value):
    """IPs / CIDRs from a list or a separated string. Returns (list, error); an empty list is not an error."""
    if isinstance(value, str):
        raws = re.split(r'[\s,;]+', value.strip())
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        raws = [v.strip() for v in value]
    else:
        return None, 'Invalid IP list.'
    ips = []
    for raw in filter(None, raws):
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            return None, f'Invalid IP address: {raw[:64]}'
        if raw not in ips:
            ips.append(raw)
    return ips, None


def _parse_location(value):
    location = str(value or '').strip()
    return (location, None) if LABEL_RE.match(location) else (None, 'Invalid storage location.')


def parse_create_bucket(body):
    """Validate the JSON body of POST /api/bucket/create. Returns (params, error)."""
    account = _valid_account(body)
    if not account:
        return None, 'Invalid account (expected: sa-<tenant>-<suffix>).'
    bucket = _text(body, 'bucket')
    if not BUCKET_RE.match(bucket):
        return None, 'Invalid bucket name (3 to 63 characters: lowercase letters, digits, - and .).'
    quota_gb, error = _parse_quota(body.get('quota_gb', ''))
    if error:
        return None, error
    location, error = _parse_location(body.get('storage_location'))
    if error:
        return None, error
    ips, error = _parse_ips(body.get('allowed_ips', ''))
    if error:
        return None, error
    if not ips:
        return None, 'At least one allowed IP is required.'
    meta, error = _valid_meta(body.get('account_meta_name'))
    if error:
        return None, error
    return {
        'account_id': account,
        'bucket_name': bucket,
        'quota_gb': quota_gb,
        'storage_location': location,
        'allowed_ips': ips,
        'account_meta_name': meta,
    }, None


def handle_create_bucket(config, body):
    params, error = parse_create_bucket(body)
    if error:
        return 400, {'error': error}
    return 200, _run_write(
        lambda: hm.create_bucket(config, **params),
        f"Bucket {params['bucket_name']} created in {params['account_id']}.",
    )


def _existing_bucket(body):
    name = _text(body, 'bucket')
    return name if BUCKET_NAME_RE.match(name) else None


def handle_get_bucket(config, body):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    result, log, aborted = call_api(lambda: _http(lambda: hm.get_bucket(config, bucket)))
    if aborted:
        return 200, {'ok': False, 'message': MSG_NO_CREDS, 'log': log}
    status, text, error = result
    if error or status != 200:
        return 200, {'ok': False, 'log': log, 'message': error or f'Failed: HTTP {status} - {text[:500]}'}
    try:
        details = json.loads(text)
    except json.JSONDecodeError:
        details = None
    return 200, {'ok': True, 'details': details, 'raw': text[:20000], 'log': log}


def parse_update_bucket(body):
    """Validate the JSON body of POST /api/bucket/update (a PATCH: only the fields given are changed)."""
    bucket = _existing_bucket(body)
    if not bucket:
        return None, 'Invalid bucket name.'
    params = {'bucket_name': bucket}
    if _text(body, 'quota_gb'):
        params['quota_gb'], error = _parse_quota(body['quota_gb'])
        if error:
            return None, error
    if _text(body, 'allowed_ips'):
        ips, error = _parse_ips(body['allowed_ips'])
        if error:
            return None, error
        if not ips:
            return None, 'At least one allowed IP is required.'
        params['allowed_ips'] = ips
    if _text(body, 'storage_location'):
        params['storage_location'], error = _parse_location(body['storage_location'])
        if error:
            return None, error
    if len(params) == 1:
        return None, 'Nothing to change.'
    return params, None


def handle_update_bucket(config, body):
    params, error = parse_update_bucket(body)
    if error:
        return 400, {'error': error}
    return 200, _run_write(
        lambda: hm.update_bucket(config, **params),
        f"Bucket {params['bucket_name']} updated.",
        DONE,
    )


def handle_delete_bucket(config, body):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    if _text(body, 'confirm') != bucket:
        return 400, {'error': 'Wrong confirmation: type the exact bucket name.'}
    return 200, _run_write(
        lambda: hm.delete_bucket(config, bucket),
        f'Bucket {bucket} deleted.',
        DONE,
    )


def handle_create_account(config, body):
    account = _valid_account(body)
    if not account:
        return 400, {'error': 'Invalid account (expected: sa-<tenant>-<suffix>).'}
    meta, error = _valid_meta(body.get('account_meta_name'))
    if error:
        return 400, {'error': error}
    return 200, _run_write(
        lambda: hm.create_storage_account(config, account, meta),
        f'Account {account} created.',
    )


def handle_credentials(config, body, create):
    account = _valid_account(body)
    if not account:
        return 400, {'error': 'Invalid account (expected: sa-<tenant>-<suffix>).'}
    fetch = hm.request_credential_for if create else hm.fetch_credential_for
    cred, log, aborted = call_api(lambda: fetch(config, account))
    if aborted:
        return 200, {'ok': False, 'message': MSG_NO_CREDS, 'log': log, 'credentials': []}
    if not cred:
        return 200, {
            'ok': False, 'log': log, 'credentials': [],
            'message': f"Failed to {'generate' if create else 'get'} the credentials (see the details).",
        }
    return 200, {
        'ok': True, 'log': log,
        'message': f"Credentials {'generated' if create else 'fetched'} for {account}.",
        'credentials': [{
            'account': account,
            'access_key_id': cred['access_key_id'],
            'secret_key': cred['secret_key'],
        }],
    }


def parse_bulk_document(document):
    """Turn a bulk file into a plan. Returns (plan, errors).

    plan = {accounts: [{account_id, account_meta_name}],
            buckets: [create_bucket() keyword arguments],
            credentials: [{account_id}]}
    Items that fail validation are left out and reported in `errors`; the
    caller must not run a plan that has errors.
    """
    plan = {'accounts': [], 'buckets': [], 'credentials': []}
    if not isinstance(document, dict):
        return plan, ['The file must be a JSON object: {"accounts": [...], "buckets": [...]}.']
    errors = [f'Unknown key at the root: {str(key)[:40]}' for key in document if key not in BULK_KEYS]

    defaults = document.get('defaults') or {}
    if not isinstance(defaults, dict):
        errors.append('"defaults" must be an object.')
        defaults = {}
    errors += [f'Unknown key in defaults: {str(key)[:40]}' for key in defaults if key not in DEFAULT_KEYS]
    defaults = {k: v for k, v in defaults.items() if k in DEFAULT_KEYS and v is not None}

    accounts_in = document.get('accounts') or []
    buckets_in = document.get('buckets') or []
    if not isinstance(accounts_in, list) or not isinstance(buckets_in, list):
        return plan, errors + ['"accounts" and "buckets" must be lists.']
    if not accounts_in and not buckets_in:
        return plan, errors + ['The file contains no accounts or buckets.']

    seen_accounts, seen_buckets, targets = set(), set(), []

    def add_target(account_id):
        if account_id not in targets:
            targets.append(account_id)

    for i, entry in enumerate(accounts_in):
        where = f'accounts[{i}]'
        if isinstance(entry, str):
            entry = {'id': entry}
        if not isinstance(entry, dict):
            errors.append(f'{where}: expected an object {{"id": ...}} or an identifier.')
            continue
        unknown = [str(k)[:40] for k in entry if k not in ACCOUNT_KEYS]
        if unknown:
            errors.append(f'{where}: unknown key {", ".join(unknown)}.')
            continue
        account_id = str(entry.get('id', '')).strip()
        if not ACCOUNT_RE.match(account_id):
            errors.append(f'{where}: invalid account identifier "{account_id[:64]}" (expected: sa-<tenant>-<suffix>).')
            continue
        if account_id in seen_accounts:
            errors.append(f'{where}: account {account_id} is duplicated.')
            continue
        meta, error = _valid_meta(entry.get('account_meta_name', defaults.get('account_meta_name')))
        if error:
            errors.append(f'{where} ({account_id}) : {error}')
            continue
        seen_accounts.add(account_id)
        plan['accounts'].append({'account_id': account_id, 'account_meta_name': meta})
        add_target(account_id)

    for i, entry in enumerate(buckets_in):
        where = f'buckets[{i}]'
        if not isinstance(entry, dict):
            errors.append(f'{where}: expected an object.')
            continue
        name = str(entry.get('name', '')).strip()
        where = f'{where} ({name[:64]})' if name else where
        unknown = [str(k)[:40] for k in entry if k not in BUCKET_KEYS]
        if unknown:
            errors.append(f'{where}: unknown key {", ".join(unknown)}.')
            continue
        merged = {**defaults, **{k: v for k, v in entry.items() if v is not None}}
        account_id = str(merged.get('account', '')).strip()
        if not ACCOUNT_RE.match(account_id):
            errors.append(f'{where}: invalid account "{account_id[:64]}" (expected: sa-<tenant>-<suffix>).')
            continue
        if not BUCKET_RE.match(name):
            errors.append(f'{where}: invalid bucket name (3 to 63 characters: lowercase letters, digits, - and .).')
            continue
        if name in seen_buckets:
            errors.append(f'{where} : bucket en double.')
            continue
        for key in ('quota_gb', 'storage_location', 'allowed_ips'):
            if key not in merged:
                errors.append(f'{where}: "{key}" is missing (neither in the bucket nor in defaults).')
        if any(key not in merged for key in ('quota_gb', 'storage_location', 'allowed_ips')):
            continue
        quota_gb, error = _parse_quota(merged['quota_gb'])
        location, error2 = _parse_location(merged['storage_location'])
        ips, error3 = _parse_ips(merged['allowed_ips'])
        meta, error4 = _valid_meta(merged.get('account_meta_name'))
        error = error or error2 or error3 or error4 or (None if ips else 'At least one allowed IP is required.')
        if error:
            errors.append(f'{where} : {error}')
            continue
        seen_buckets.add(name)
        plan['buckets'].append({
            'account_id': account_id, 'bucket_name': name, 'quota_gb': quota_gb,
            'storage_location': location, 'allowed_ips': ips, 'account_meta_name': meta,
        })
        add_target(account_id)

    plan['credentials'] = [{'account_id': account_id} for account_id in targets]
    return plan, errors


def parse_bulk_request(body):
    """Validate the JSON body of the bulk endpoints. Returns (plan, credentials_mode, errors, fatal)."""
    credentials = body.get('credentials', 'none')
    if credentials not in ('none', 'get', 'create'):
        return None, None, [], 'Invalid credentials option.'
    plan, errors = parse_bulk_document(body.get('document'))
    if credentials == 'none':
        plan['credentials'] = []
    return plan, credentials, errors, None


def handle_bulk_plan(config, body):
    plan, _, errors, fatal = parse_bulk_request(body)
    if fatal:
        return 400, {'error': fatal}
    plan['buckets'] = [
        {**{k: v for k, v in item.items() if k != 'allowed_ips'}, 'ip_count': len(item['allowed_ips'])}
        for item in plan['buckets']
    ]
    plan['errors'] = errors
    return 200, {'plan': plan}


def handle_bulk_expand(config, body):
    """Expand config.json (hive_management.py format) into a bulk file."""
    known = list(config.get('environments', []))
    envs = body.get('environments')
    if not isinstance(envs, list) or not envs or not all(isinstance(e, str) for e in envs):
        return 400, {'error': 'Choose at least one environment.'}
    unknown = [e for e in envs if e not in known]
    if unknown:
        return 400, {'error': f"Environnement inconnu : {', '.join(unknown)[:64]}"}
    accounts = hm.plan_storage_accounts(config, envs)
    buckets, warnings = hm.plan_buckets(config, envs)
    document = {
        'accounts': [
            {'id': a['account_id'], 'account_meta_name': a['account_meta_name']} for a in accounts
        ],
        'buckets': [
            {
                'account': b['account_id'], 'name': b['bucket_name'], 'quota_gb': b['quota_gb'],
                'storage_location': b['storage_location'], 'allowed_ips': b['allowed_ips'],
                'account_meta_name': b['account_meta_name'],
            }
            for b in buckets
        ],
    }
    return 200, {'document': document, 'warnings': warnings}


def handle_bulk_run(config, body):
    plan, credentials_mode, errors, fatal = parse_bulk_request(body)
    if fatal:
        return 400, {'error': fatal}
    if errors:
        return 400, {'error': f'The file contains {len(errors)} error(s): preview it and fix them.'}
    results, credentials = [], []

    def record(kind, target, ok, message):
        results.append({'kind': kind, 'target': target, 'ok': ok, 'message': message})

    def write_step(kind, target, thunk):
        ok, message = _write_outcome(thunk, 'created')
        record(kind, target, ok, message)

    def work():
        for item in plan['accounts']:
            write_step('Account', item['account_id'], lambda item=item: hm.create_storage_account(
                config, item['account_id'], item['account_meta_name']))
        for item in plan['buckets']:
            write_step('Bucket', item['bucket_name'], lambda item=item: hm.create_bucket(config, **item))
        if credentials_mode != 'none':
            create = credentials_mode == 'create'
            fetch = hm.request_credential_for if create else hm.fetch_credential_for
            for item in plan['credentials']:
                cred = fetch(config, item['account_id'])
                if cred:
                    credentials.append({
                        'account': item['account_id'],
                        'access_key_id': cred['access_key_id'],
                        'secret_key': cred['secret_key'],
                    })
                record('Credentials', item['account_id'], bool(cred),
                       ('generated' if create else 'fetched') if cred else 'failed (see the details)')

    _, log, aborted = call_api(work)
    if aborted:
        record('Aborted', '', False, MSG_NO_CREDS)
    return 200, {'results': results, 'credentials': credentials, 'log': log}


def replication_settings(config):
    """config.json's optional "cos2pag": {"config": "<path to cos2pag's config.yaml>", "env_file": "<path to its .env>"}.
    Returns {config, env_file} or None when replication to PAG is not configured."""
    section = config.get('cos2pag')
    if not isinstance(section, dict) or not isinstance(section.get('config'), str) or not section['config'].strip():
        return None
    env_file = section.get('env_file')
    return {'config': section['config'].strip(), 'env_file': env_file.strip() if isinstance(env_file, str) and env_file.strip() else None}


def _cos2pag():
    """The cos2pag pieces we use, or None if the package is not installed (it is optional)."""
    try:
        from cos2pag.config import ConfigError, load_config
        from cos2pag.http_client import ApiError
        from cos2pag.sync import sync_bucket
    except ImportError:
        return None
    return types.SimpleNamespace(ConfigError=ConfigError, ApiError=ApiError, load_config=load_config, sync_bucket=sync_bucket)


class _LogLines(logging.Handler):
    """Collects cos2pag's log records so the page can show them."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f'{record.levelname:7s} {record.getMessage()}')


class ReplicationInputError(Exception):
    """A request cos2pag should not even be given (shown to the user as is)."""


def _check_partition(cfg, tenant):
    """Refuse a tenant that is not an exact partition name when one exists that differs only by case.

    cos2pag looks the partition up by exact name, finds nothing, tries to create it, and PAG answers that it
    already exists. A tenant with no similar partition at all is fine: cos2pag creates it.
    """
    modules = pag_teardown.load_modules()
    if modules is None:
        return
    conflict = pag_teardown.partition_conflict(pag_teardown.partition_names(modules, cfg), tenant)
    if conflict:
        raise ReplicationInputError(conflict)


def _cos2pag_call(config, dry_run, work):
    """Run work(cfg, mods) -> (steps, message) with cos2pag's configuration loaded.

    Holds the API lock, loads cos2pag's .env and config.yaml, captures its log lines, and turns every failure
    (not configured, not installed, missing file, bad config, API error, anything else) into a message instead of
    a dropped request. Returns {ok, dry_run, message, steps, log}.
    """
    result = {'ok': False, 'dry_run': dry_run, 'message': '', 'steps': [], 'log': ''}
    settings = replication_settings(config)
    if settings is None:
        result['message'] = ('Replication not configured: add "cos2pag": {"config": "<path to cos2pag\'s config.yaml>", '
                             '"env_file": "<path to its .env>"} to config.json.')
        return result
    mods = _cos2pag()
    if mods is None:
        result['message'] = 'The cos2pag package is not installed: pip install <cos2pag folder> in the same Python environment.'
        return result

    handler, logger = _LogLines(), logging.getLogger('cos2pag')
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with _api_lock:
            try:
                if settings['env_file']:
                    load_dotenv(settings['env_file'], override=True)
                result['steps'], result['message'] = work(mods.load_config(settings['config']), mods)
            except ReplicationInputError as err:
                result['message'] = str(err)
            except FileNotFoundError as err:
                result['message'] = f"File not found: {err.filename or err}"
            except mods.ConfigError as err:
                result['message'] = f'cos2pag configuration: {err}'
            except mods.ApiError as err:
                result['message'] = f'API error: {err}'
            except KeyError as err:
                result['message'] = f'cos2pag configuration: missing key {err}'
            except LookupError as err:
                result['message'] = f'{err}'
            except Exception as err:  # e.g. boto3 / network errors from a step: report them instead of dropping the request
                result['message'] = f'{type(err).__name__}: {err}'
            else:
                result['ok'] = not any(step['status'] == 'FAILED' for step in result['steps'])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    result['log'] = '\n'.join(handler.lines)
    return result


JOB_WAIT_SECONDS = 60       # how long to wait for COS to let PDR's source credentials read the bucket
JOB_WAIT_STEP = 5


def job_wait_seconds(config):
    """config.json "cos2pag": {"job_wait_seconds": N}: how long to wait for COS to honour the new ACL grant before the
    first copy job is started (default 60, 0 to start at once)."""
    value = (config.get('cos2pag') or {}).get('job_wait_seconds')
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3600 else JOB_WAIT_SECONDS


def _is_access_denied(err):
    response = getattr(err, 'response', None)
    code = (response or {}).get('Error', {}).get('Code', '') if isinstance(response, dict) else ''
    status = (response or {}).get('ResponseMetadata', {}).get('HTTPStatusCode') if isinstance(response, dict) else None
    return code in ('AccessDenied', 'Forbidden', '403') or status == 403


def wait_for_source_access(s3, bucket, wait, sleep=time.sleep, step=JOB_WAIT_STEP):
    """List one object of `bucket` with the credentials PDR uses to read it, retrying while COS answers
    AccessDenied for up to `wait` seconds. A grant added to the ACL seconds earlier is not always honoured at once.

    Returns (ready, detail): ready is False only when access is still denied after the wait; any other failure
    (network, TLS...) is not an access problem, so it does not block the job (ready True, with the reason).
    """
    waited = 0
    while True:
        try:
            s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            return True, f'COS accepted reading the bucket after {waited} s' if waited else 'COS accepted reading the bucket'
        except Exception as err:   # botocore ClientError and anything else
            if not _is_access_denied(err):
                return True, f'access not verified ({type(err).__name__}: {err})'
            if waited >= wait:
                return False, (f"COS still answers AccessDenied when reading the bucket with PDR's source credentials after {waited} s. "
                               "It is not only a matter of delay: check that the access key of pdr.source_s3 belongs to the account "
                               "granted in the ACL (cos.backup_service_account.grantee) and that PDR's IP is in the bucket's whitelist. Once fixed, start a Copy job of the task from PDR.")
        sleep(step)
        waited += step


def _start_initial_job(config, cfg, bucket, steps):
    """Start the first Copy job of a task cos2pag has just created, once COS lets PDR read the bucket.

    cos2pag starts it itself right after patching the ACL, and PDR then failed with "Failed to enumerate objects:
    Forbidden (AccessDenied)" although the grant was in place. Adds a `pdr.job` step to `steps`.
    """
    from cos2pag.s3_lifecycle_client import build_s3_client
    mods = pag_teardown.load_modules()
    pdr_cfg = cfg['pdr']
    ready, detail = wait_for_source_access(build_s3_client(pdr_cfg['source_s3']), bucket, job_wait_seconds(config))
    if not ready:
        steps.append({'name': 'pdr.job', 'status': 'FAILED', 'detail': f"initial copy not started: {detail}"})
        return
    client = mods.PdrClient(pdr_cfg['base_url'], pag_teardown._session(mods, pdr_cfg), timeout=pdr_cfg.get('timeout', 30))
    task = client.find_task_by_alias(bucket)
    if task is None or task.get('id') is None:
        steps.append({'name': 'pdr.job', 'status': 'FAILED', 'detail': f"PDR task '{bucket}' not found: initial copy not started"})
        return
    client.start_job(task['id'])
    steps.append({'name': 'pdr.job', 'status': 'CHANGED', 'detail': f"initial copy started (task id={task['id']}); {detail}"})


def run_replication(config, bucket, tenant, dry_run, noncurrent_days=None):
    """Set up (or, with dry_run, preview) the replication of a bucket to PAG with cos2pag's sync_bucket.

    cos2pag does the whole job (COS ACL / firewall / notifications, PAG partition and repository, PDR task) and is
    idempotent; it keeps its own config file and secrets, referenced from config.json. `noncurrent_days` overrides
    the retention of noncurrent object versions (cos2pag's --noncurrent-expiration-days) for this run; applied to
    an existing replication it updates the lifecycle rule on PAG. Returns {ok, dry_run, message, steps: [{name,
    status, detail}], log}.
    """
    def work(cfg, mods):
        _check_partition(cfg, tenant)
        extra = {} if noncurrent_days is None else {'noncurrent_expiration_days': noncurrent_days}
        # cos2pag would start the first copy job at once; we start it ourselves after checking that COS lets PDR read.
        start_job = (cfg.get('pdr') or {}).get('auto_start_job', True) and not dry_run
        if start_job:
            cfg = {**cfg, 'pdr': {**cfg['pdr'], 'auto_start_job': False}}
        report = mods.sync_bucket(cfg, bucket, tenant, dry_run=dry_run, **extra)
        steps = [
            {'name': step.name, 'status': 'SKIPPED' if step.skipped else 'CHANGED' if step.changed else 'OK', 'detail': step.detail}
            for step in report.steps
        ]
        if start_job and any(st['name'] == 'pdr.task' and st['detail'].startswith('created') for st in steps):
            _start_initial_job(config, cfg, bucket, steps)
        message = (f'Simulation finished for {bucket} (no change sent).' if dry_run
                   else f'Replication of {bucket} to the PAG partition {tenant} set up.')
        if any(st['status'] == 'FAILED' for st in steps):
            message = f'Replication of {bucket} set up, but the initial copy did not start (see the detail).'
        return steps, message

    return _cos2pag_call(config, dry_run, work)


TEARDOWN_MESSAGES = {
    'suspend': ('suspended', 'Suspension of the replication of {bucket}'),
    'undo': ('undone (PAG repository kept)', 'Replication of {bucket} undone, PAG repository kept'),
    'purge': ('undone, PAG repository deleted', 'Replication of {bucket} undone, PAG repository deleted'),
}


def run_teardown(config, bucket, tenant, level, dry_run):
    """Suspend or undo the replication of a bucket to PAG (see pag_teardown). Returns the same shape as run_replication."""
    def work(cfg, mods):
        modules = pag_teardown.load_modules()
        if modules is None:
            raise mods.ConfigError('the cos2pag clients cannot be imported')
        steps = pag_teardown.teardown(modules, cfg, bucket, tenant, level, dry_run)
        done, label = TEARDOWN_MESSAGES[level]
        failed = [step['name'] for step in steps if step['status'] == 'FAILED']
        if failed:
            message = f"{label.format(bucket=bucket)} interrupted: step {failed[0]} failed."
        elif dry_run:
            message = f'Simulation finished for {bucket} (no change sent).'
        else:
            message = f'Replication of {bucket} {done}.'
        return steps, message

    return _cos2pag_call(config, dry_run, work)


def _cos2pag_clients():
    """The cos2pag client pieces needed to read a replication's state, or None if not installed."""
    try:
        from cos2pag.config import AuthConfig
        from cos2pag.http_client import build_session
        from cos2pag.pdr_client import PdrClient
        from cos2pag.s3_lifecycle_client import build_s3_client, get_bucket_lifecycle
    except ImportError:
        return None
    return types.SimpleNamespace(AuthConfig=AuthConfig, build_session=build_session, PdrClient=PdrClient,
                                 build_s3_client=build_s3_client, get_bucket_lifecycle=get_bucket_lifecycle)


DAY_BITS = [('sunday', 1), ('monday', 2), ('tuesday', 4), ('wednesday', 8), ('thursday', 16), ('friday', 32), ('saturday', 64)]


def noncurrent_days_from_rules(rules):
    """Retention of noncurrent versions (days) from a bucket's S3 lifecycle rules, or None."""
    found = None
    for rule in rules or []:
        days = (rule.get('NoncurrentVersionExpiration') or {}).get('NoncurrentDays')
        if isinstance(days, int) and not isinstance(days, bool):
            if rule.get('ID') == 'expire-noncurrent-versions':
                return days
            found = days if found is None else found
    return found


def replication_status(config, bucket):
    """What exists today for a bucket's replication: the PDR task (alias = bucket name) and the PAG lifecycle rules.

    Read-only: a GET on PDR's task list and a GET of the bucket's lifecycle on PAG's S3 endpoint.
    Returns {ok, message, pdr: {found, id, schedule}, lifecycle: {noncurrent_days, rules}, log}.
    """
    result = {'ok': False, 'message': '', 'pdr': None, 'lifecycle': None, 'log': ''}
    settings = replication_settings(config)
    if settings is None:
        result['message'] = 'Replication not configured.'
        return result
    mods, clients = _cos2pag(), _cos2pag_clients()
    if mods is None or clients is None:
        result['message'] = 'The cos2pag package is not installed.'
        return result
    with _api_lock:
        try:
            if settings['env_file']:
                load_dotenv(settings['env_file'], override=True)
            cfg = mods.load_config(settings['config'])
            pdr_cfg = cfg['pdr']
            session = clients.build_session(clients.AuthConfig.from_dict(pdr_cfg.get('auth', {})), verify_ssl=pdr_cfg.get('verify_ssl', True))
            task = clients.PdrClient(pdr_cfg['base_url'], session, timeout=pdr_cfg.get('timeout', 30)).find_task_by_alias(bucket)
            schedule = (task or {}).get('schedule') or {}
            mask = schedule.get('dowMask') if isinstance(schedule.get('dowMask'), int) else 0
            result['pdr'] = {
                'found': task is not None, 'id': (task or {}).get('id'),
                'schedule': {'enabled': bool(schedule.get('enabled')), 'hour': schedule.get('hour'),
                             'days': [name for name, bit in DAY_BITS if mask & bit]} if schedule else None,
            }
            try:
                rules = clients.get_bucket_lifecycle(clients.build_s3_client(pdr_cfg['target_s3']), bucket)
            except Exception:  # no repository on PAG yet (or PAG's S3 not reachable): nothing to read
                rules = None
            result['lifecycle'] = {'noncurrent_days': noncurrent_days_from_rules(rules), 'rules': [r.get('ID') for r in rules or []],
                                   'readable': rules is not None}
            result['ok'] = True
        except FileNotFoundError as err:
            result['message'] = f"File not found: {err.filename or err}"
        except mods.ConfigError as err:
            result['message'] = f'cos2pag configuration: {err}'
        except mods.ApiError as err:
            result['message'] = f'API error: {err}'
        except KeyError as err:
            result['message'] = f'cos2pag configuration: missing key {err}'
        except Exception as err:
            result['message'] = f'{type(err).__name__}: {err}'
    return result


def replication_partitions(config, tenant):
    """The PAG partitions (one per tenant) and how `tenant`, if given, matches them. Read-only."""
    held = {}

    def work(cfg, mods):
        modules = pag_teardown.load_modules()
        if modules is None:
            raise mods.ConfigError('the cos2pag clients cannot be imported')
        held['names'] = sorted(pag_teardown.partition_names(modules, cfg), key=str.casefold)
        return [], ''

    result = _cos2pag_call(config, True, work)
    result['names'] = held.get('names', [])
    result['match'] = pag_teardown.resolve_partition(result['names'], tenant) if tenant and result['ok'] else None
    return result


def handle_replication_partitions(config, body):
    tenant = _text(body, 'tenant')
    if tenant and not LABEL_RE.match(tenant):
        return 400, {'error': 'Invalid PAG tenant (letters, digits, . - and _ only).'}
    return 200, replication_partitions(config, tenant)


def handle_replication_status(config, body):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    return 200, replication_status(config, bucket)


def handle_teardown(config, body, dry_run):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    level = _text(body, 'level')
    if level not in pag_teardown.LEVELS:
        return 400, {'error': 'Invalid level (suspend, undo or purge).'}
    tenant = _text(body, 'tenant')
    if (level == 'purge' or tenant) and not LABEL_RE.match(tenant):
        return 400, {'error': 'Invalid PAG tenant (letters, digits, . - and _ only); it is required to delete the repository.'}
    if not dry_run and level != 'suspend' and _text(body, 'confirm') != bucket:
        return 400, {'error': 'Wrong confirmation: type the exact bucket name.'}
    return 200, run_teardown(config, bucket, tenant, level, dry_run)


def handle_replication(config, body, dry_run):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    tenant = _text(body, 'tenant')
    if not LABEL_RE.match(tenant):
        return 400, {'error': 'Invalid PAG tenant (letters, digits, . - and _ only).'}
    days = None
    if _text(body, 'noncurrent_days'):
        try:
            days = int(_text(body, 'noncurrent_days'))
        except ValueError:
            days = 0
        if not 1 <= days <= MAX_RETENTION_DAYS:
            return 400, {'error': f'Invalid retention (whole number of days, between 1 and {MAX_RETENTION_DAYS}).'}
    return 200, run_replication(config, bucket, tenant, dry_run, days)


POST_ROUTES = {
    '/api/replication/partitions': handle_replication_partitions,
    '/api/replication/teardown/preview': lambda config, body: handle_teardown(config, body, dry_run=True),
    '/api/replication/teardown/run': lambda config, body: handle_teardown(config, body, dry_run=False),
    '/api/replication/status': handle_replication_status,
    '/api/replication/preview': lambda config, body: handle_replication(config, body, dry_run=True),
    '/api/replication/run': lambda config, body: handle_replication(config, body, dry_run=False),
    '/api/bucket/create': handle_create_bucket,
    '/api/bucket/get': handle_get_bucket,
    '/api/bucket/update': handle_update_bucket,
    '/api/bucket/delete': handle_delete_bucket,
    '/api/account/create': handle_create_account,
    '/api/creds/get': lambda config, body: handle_credentials(config, body, create=False),
    '/api/creds/create': lambda config, body: handle_credentials(config, body, create=True),
    '/api/bulk/expand': handle_bulk_expand,
    '/api/bulk/plan': handle_bulk_plan,
    '/api/bulk/run': handle_bulk_run,
}


class Handler(BaseHTTPRequestHandler):
    config = None
    locations = ()
    allowed_hosts = frozenset()

    def _send(self, status, body, content_type):
        payload = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj), 'application/json; charset=utf-8')

    def _host_ok(self):
        # Refuse foreign Host headers so a web page can't reach this server via DNS rebinding.
        host = self.headers.get('Host', '')
        if host_allowed(host, self.allowed_hosts):
            return True
        shown = host[:100]
        print(f'Refused a request whose Host header is {shown!r}. If you reach this page under that name '
              f'(machine name, tunnel, proxy), restart with: --allow-host {shown}', file=sys.stderr)
        self._json(403, {'error': f'forbidden host: {shown!r}. Open http://127.0.0.1:<port>/ or http://localhost:<port>/ (any port), '
                                  f'or restart cos_ui.py with --allow-host {shown} to accept that name.'})
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if url.path == '/':
            self._send(200, PAGE, 'text/html; charset=utf-8')
        elif url.path == '/api/info':
            by_tenant = tenant_locations(self.config)
            legacy = self.config.get('storage_locations')
            known = {v for v in legacy.values() if isinstance(v, str)} if isinstance(legacy, dict) else set()
            known.update(name for names in by_tenant.values() for name in names)
            known.update(self.locations)
            self._json(200, {
                'environments': list(self.config.get('environments', [])),
                'storage_locations': sorted(known),
                'tenants': by_tenant,
                'labels': location_labels(self.config),
                'replication': {'configured': replication_settings(self.config) is not None},
            })
        elif url.path == '/api/tenants':
            self._json(200, list_tenants(self.config, self.locations))
        elif url.path == '/api/search':
            query = parse_qs(url.query)
            kind = (query.get('kind') or [''])[0]
            text = (query.get('q') or [''])[0].strip()
            if kind not in SEARCH_KINDS:
                self._json(400, {'error': 'Unknown search type.'})
            elif not PATTERN_RE.match(text) or not text.strip('*,') or (',' in text and kind != 'tenant'):
                self._json(400, {'error': 'Invalid name (letters, digits, . - _ and * only; a comma separates several tenants).'})
            else:
                self._json(200, search(self.config, kind, text))
        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self):
        if not self._host_ok():
            return
        # Writes must come from this page: same-origin, and JSON (which a
        # cross-site form can't send without a CORS preflight we never allow).
        origin = self.headers.get('Origin')
        if origin and not host_allowed(urlparse(origin).netloc, self.allowed_hosts):
            self._json(403, {'error': 'forbidden origin'})
            return
        route = POST_ROUTES.get(urlparse(self.path).path)
        if route is None:
            self._json(404, {'error': 'not found'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            self._json(415, {'error': 'application/json expected'})
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._json(400, {'error': 'Invalid request body.'})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {'error': 'Invalid JSON.'})
            return
        if not isinstance(body, dict):
            self._json(400, {'error': 'Invalid request.'})
            return
        try:
            status, obj = route(self.config, body)
        except KeyError as err:
            status, obj = 400, {'error': f'config.json incomplete: missing key {err}.'}
        self._json(status, obj)

    def log_message(self, fmt, *args):
        pass  # keep the terminal for the API messages


def main():
    parser = argparse.ArgumentParser(description='Local web UI to manage IBM COS accounts, buckets and credentials')
    parser.add_argument('--config', default='config.json', help='Path to config.json')
    parser.add_argument('--env-file', default='.env', help='Path to a .env file with the credentials')
    parser.add_argument('--locations', default='storage_locations.txt',
                        help='Optional local list of storage locations (container vaults), one per line; '
                             'a pasted table works, only the first word starting with cv- is kept')
    parser.add_argument('--port', type=int, default=8765, help='Local port (default: 8765)')
    parser.add_argument('--allow-host', action='append', default=[], metavar='HOST',
                        help='Also accept this Host header (machine name, tunnel or proxy name); repeatable. '
                             'The server still only listens on 127.0.0.1.')
    parser.add_argument('--no-browser', action='store_true', help="Don't open the browser automatically")
    args = parser.parse_args()

    load_dotenv(args.env_file, override=True)
    Handler.config = hm.load_config(args.config)
    Handler.locations = load_locations(args.locations)
    if Handler.locations:
        print(f'{len(Handler.locations)} storage location(s) read from {args.locations}')
    Handler.allowed_hosts = build_allowed_hosts(args.port, args.allow_host)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = f'http://127.0.0.1:{args.port}/'
    print(f'COS management UI on {url} (Ctrl+C to stop)')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
