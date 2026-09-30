#!/usr/bin/env python3
"""
Local web UI for the Hive / IBM COS management tasks of hive_management.py.

Usage:
    python hive_ui.py                 # http://127.0.0.1:8765, opens the browser
    python hive_ui.py --port 9000 --no-browser

Tabs:
  Recherche   list the buckets of every account starting with sa-<tenant>-,
              fetch or generate an account's credentials
  Créer       create one storage account, or one bucket, from typed-in values
  En masse    what create_sa / create_buckets / create_creds / get_creds do
              from config.json, with a preview and a per-item result

Same setup as hive_management.py: config.json next to the script, credentials
from .env / environment / secrets.json (see load_auth_header()). The server
only listens on 127.0.0.1. Credentials (access/secret keys) are shown in the
page for copying and are never written to disk by the UI.

Listing the buckets of an account is flagged as resource-intensive by the API
guide, so a search only runs on demand, one account at a time, and the API is
never called by more than one request at once.
"""

import argparse
import contextlib
import io
import ipaddress
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

import hive_management as hm

TENANT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
ACCOUNT_RE = re.compile(r'^sa-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
LABEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
MAX_QUOTA_GB = 1_000_000
MAX_BODY = 64 * 1024
MSG_NO_CREDS = 'Identifiants introuvables (voir les détails).'
_api_lock = threading.Lock()

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hive management</title>
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
  button.small { padding:2px 8px; font-size:13px; }
  button:disabled { opacity:.6; cursor:not-allowed; }
  [hidden] { display:none !important; }

  #tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:16px; }
  #tabs button { background:none; color:var(--muted); border:0; border-bottom:2px solid transparent; border-radius:0; padding:8px 14px; }
  #tabs button.active { color:var(--fg); border-bottom-color:var(--accent); font-weight:600; }

  #search { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  #search input { min-width:200px; }
  #status { margin:12px 0; color:var(--muted); min-height:1.5em; }
  #status.error { color:var(--err); }
  .toolbar { display:none; gap:8px; margin:8px 0 16px; flex-wrap:wrap; }

  .account { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 14px; margin-bottom:10px; }
  .account h2 { font:600 14px ui-monospace, monospace; margin:0 0 6px; display:flex; justify-content:space-between; gap:8px; }
  .account h2 span { color:var(--muted); font-weight:400; }
  .account ul { margin:0; padding-left:18px; font-family: ui-monospace, monospace; font-size:13px; }
  .account .actions { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
  .empty { color:var(--muted); font-size:13px; }

  .grid-form { display:grid; grid-template-columns: 200px 1fr; gap:8px 12px; align-items:start; }
  .grid-form label { padding-top:7px; }
  .grid-form small { color:var(--muted); grid-column:2; margin-top:-4px; }
  .grid-form textarea, .grid-form select, .grid-form input[type=text] { width:100%; }
  .grid-form button { grid-column:2; justify-self:start; }
  @media (max-width: 560px) { .grid-form { grid-template-columns:1fr; } .grid-form small, .grid-form button { grid-column:1; } }
  fieldset { border:1px solid var(--line); border-radius:8px; margin:0 0 12px; padding:8px 12px; }
  fieldset label { margin-right:16px; display:inline-block; }

  table { border-collapse:collapse; width:100%; font-size:13px; margin:6px 0 12px; }
  th, td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  td.ok { color:var(--ok); } td.fail { color:var(--err); }

  #creds { background:var(--card); border:1px solid var(--accent); border-radius:8px; padding:10px 14px; margin:12px 0; }
  #creds .cred { margin:8px 0 12px; }
  #creds .row { display:flex; gap:8px; align-items:center; margin:2px 0; flex-wrap:wrap; }
  #creds .row .k { width:110px; color:var(--muted); }
  #creds .row .v { font-family:ui-monospace, monospace; word-break:break-all; }

  details { margin-top:16px; color:var(--muted); font-size:13px; }
  pre { white-space:pre-wrap; background:var(--card); border:1px solid var(--line); border-radius:6px; padding:8px; }
</style>
</head>
<body>
<main>
  <h1>Hive management</h1>
  <nav id="tabs">
    <button type="button" class="active" data-tab="search">Recherche</button>
    <button type="button" data-tab="create">Créer</button>
    <button type="button" data-tab="bulk">En masse (config.json)</button>
  </nav>
  <div id="status"></div>
  <div id="creds" hidden></div>

  <section id="tab-search">
    <form id="search">
      <span class="prefix">sa-</span>
      <input type="text" id="tenant" placeholder="tenant" autocomplete="off" autofocus required
             pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" maxlength="64">
      <span class="prefix">-</span>
      <button type="submit" id="go">Rechercher</button>
    </form>
    <div class="toolbar" id="toolbar">
      <input type="text" id="filter" placeholder="Filtrer les buckets..." autocomplete="off">
      <button type="button" class="secondary" id="copy">Copier les buckets affichés</button>
    </div>
    <div id="results"></div>
  </section>

  <section id="tab-create" hidden>
    <h3>Créer un storage account</h3>
    <form id="account-form" class="grid-form" autocomplete="off">
      <label for="a-id">Compte</label>
      <input type="text" id="a-id" required maxlength="132" placeholder="ex. sa-fina-0001">
      <small>sa-&lt;tenant&gt;-&lt;suffixe&gt;</small>
      <label for="a-meta">x-Account-Meta-name</label>
      <input type="text" id="a-meta" placeholder="facultatif">
      <button type="submit" id="a-go">Créer le compte</button>
    </form>

    <h3>Créer un bucket</h3>
    <form id="bucket-form" class="grid-form" autocomplete="off">
      <label for="c-account">Compte</label>
      <input type="text" id="c-account" required list="accounts-list" maxlength="132" placeholder="ex. sa-fina-0001">
      <datalist id="accounts-list"></datalist>
      <label for="c-bucket">Nom du bucket</label>
      <input type="text" id="c-bucket" required maxlength="63" placeholder="ex. ctie-hive-dev-fina">
      <small>3 à 63 caractères : minuscules, chiffres, - et .</small>
      <label for="c-quota">Quota (Go)</label>
      <input type="text" id="c-quota" required inputmode="numeric" placeholder="ex. 60">
      <label for="c-location">Storage location</label>
      <input type="text" id="c-location" required placeholder="ex. cv-ctie-hive-dev-01">
      <label for="c-ips">IP autorisées</label>
      <textarea id="c-ips" rows="4" required placeholder="Une IP (ou CIDR) par ligne, ou séparées par des virgules"></textarea>
      <label for="c-meta">x-Account-Meta-name</label>
      <input type="text" id="c-meta" placeholder="ex. ctie-hive-dev (facultatif)">
      <button type="submit" id="c-go">Créer le bucket</button>
    </form>
  </section>

  <section id="tab-bulk" hidden>
    <p class="empty">Reprend ce que font <code>create_sa</code>, <code>create_buckets</code>, <code>create_creds</code> et <code>get_creds</code>
    en ligne de commande, d'après <code>config.json</code>. Les credentials s'affichent dans la page et ne sont pas écrites sur disque.</p>
    <fieldset><legend>Environnements</legend><div id="b-envs"></div></fieldset>
    <fieldset><legend>Étapes</legend>
      <label><input type="checkbox" id="b-accounts" checked> Storage accounts</label>
      <label><input type="checkbox" id="b-buckets" checked> Buckets</label>
      <div>
        Credentials :
        <label><input type="radio" name="b-creds" value="none" checked> aucune</label>
        <label><input type="radio" name="b-creds" value="get"> récupérer l'existante</label>
        <label><input type="radio" name="b-creds" value="create"> générer une nouvelle</label>
      </div>
    </fieldset>
    <button type="button" id="b-preview">Prévisualiser</button>
    <button type="button" id="b-run" disabled>Exécuter le plan</button>
    <div id="b-plan"></div>
    <div id="b-result"></div>
  </section>

  <details id="logbox" hidden><summary>Détails de l'appel API</summary><pre id="log"></pre></details>
</main>
<script>
  const $ = id => document.getElementById(id);
  let data = {};
  let lastTenant = '';

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
      try { out = await res.json(); } catch (e) { out = {error: 'Réponse invalide (HTTP ' + res.status + ')'}; }
    } catch (err) {
      out = {error: 'Erreur : ' + err};
    }
    return out;
  }

  // ---- tabs ---------------------------------------------------------------
  document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('active', x === b));
    for (const name of ['search', 'create', 'bulk']) $('tab-' + name).hidden = name !== b.dataset.tab;
  }));

  // ---- credentials panel ----------------------------------------------------
  function copyText(text, label) {
    navigator.clipboard.writeText(text).then(
      () => setStatus(label + ' copié.', false),
      () => setStatus('Copie impossible (presse-papiers refusé).', true));
  }

  function showCreds(list) {
    const box = $('creds');
    box.textContent = '';
    box.append(el('strong', 'Credentials (affichées ici seulement, rien n\'est écrit sur disque)'));
    for (const c of list) {
      const wrap = el('div', undefined, 'cred');
      wrap.append(el('div', c.account + (c.env ? ' (' + c.env + ')' : ''), 'mono'));

      const r1 = el('div', undefined, 'row');
      r1.append(el('span', 'Access key ID', 'k'), el('span', c.access_key_id, 'v'));
      const c1 = el('button', 'Copier', 'small secondary');
      c1.addEventListener('click', () => copyText(c.access_key_id, 'Access key ID'));
      r1.append(c1);

      const r2 = el('div', undefined, 'row');
      const secret = el('span', '••••••••••••', 'v');
      let shown = false;
      const reveal = el('button', 'Afficher', 'small secondary');
      reveal.addEventListener('click', () => {
        shown = !shown;
        secret.textContent = shown ? c.secret_key : '••••••••••••';
        reveal.textContent = shown ? 'Masquer' : 'Afficher';
      });
      const c2 = el('button', 'Copier', 'small secondary');
      c2.addEventListener('click', () => copyText(c.secret_key, 'Secret key'));
      r2.append(el('span', 'Secret key', 'k'), secret, reveal, c2);

      wrap.append(r1, r2);
      box.append(wrap);
    }
    const all = el('button', 'Copier en JSON', 'small secondary');
    all.addEventListener('click', () => copyText(JSON.stringify({credentials: list}, null, 2), 'Credentials (JSON)'));
    const close = el('button', 'Effacer', 'small secondary');
    close.addEventListener('click', () => { box.textContent = ''; box.hidden = true; });
    const bar = el('div');
    bar.style.display = 'flex'; bar.style.gap = '6px';
    bar.append(all, close);
    box.append(bar);
    box.hidden = false;
    box.scrollIntoView({block: 'nearest'});
  }

  // ---- search ---------------------------------------------------------------
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
        const ul = el('ul');
        for (const b of list) ul.append(el('li', b));
        card.append(ul);
      } else {
        card.append(el('div', 'Aucun bucket', 'empty'));
      }
      const actions = el('div', undefined, 'actions');
      for (const [mode, label] of [['get', 'Récupérer les credentials'], ['create', 'Générer de nouvelles credentials']]) {
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

  $('results').addEventListener('click', async e => {
    const b = e.target.closest('button[data-cred]');
    if (!b) return;
    const account = b.dataset.account, mode = b.dataset.cred;
    if (mode === 'create' && !confirm('Générer une nouvelle paire de credentials pour ' + account + ' ?')) return;
    b.disabled = true;
    setStatus(mode === 'get' ? 'Récupération des credentials...' : 'Génération des credentials...', false);
    const out = await post('/api/creds/' + mode, {account});
    b.disabled = false;
    showLog(out.log);
    if (out.error || !out.ok) { setStatus(out.error || out.message, true); return; }
    setStatus(out.message, false);
    showCreds(out.credentials);
  });

  $('copy').addEventListener('click', () => {
    const q = $('filter').value.trim().toLowerCase();
    const names = Object.values(data).flat().filter(b => b.toLowerCase().includes(q));
    copyText(names.join('\n'), names.length + ' bucket(s)');
  });

  function fillAccounts() {
    const list = $('accounts-list');
    list.textContent = '';
    for (const account of Object.keys(data)) { const o = el('option'); o.value = account; list.append(o); }
  }

  async function runSearch(tenant, keepMessage) {
    const go = $('go');
    lastTenant = tenant;
    go.disabled = true;
    if (!keepMessage) setStatus('Recherche en cours (un appel API par compte, ça peut prendre un moment)...', false);
    $('results').textContent = '';
    $('toolbar').style.display = 'none';
    try {
      const res = await fetch('/api/search?tenant=' + encodeURIComponent(tenant));
      const out = await res.json();
      showLog(out.log);
      if (!res.ok || out.error) { setStatus(out.error || ('Erreur HTTP ' + res.status), true); return; }
      data = out.accounts;
      const n = Object.keys(data).length;
      const total = Object.values(data).reduce((s, b) => s + b.length, 0);
      if (!keepMessage || out.has_error) {
        setStatus(n
          ? n + ' compte(s) "' + out.prefix + '*", ' + total + ' bucket(s).' + (out.has_error ? ' Des erreurs ont eu lieu, voir les détails.' : '')
          : 'Aucun compte "' + out.prefix + '*" trouvé.' + (out.has_error ? ' Voir les détails ci-dessous.' : ''),
          out.has_error);
      }
      $('filter').value = '';
      $('toolbar').style.display = n ? 'flex' : 'none';
      fillAccounts();
      if (!$('a-id').value.trim() || $('a-id').dataset.prefilled) {
        $('a-id').value = out.prefix; $('a-id').dataset.prefilled = '1';
      }
      render();
    } catch (err) {
      setStatus('Erreur : ' + err, true);
    } finally {
      go.disabled = false;
    }
  }

  $('search').addEventListener('submit', e => { e.preventDefault(); runSearch($('tenant').value.trim(), false); });
  $('a-id').addEventListener('input', () => { delete $('a-id').dataset.prefilled; });

  // ---- create one account / one bucket ---------------------------------------
  async function afterCreate(account) {
    if (lastTenant && account.startsWith('sa-' + lastTenant + '-')) await runSearch(lastTenant, true);
  }

  $('account-form').addEventListener('submit', async e => {
    e.preventDefault();
    const body = {account: $('a-id').value.trim(), account_meta_name: $('a-meta').value.trim()};
    if (!confirm('Créer le storage account "' + body.account + '"\n  x-Account-Meta-name : ' + (body.account_meta_name || '(aucun)'))) return;
    const btn = $('a-go');
    btn.disabled = true;
    setStatus('Création du compte en cours...', false);
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
    const summary = 'Créer le bucket "' + body.bucket + '"\n' +
      '  compte : ' + body.account + '\n' +
      '  quota : ' + body.quota_gb + ' Go\n' +
      '  storage location : ' + body.storage_location + '\n' +
      '  IP autorisées : ' + ips + '\n' +
      '  x-Account-Meta-name : ' + (body.account_meta_name || '(aucun)');
    if (!confirm(summary)) return;
    const btn = $('c-go');
    btn.disabled = true;
    setStatus('Création du bucket en cours...', false);
    const out = await post('/api/bucket/create', body);
    btn.disabled = false;
    if (out.log) showLog(out.log);
    setStatus(out.error || out.message, !!out.error || !out.ok);
    if (out.ok) {
      $('c-bucket').value = '';
      await afterCreate(body.account);
    }
  });

  // ---- bulk (config.json) ------------------------------------------------------
  let plannedOptions = null;

  function bulkOptions() {
    return {
      environments: [...document.querySelectorAll('#b-envs input:checked')].map(i => i.value),
      accounts: $('b-accounts').checked,
      buckets: $('b-buckets').checked,
      credentials: document.querySelector('input[name=b-creds]:checked').value,
    };
  }

  function invalidatePlan() {
    plannedOptions = null;
    $('b-run').disabled = true;
    $('b-plan').textContent = '';
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

  async function loadInfo() {
    try {
      const info = await (await fetch('/api/info')).json();
      const box = $('b-envs');
      box.textContent = '';
      for (const env of info.environments || []) {
        const label = el('label');
        const cb = el('input');
        cb.type = 'checkbox'; cb.value = env; cb.checked = env !== 'prd';
        cb.addEventListener('change', invalidatePlan);
        label.append(cb, ' ' + env);
        box.append(label);
      }
    } catch (err) { setStatus('Impossible de lire la configuration : ' + err, true); }
  }

  for (const id of ['b-accounts', 'b-buckets']) $(id).addEventListener('change', invalidatePlan);
  document.querySelectorAll('input[name=b-creds]').forEach(r => r.addEventListener('change', invalidatePlan));

  $('b-preview').addEventListener('click', async () => {
    invalidatePlan();
    $('b-result').textContent = '';
    const opts = bulkOptions();
    setStatus('Calcul du plan...', false);
    const out = await post('/api/bulk/plan', opts);
    if (out.error) { setStatus(out.error, true); return; }
    const plan = out.plan, box = $('b-plan');
    const total = plan.accounts.length + plan.buckets.length + plan.credentials.length;
    setStatus(total + ' opération(s) prévue(s).' + (plan.errors.length ? ' ' + plan.errors.length + ' avertissement(s).' : ''), plan.errors.length > 0);
    if (plan.accounts.length) {
      box.append(el('h3', 'Storage accounts (' + plan.accounts.length + ')'));
      box.append(table(['Env', 'Compte', 'x-Account-Meta-name'], plan.accounts.map(a => [a.env, a.account_id, a.account_meta_name])));
    }
    if (plan.buckets.length) {
      box.append(el('h3', 'Buckets (' + plan.buckets.length + ')'));
      box.append(table(['Env', 'Compte', 'Bucket', 'Quota (Go)', 'Storage location', 'IP'],
        plan.buckets.map(b => [b.env, b.account_id, b.bucket_name, String(b.quota_gb), b.storage_location, String(b.ip_count)])));
    }
    if (plan.credentials.length) {
      box.append(el('h3', (opts.credentials === 'create' ? 'Nouvelles credentials' : 'Credentials existantes') + ' (' + plan.credentials.length + ')'));
      box.append(table(['Env', 'Compte'], plan.credentials.map(c => [c.env, c.account_id])));
    }
    for (const w of plan.errors) box.append(el('div', w, 'empty'));
    if (total) { plannedOptions = JSON.stringify(opts); $('b-run').disabled = false; }
  });

  $('b-run').addEventListener('click', async () => {
    const opts = bulkOptions();
    if (JSON.stringify(opts) !== plannedOptions) { invalidatePlan(); setStatus('Les options ont changé : prévisualise de nouveau.', true); return; }
    const parts = [];
    if (opts.accounts) parts.push('storage accounts');
    if (opts.buckets) parts.push('buckets');
    if (opts.credentials === 'create') parts.push('génération de nouvelles credentials');
    if (opts.credentials === 'get') parts.push('récupération des credentials');
    if (!confirm('Exécuter : ' + parts.join(', ') + '\nEnvironnements : ' + opts.environments.join(', '))) return;
    if (opts.environments.includes('prd') && prompt('Production sélectionnée. Tape prd pour confirmer.') !== 'prd') {
      setStatus('Exécution annulée.', false);
      return;
    }
    $('b-run').disabled = true; $('b-preview').disabled = true;
    setStatus('Exécution en cours (un appel API par opération)...', false);
    const out = await post('/api/bulk/run', opts);
    $('b-preview').disabled = false;
    if (out.log) showLog(out.log);
    if (out.error) { setStatus(out.error, true); return; }
    const failed = out.results.filter(r => !r.ok).length;
    setStatus(out.results.length + ' opération(s), ' + failed + ' échec(s).', failed > 0);
    const box = $('b-result');
    box.textContent = '';
    box.append(el('h3', 'Résultat'));
    box.append(table(['Étape', 'Cible', 'Résultat'],
      out.results.map(r => [r.kind, r.target, {text: r.message, cls: r.ok ? 'ok' : 'fail'}])));
    if (out.credentials.length) showCreds(out.credentials);
    invalidatePlan();
  });

  loadInfo();
</script>
</body>
</html>
"""


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


def search_tenant(config, tenant):
    """Return {prefix, accounts: {id: [buckets]}, log, has_error} for sa-<tenant>-*."""
    prefix = f'sa-{tenant}-'

    def work():
        return {
            account['id']: hm.list_bucket_names(config, account['id'])
            for account in hm.list_accounts(config, prefix=prefix)
        }

    accounts, log, aborted = call_api(work)
    has_error = aborted or any(
        line.startswith(('Error', 'Network error')) for line in log.splitlines()
    )
    return {
        'prefix': prefix,
        'accounts': dict(sorted((accounts or {}).items())),
        'log': log,
        'has_error': has_error,
    }


def _write_outcome(thunk, ok_message):
    """Run a (status, text) write call; returns (ok, message)."""
    try:
        status, text = thunk()
    except requests.RequestException as err:
        return False, f'Erreur réseau : {err}'
    if status == 201:
        return True, ok_message
    return False, f'Échec : HTTP {status} - {text[:500]}'


def _run_write(thunk, ok_message):
    outcome, log, aborted = call_api(lambda: _write_outcome(thunk, ok_message))
    ok, message = (False, MSG_NO_CREDS) if aborted else outcome
    return {'ok': ok, 'message': message, 'log': log}


def _text(body, key):
    return str(body.get(key, '')).strip()


def _valid_account(body):
    account = _text(body, 'account')
    return account if ACCOUNT_RE.match(account) else None


def _valid_meta(body):
    """Optional x-Account-Meta-name. Returns (value_or_None, error)."""
    meta = _text(body, 'account_meta_name')
    if meta and not LABEL_RE.match(meta):
        return None, "Valeur invalide pour l'en-tête x-Account-Meta-name."
    return meta or None, None


def parse_create_bucket(body):
    """Validate the JSON body of POST /api/bucket/create. Returns (params, error)."""
    account = _valid_account(body)
    if not account:
        return None, 'Compte invalide (attendu : sa-<tenant>-<suffixe>).'

    bucket = _text(body, 'bucket')
    if not BUCKET_RE.match(bucket):
        return None, 'Nom de bucket invalide (3 à 63 caractères : minuscules, chiffres, - et .).'

    try:
        quota_gb = int(_text(body, 'quota_gb'))
    except ValueError:
        return None, 'Quota invalide (nombre entier de Go).'
    if not 1 <= quota_gb <= MAX_QUOTA_GB:
        return None, f'Quota invalide (entre 1 et {MAX_QUOTA_GB} Go).'

    location = _text(body, 'storage_location')
    if not LABEL_RE.match(location):
        return None, 'Storage location invalide.'

    ips = []
    for raw in filter(None, re.split(r'[\s,;]+', _text(body, 'allowed_ips'))):
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            return None, f'Adresse IP invalide : {raw[:64]}'
        if raw not in ips:
            ips.append(raw)
    if not ips:
        return None, 'Au moins une IP autorisée est requise.'

    meta, error = _valid_meta(body)
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
        f"Bucket {params['bucket_name']} créé dans {params['account_id']}.",
    )


def handle_create_account(config, body):
    account = _valid_account(body)
    if not account:
        return 400, {'error': 'Compte invalide (attendu : sa-<tenant>-<suffixe>).'}
    meta, error = _valid_meta(body)
    if error:
        return 400, {'error': error}
    return 200, _run_write(
        lambda: hm.create_storage_account(config, account, meta),
        f'Compte {account} créé.',
    )


def handle_credentials(config, body, create):
    account = _valid_account(body)
    if not account:
        return 400, {'error': 'Compte invalide (attendu : sa-<tenant>-<suffixe>).'}
    fetch = hm.request_credential_for if create else hm.fetch_credential_for
    cred, log, aborted = call_api(lambda: fetch(config, account))
    if aborted:
        return 200, {'ok': False, 'message': MSG_NO_CREDS, 'log': log, 'credentials': []}
    if not cred:
        return 200, {
            'ok': False, 'log': log, 'credentials': [],
            'message': f"Échec {'de la génération' if create else 'de la récupération'} des credentials (voir les détails).",
        }
    return 200, {
        'ok': True, 'log': log,
        'message': f"Credentials {'générées' if create else 'récupérées'} pour {account}.",
        'credentials': [{
            'account': account,
            'access_key_id': cred['access_key_id'],
            'secret_key': cred['secret_key'],
        }],
    }


def parse_bulk_request(config, body):
    """Validate the JSON body of the bulk endpoints. Returns (options, error)."""
    known = list(config.get('environments', []))
    envs = body.get('environments')
    if not isinstance(envs, list) or not envs or not all(isinstance(e, str) for e in envs):
        return None, 'Choisis au moins un environnement.'
    unknown = [e for e in envs if e not in known]
    if unknown:
        return None, f"Environnement inconnu : {', '.join(unknown)[:64]}"
    credentials = body.get('credentials', 'none')
    if credentials not in ('none', 'get', 'create'):
        return None, 'Option credentials invalide.'
    options = {
        'environments': envs,
        'accounts': body.get('accounts') is True,
        'buckets': body.get('buckets') is True,
        'credentials': credentials,
    }
    if not (options['accounts'] or options['buckets'] or credentials != 'none'):
        return None, 'Choisis au moins une étape.'
    return options, None


def build_plan(config, options):
    """What the bulk run would do, from config.json (never from the client)."""
    envs = options['environments']
    plan = {'accounts': [], 'buckets': [], 'credentials': [], 'errors': []}
    if options['accounts']:
        plan['accounts'] = hm.plan_storage_accounts(config, envs)
    if options['buckets']:
        plan['buckets'], plan['errors'] = hm.plan_buckets(config, envs)
    if options['credentials'] != 'none':
        plan['credentials'] = hm.plan_credentials(config, envs)
    return plan


def handle_bulk_plan(config, body):
    options, error = parse_bulk_request(config, body)
    if error:
        return 400, {'error': error}
    plan = build_plan(config, options)
    plan['buckets'] = [
        {**{k: v for k, v in item.items() if k != 'allowed_ips'}, 'ip_count': len(item['allowed_ips'])}
        for item in plan['buckets']
    ]
    return 200, {'plan': plan}


def handle_bulk_run(config, body):
    options, error = parse_bulk_request(config, body)
    if error:
        return 400, {'error': error}
    plan = build_plan(config, options)
    results, credentials = [], []

    def record(kind, target, ok, message):
        results.append({'kind': kind, 'target': target, 'ok': ok, 'message': message})

    def write_step(kind, target, thunk):
        ok, message = _write_outcome(thunk, 'créé')
        record(kind, target, ok, message)

    def work():
        for item in plan['accounts']:
            write_step('Compte', item['account_id'], lambda item=item: hm.create_storage_account(
                config, item['account_id'], item['account_meta_name']))
        for item in plan['buckets']:
            params = {k: v for k, v in item.items() if k != 'env'}
            write_step('Bucket', item['bucket_name'], lambda params=params: hm.create_bucket(config, **params))
        if options['credentials'] != 'none':
            create = options['credentials'] == 'create'
            fetch = hm.request_credential_for if create else hm.fetch_credential_for
            for item in plan['credentials']:
                cred = fetch(config, item['account_id'])
                if cred:
                    credentials.append({
                        'env': item['env'],
                        'account': item['account_id'],
                        'access_key_id': cred['access_key_id'],
                        'secret_key': cred['secret_key'],
                    })
                record('Credentials', item['account_id'], bool(cred),
                       ('générées' if create else 'récupérées') if cred else 'échec (voir les détails)')

    _, log, aborted = call_api(work)
    if aborted:
        record('Abandon', '', False, MSG_NO_CREDS)
    for message in plan['errors']:
        record('Config', '', False, message)
    return 200, {'results': results, 'credentials': credentials, 'log': log}


POST_ROUTES = {
    '/api/bucket/create': handle_create_bucket,
    '/api/account/create': handle_create_account,
    '/api/creds/get': lambda config, body: handle_credentials(config, body, create=False),
    '/api/creds/create': lambda config, body: handle_credentials(config, body, create=True),
    '/api/bulk/plan': handle_bulk_plan,
    '/api/bulk/run': handle_bulk_run,
}


class Handler(BaseHTTPRequestHandler):
    config = None
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
        if self.headers.get('Host', '') in self.allowed_hosts:
            return True
        self._json(403, {'error': 'forbidden host'})
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if url.path == '/':
            self._send(200, PAGE, 'text/html; charset=utf-8')
        elif url.path == '/api/info':
            self._json(200, {'environments': list(self.config.get('environments', []))})
        elif url.path == '/api/search':
            tenant = (parse_qs(url.query).get('tenant') or [''])[0].strip()
            if not TENANT_RE.match(tenant):
                self._json(400, {'error': 'Tenant invalide (lettres, chiffres, - et _ uniquement).'})
                return
            self._json(200, search_tenant(self.config, tenant))
        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self):
        if not self._host_ok():
            return
        # Writes must come from this page: same-origin, and JSON (which a
        # cross-site form can't send without a CORS preflight we never allow).
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc not in self.allowed_hosts:
            self._json(403, {'error': 'forbidden origin'})
            return
        route = POST_ROUTES.get(urlparse(self.path).path)
        if route is None:
            self._json(404, {'error': 'not found'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            self._json(415, {'error': 'application/json attendu'})
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._json(400, {'error': 'Corps de requête invalide.'})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {'error': 'JSON invalide.'})
            return
        if not isinstance(body, dict):
            self._json(400, {'error': 'Requête invalide.'})
            return
        try:
            status, obj = route(self.config, body)
        except KeyError as err:
            status, obj = 400, {'error': f'config.json incomplet : clé manquante {err}.'}
        self._json(status, obj)

    def log_message(self, fmt, *args):
        pass  # keep the terminal for the API messages


def main():
    parser = argparse.ArgumentParser(description='Local web UI for the Hive management tasks')
    parser.add_argument('--config', default='config.json', help='Path to config.json')
    parser.add_argument('--env-file', default='.env', help='Path to a .env file with the credentials')
    parser.add_argument('--port', type=int, default=8765, help='Local port (default: 8765)')
    parser.add_argument('--no-browser', action='store_true', help="Don't open the browser automatically")
    args = parser.parse_args()

    load_dotenv(args.env_file, override=True)
    Handler.config = hm.load_config(args.config)
    Handler.allowed_hosts = frozenset({f'127.0.0.1:{args.port}', f'localhost:{args.port}'})

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = f'http://127.0.0.1:{args.port}/'
    print(f'Hive management UI on {url} (Ctrl+C to stop)')
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
