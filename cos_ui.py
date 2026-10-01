#!/usr/bin/env python3
"""
Local web UI to manage IBM COS storage accounts, buckets and credentials.

Usage:
    python cos_ui.py                        # http://127.0.0.1:8765, opens the browser
    python cos_ui.py --port 9000 --no-browser

Tabs:
  Recherche          list the buckets of every account starting with sa-<tenant>-;
                     view, edit or delete a bucket; fetch or generate an
                     account's credentials
  Créer              create one storage account, or one bucket, from typed-in values
  Création en masse  create many accounts / buckets from a JSON file that you load
                     or paste in the page (format below), with a preview and a
                     per-item result

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
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

import hive_management as hm

TENANT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
ACCOUNT_RE = re.compile(r'^sa-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')          # names we create
BUCKET_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$')     # names that already exist
LABEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
CV_PREFIX = 'cv-'                                                      # container vault names
LABEL_SUFFIX_RE = re.compile(r'^[-_.A-Za-z0-9]{1,20}$')
ACCOUNT_TENANT_RE = re.compile(r'^sa-(.+)-\d+$')
FIND_RE = re.compile(r'^[A-Za-z0-9._-]{1,255}$')                       # a bucket name, or part of one
MAX_QUOTA_GB = 1_000_000
MAX_BODY = 1024 * 1024
CREATED = (201,)
DONE = (200, 201, 202, 204)
MSG_NO_CREDS = 'Identifiants introuvables (voir les détails).'
BULK_KEYS = {'defaults', 'accounts', 'buckets'}
DEFAULT_KEYS = {'storage_location', 'allowed_ips', 'quota_gb', 'account_meta_name'}
BUCKET_KEYS = DEFAULT_KEYS | {'account', 'name'}
ACCOUNT_KEYS = {'id', 'account_meta_name'}
_api_lock = threading.Lock()

PAGE = r"""<!doctype html>
<html lang="fr">
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

  #search, #find { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  #find { margin-top:10px; }
  #search input, #find input { min-width:200px; }
  #find-help { margin:4px 0 0; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  #tenants-panel .chips { margin-top:8px; }
  .grid-form .chips { grid-column:2; margin-top:-4px; }
  @media (max-width: 560px) { .grid-form .chips { grid-column:1; } }
  #tenants-panel input { margin-top:8px; min-width:220px; }
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

  details { margin-top:16px; color:var(--muted); font-size:13px; }
  pre { white-space:pre-wrap; background:var(--card); border:1px solid var(--line); border-radius:6px; padding:8px; }
</style>
</head>
<body>
<main>
  <h1>COS management</h1>
  <nav id="tabs">
    <button type="button" class="active" data-tab="search">Recherche</button>
    <button type="button" data-tab="create">Créer</button>
    <button type="button" data-tab="bulk">Création en masse</button>
  </nav>
  <div id="status"></div>
  <div id="creds" class="panel" hidden></div>
  <div id="bucket-panel" class="panel" hidden></div>

  <section id="tab-search">
    <form id="search">
      <span class="prefix">sa-</span>
      <input type="text" id="tenant" placeholder="tenant" autocomplete="off" autofocus required list="tenants-datalist"
             pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" maxlength="64">
      <span class="prefix">-</span>
      <button type="submit" id="go">Rechercher</button>
      <button type="button" class="secondary" id="tenants-go">Lister les tenants</button>
    </form>
    <datalist id="tenants-datalist"></datalist>
    <div id="tenants-panel" class="panel" hidden></div>
    <form id="find">
      <span class="prefix">bucket</span>
      <input type="text" id="bname" placeholder="nom du bucket" autocomplete="off" maxlength="255"
             pattern="[A-Za-z0-9._\-]+">
      <button type="submit" id="find-go">Trouver</button>
      <button type="button" class="secondary" id="scan-go">Parcourir tous les comptes</button>
    </form>
    <p class="empty" id="find-help">« Trouver » interroge le bucket par son nom exact (un seul appel) et en déduit son compte.
    « Parcourir » cherche dans tous les comptes, avec un nom partiel si besoin : un appel API par compte, ça peut être long.</p>
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
      <div class="buttons"><button type="submit" id="a-go">Créer le compte</button></div>
    </form>

    <h3>Créer un bucket</h3>
    <form id="bucket-form" class="grid-form" autocomplete="off">
      <label for="c-account">Compte</label>
      <input type="text" id="c-account" required list="accounts-list" maxlength="132" placeholder="ex. sa-fina-0001">
      <datalist id="accounts-list"></datalist>
      <label for="c-bucket">Nom du bucket</label>
      <input type="text" id="c-bucket" required maxlength="63" placeholder="ex. fina-docs">
      <small>3 à 63 caractères : minuscules, chiffres, - et .</small>
      <label for="c-quota">Quota (Go)</label>
      <input type="text" id="c-quota" required inputmode="numeric" placeholder="ex. 60">
      <label for="c-location">Storage location</label>
      <input type="text" id="c-location" required list="locations-list" placeholder="ex. cv-dev-01">
      <datalist id="locations-list"></datalist>
      <div id="c-location-choices" class="chips"></div>
      <label for="c-ips">IP autorisées</label>
      <textarea id="c-ips" rows="4" required placeholder="Une IP (ou CIDR) par ligne, ou séparées par des virgules"></textarea>
      <label for="c-meta">x-Account-Meta-name</label>
      <input type="text" id="c-meta" placeholder="facultatif">
      <div class="buttons"><button type="submit" id="c-go">Créer le bucket</button></div>
    </form>
  </section>

  <section id="tab-bulk" hidden>
    <p class="empty">Crée plusieurs comptes et buckets d'après un fichier de configuration JSON. Charge un fichier, colle son contenu, ou
    pars de l'exemple, puis prévisualise avant d'exécuter. Format : <code>defaults</code> (valeurs par défaut des buckets),
    <code>accounts</code> (comptes à créer) et <code>buckets</code> (buckets à créer).</p>
    <div class="row-buttons">
      <button type="button" class="secondary" id="b-load">Charger un fichier JSON...</button>
      <input type="file" id="b-file" accept=".json,application/json" hidden>
      <button type="button" class="secondary" id="b-example">Insérer un exemple</button>
    </div>
    <fieldset id="b-legacy" hidden><legend>Générer depuis config.json (format hive_management.py)</legend>
      <div id="b-envs"></div>
      <button type="button" class="secondary" id="b-expand">Générer le fichier</button>
    </fieldset>
    <textarea id="b-doc" rows="16" spellcheck="false" placeholder='{"accounts": [...], "buckets": [...]}'></textarea>
    <fieldset><legend>Credentials pour les comptes du fichier</legend>
      <label><input type="radio" name="b-creds" value="none" checked> aucune</label>
      <label><input type="radio" name="b-creds" value="get"> récupérer l'existante</label>
      <label><input type="radio" name="b-creds" value="create"> générer une nouvelle</label>
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
  let lastSearch = null;   // {kind: 'tenant' | 'bucket' | 'scan', value}
  let tenantLocations = {};   // tenant -> storage locations, from config.json
  let allLocations = [];
  let locationLabels = {};   // name suffix -> label, from config.json
  let tenants = [];

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
      try { out = await res.json(); } catch (e) { out = {error: 'Réponse invalide (HTTP ' + res.status + ')'}; }
    } catch (err) {
      out = {error: 'Erreur : ' + err};
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
      wrap.append(el('div', c.account, 'mono'));

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

  function closeBucketPanel() { $('bucket-panel').textContent = ''; $('bucket-panel').hidden = true; }

  function panelHeader(title) {
    const box = $('bucket-panel');
    box.textContent = '';
    box.append(el('strong', title));
    box.hidden = false;
    return box;
  }

  async function openBucket(account, bucket, mode) {
    setStatus('Lecture du bucket ' + bucket + '...', false);
    const out = await post('/api/bucket/get', {bucket});
    showLog(out.log);
    const ok = !out.error && out.ok;
    setStatus(ok ? '' : (out.error || out.message), !ok);
    const box = panelHeader((mode === 'edit' ? 'Modifier le bucket ' : 'Bucket ') + bucket + ' (' + account + ')');

    if (mode === 'view') {
      if (ok) {
        const rows = out.details !== null && typeof out.details === 'object' ? flatten(out.details, '', []) : [];
        if (rows.length) box.append(table(['Champ', 'Valeur'], rows));
        const raw = el('details');
        raw.append(el('summary', 'Réponse brute'), el('pre', out.raw));
        box.append(raw);
      }
      const close = el('button', 'Fermer', 'small secondary');
      close.addEventListener('click', closeBucketPanel);
      box.append(close);
      return;
    }

    // edit: a PATCH changes only what it carries, so only the fields you changed are sent.
    const details = ok ? out.details : null;
    const quota = findQuota(details);
    const ips = findIps(details);
    box.append(el('div', 'Seuls les champs modifiés sont envoyés (PATCH). La storage location ne se change pas ici.', 'empty'));
    if (!ok) box.append(el('div', 'Détails illisibles : les champs restent vides, renseigne ce que tu veux changer.', 'empty'));
    else {
      const unread = [];
      if (quota === undefined) unread.push('le quota');
      if (ips === undefined) unread.push('les IP autorisées');
      if (unread.length) box.append(el('div', 'Non lu dans la réponse de l\'API : ' + unread.join(' et ') + '. Ouvre « Voir » pour les retrouver dans la réponse brute.', 'empty'));
    }

    const form = el('form', undefined, 'grid-form');
    form.autocomplete = 'off';
    const fQuota = el('input'); fQuota.type = 'text'; fQuota.inputMode = 'numeric';
    fQuota.value = typeof quota === 'number' ? String(Math.round(quota / 1e9 * 100) / 100) : '';
    const fIps = el('textarea'); fIps.rows = 4; fIps.value = Array.isArray(ips) ? ips.join('\n') : '';
    form.append(el('label', 'Quota (Go)'), fQuota, el('label', 'IP autorisées'), fIps);
    const ipList = v => v.split(/[\s,;]+/).filter(Boolean).join(',');
    const initial = {quota: fQuota.value.trim(), ips: ipList(fIps.value)};
    const buttons = el('div', undefined, 'buttons');
    const save = el('button', 'Enregistrer'); save.type = 'submit';
    const cancel = el('button', 'Annuler', 'secondary'); cancel.type = 'button';
    cancel.addEventListener('click', closeBucketPanel);
    buttons.append(save, cancel);
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
      if (!body.quota_gb && !body.allowed_ips) { setStatus('Aucune modification à envoyer.', false); return; }
      const summary = 'Modifier le bucket "' + bucket + '"\n' +
        (body.quota_gb ? '  quota : ' + body.quota_gb + ' Go\n' : '') +
        (body.allowed_ips ? '  IP autorisées : ' + body.allowed_ips.split(/[\s,;]+/).filter(Boolean).length + '\n' : '');
      if (!confirm(summary)) return;
      save.disabled = true;
      setStatus('Modification en cours...', false);
      const res = await post('/api/bucket/update', body);
      save.disabled = false;
      if (res.log) showLog(res.log);
      const done = !res.error && res.ok;
      setStatus(res.error || res.message, !done);
      if (done) closeBucketPanel();
    });
    box.append(form);
  }

  async function deleteBucket(account, bucket) {
    const typed = prompt('Supprimer définitivement le bucket "' + bucket + '" (compte ' + account + ').\n' +
                         'Cette action est irréversible. Tape le nom du bucket pour confirmer.');
    if (typed !== bucket) { setStatus('Suppression annulée.', false); return; }
    setStatus('Suppression en cours...', false);
    const out = await post('/api/bucket/delete', {bucket, confirm: typed});
    if (out.log) showLog(out.log);
    const ok = !out.error && out.ok;
    setStatus(out.error || out.message, !ok);
    if (ok) {
      closeBucketPanel();
      if (lastSearch && lastSearch.kind === 'tenant') { await runSearch(lastSearch.value, true); return; }
      for (const list of Object.values(data)) { const i = list.indexOf(bucket); if (i >= 0) list.splice(i, 1); }
      for (const acct of Object.keys(data)) if (!data[acct].length) delete data[acct];
      render();
    }
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
        for (const b of list) {
          const li = el('li');
          li.append(b);
          for (const [act, label, cls] of [['view', 'Voir', 'secondary'], ['edit', 'Modifier', 'secondary'], ['delete', 'Supprimer', 'danger']]) {
            const btn = el('button', label, 'small ' + cls);
            btn.dataset.act = act; btn.dataset.bucket = b; btn.dataset.account = account;
            li.append(btn);
          }
          ul.append(li);
        }
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
    const act = e.target.closest('button[data-act]');
    if (act) {
      const {account, bucket} = act.dataset;
      if (act.dataset.act === 'delete') await deleteBucket(account, bucket);
      else await openBucket(account, bucket, act.dataset.act);
      return;
    }
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
    lastSearch = {kind: 'tenant', value: tenant};
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

  async function runFind(name, scan) {
    if (!name) { setStatus('Saisis un nom de bucket.', true); return; }
    lastSearch = {kind: scan ? 'scan' : 'bucket', value: name};
    const buttons = [$('find-go'), $('scan-go')];
    buttons.forEach(b => { b.disabled = true; });
    setStatus(scan ? 'Parcours de tous les comptes (un appel API par compte, ça peut être long)...' : 'Recherche du bucket...', false);
    $('results').textContent = '';
    $('toolbar').style.display = 'none';
    try {
      const res = await fetch('/api/bucket/find?name=' + encodeURIComponent(name) + (scan ? '&scan=1' : ''));
      const out = await res.json();
      showLog(out.log);
      if (!res.ok || out.error) { setStatus(out.error || ('Erreur HTTP ' + res.status), true); return; }
      data = out.accounts;
      const n = Object.keys(data).length;
      const total = Object.values(data).reduce((s, b) => s + b.length, 0);
      let msg;
      if (out.message) msg = out.message;
      else if (total) msg = total + ' bucket(s) trouvé(s) dans ' + n + ' compte(s)' + (scan ? ' (' + out.scanned + ' compte(s) parcouru(s))' : '') + '.';
      else if (out.found_without_owner) msg = 'Bucket trouvé, mais aucun compte propriétaire dans la réponse de l\'API (voir les détails) : utilise « Parcourir tous les comptes ».';
      else if (scan) msg = 'Aucun bucket contenant "' + name + '" dans les ' + out.scanned + ' compte(s) parcouru(s).';
      else msg = 'Aucun bucket nommé "' + name + '"' + (out.status ? ' (HTTP ' + out.status + ')' : '') +
                 '. Si tu sais qu\'il existe, l\'API ne propose peut-être pas cette recherche directe : utilise « Parcourir tous les comptes ».';
      setStatus(msg, out.has_error);
      $('filter').value = '';
      $('toolbar').style.display = total ? 'flex' : 'none';
      fillAccounts();
      render();
    } catch (err) {
      setStatus('Erreur : ' + err, true);
    } finally {
      buttons.forEach(b => { b.disabled = false; });
    }
  }

  $('find').addEventListener('submit', e => { e.preventDefault(); runFind($('bname').value.trim(), false); });
  $('scan-go').addEventListener('click', () => runFind($('bname').value.trim(), true));
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
  function renderTenants(filter) {
    const box = $('tenants-panel');
    box.textContent = '';
    box.append(el('strong', tenants.length + ' tenant(s) (déduits des comptes sa-<tenant>-<numéro>)'));
    const input = el('input'); input.type = 'text'; input.placeholder = 'Filtrer...'; input.value = filter || '';
    input.addEventListener('input', () => renderTenants(input.value)); box.append(el('br'), input);
    const chips = el('div', undefined, 'chips');
    for (const t of tenants) {
      if (filter && !t.name.toLowerCase().includes(filter.toLowerCase())) continue;
      const b = el('button', t.name + ' (' + t.accounts.length + ')', 'small secondary');
      b.title = t.accounts.join(', ');
      b.addEventListener('click', () => { $('tenant').value = t.name; runSearch(t.name, false); });
      chips.append(b);
    }
    const close = el('button', 'Fermer', 'small secondary');
    close.addEventListener('click', () => { box.textContent = ''; box.hidden = true; });
    box.append(chips, el('br'), close);
    box.hidden = false;
    if (filter) input.focus();
  }

  $('tenants-go').addEventListener('click', async () => {
    const btn = $('tenants-go');
    btn.disabled = true;
    setStatus('Lecture des comptes (un seul appel API)...', false);
    try {
      const res = await fetch('/api/tenants');
      const out = await res.json();
      showLog(out.log);
      if (!res.ok || out.error) { setStatus(out.error || ('Erreur HTTP ' + res.status), true); return; }
      tenants = out.tenants;
      const list = $('tenants-datalist');
      list.textContent = '';
      for (const t of tenants) { const o = el('option'); o.value = t.name; list.append(o); }
      setStatus(out.message || (tenants.length + ' tenant(s) trouvé(s).'), out.has_error);
      if (tenants.length) renderTenants(''); else { $('tenants-panel').hidden = true; }
    } catch (err) {
      setStatus('Erreur : ' + err, true);
    } finally {
      btn.disabled = false;
    }
  });

  // ---- create one account / one bucket ---------------------------------------
  async function afterCreate(account) {
    if (lastSearch && lastSearch.kind === 'tenant' && account.startsWith('sa-' + lastSearch.value + '-')) {
      await runSearch(lastSearch.value, true);
    }
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

  // ---- bulk creation from a JSON file --------------------------------------------
  let plannedKey = null;
  let plannedCount = 0;

  function bulkRequest() {
    const text = $('b-doc').value.trim();
    if (!text) throw new Error('Le fichier de configuration est vide.');
    let document_;
    try { document_ = JSON.parse(text); } catch (e) { throw new Error('JSON invalide : ' + e.message); }
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
    if (file.size > 1024 * 1024) { setStatus('Fichier trop volumineux (1 Mo maximum).', true); return; }
    const reader = new FileReader();
    reader.onload = () => {
      $('b-doc').value = String(reader.result);
      invalidatePlan();
      setStatus('Fichier "' + file.name + '" chargé. Prévisualise avant d\'exécuter.', false);
    };
    reader.onerror = () => setStatus('Lecture du fichier impossible.', true);
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
    } catch (err) { setStatus('Impossible de lire la configuration : ' + err, true); }
  }

  $('b-expand').addEventListener('click', async () => {
    const environments = [...document.querySelectorAll('#b-envs input:checked')].map(i => i.value);
    const out = await post('/api/bulk/expand', {environments});
    if (out.error) { setStatus(out.error, true); return; }
    $('b-doc').value = JSON.stringify(out.document, null, 2);
    invalidatePlan();
    setStatus('Fichier généré depuis config.json.' + (out.warnings.length ? ' ' + out.warnings.join(' ') : ''), out.warnings.length > 0);
  });

  $('b-preview').addEventListener('click', async () => {
    invalidatePlan();
    $('b-result').textContent = '';
    let req;
    try { req = bulkRequest(); } catch (err) { setStatus(err.message, true); return; }
    setStatus('Calcul du plan...', false);
    const out = await post('/api/bulk/plan', req);
    if (out.error) { setStatus(out.error, true); return; }
    const plan = out.plan, box = $('b-plan');
    const total = plan.accounts.length + plan.buckets.length + plan.credentials.length;
    setStatus(plan.errors.length
      ? plan.errors.length + ' erreur(s) dans le fichier : corrige-les pour pouvoir exécuter.'
      : total + ' opération(s) prévue(s).', plan.errors.length > 0);
    if (plan.errors.length) {
      const list = el('div', undefined, 'err-list');
      for (const e of plan.errors) list.append(el('div', e));
      box.append(list);
    }
    if (plan.accounts.length) {
      box.append(el('h3', 'Storage accounts (' + plan.accounts.length + ')'));
      box.append(table(['Compte', 'x-Account-Meta-name'], plan.accounts.map(a => [a.account_id, a.account_meta_name || ''])));
    }
    if (plan.buckets.length) {
      box.append(el('h3', 'Buckets (' + plan.buckets.length + ')'));
      box.append(table(['Compte', 'Bucket', 'Quota (Go)', 'Storage location', 'IP'],
        plan.buckets.map(b => [b.account_id, b.bucket_name, String(b.quota_gb), b.storage_location, String(b.ip_count)])));
    }
    if (plan.credentials.length) {
      box.append(el('h3', (req.credentials === 'create' ? 'Nouvelles credentials' : 'Credentials existantes') + ' (' + plan.credentials.length + ')'));
      box.append(table(['Compte'], plan.credentials.map(c => [c.account_id])));
    }
    if (total && !plan.errors.length) { plannedKey = JSON.stringify(req); plannedCount = total; $('b-run').disabled = false; }
  });

  $('b-run').addEventListener('click', async () => {
    let req;
    try { req = bulkRequest(); } catch (err) { setStatus(err.message, true); return; }
    if (JSON.stringify(req) !== plannedKey) { invalidatePlan(); setStatus('Le fichier a changé : prévisualise de nouveau.', true); return; }
    const n = String(plannedCount);
    if (!confirm('Exécuter ' + n + ' opération(s) d\'après ce fichier ?')) return;
    if (prompt('Tape ' + n + ' (le nombre d\'opérations) pour confirmer.') !== n) {
      setStatus('Exécution annulée.', false);
      return;
    }
    $('b-run').disabled = true; $('b-preview').disabled = true;
    setStatus('Exécution en cours (un appel API par opération)...', false);
    const out = await post('/api/bulk/run', req);
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


def list_tenants(config):
    """Tenants deduced from the account ids (one GET /accounts). Returns {tenants: [{name, accounts}], log, has_error}."""
    ids, log, aborted = call_api(lambda: [a['id'] for a in hm.list_accounts(config, prefix='sa-')])
    tenants = {}
    for account_id in ids or []:
        tenants.setdefault(tenant_of(account_id), []).append(account_id)
    return {
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


def find_bucket(config, name, scan):
    """Look a bucket up without knowing its account.

    Direct (scan=False): one GET on the exact name; the owner is read from the
    response's `service_instance`. Scan: list every account and keep the
    buckets whose name contains `name` (case-insensitive), which costs one API
    call per account. Returns {name, mode, accounts: {id: [buckets]},
    found_without_owner, scanned, message, log, has_error}.
    """
    result = {
        'name': name, 'mode': 'scan' if scan else 'direct', 'accounts': {},
        'found_without_owner': False, 'scanned': 0, 'message': '', 'has_error': False, 'status': None,
    }

    def work():
        if scan:
            needle = name.lower()
            accounts = hm.list_accounts(config, prefix='sa-')
            for account in accounts:
                matches = [b for b in hm.list_bucket_names(config, account['id']) if needle in b.lower()]
                if matches:
                    result['accounts'][account['id']] = matches
            result['scanned'] = len(accounts)
            return
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
            if owner:
                result['accounts'][owner] = [name]
            else:
                result['found_without_owner'] = True
        elif status != 404:
            result.update(message=f'Échec : HTTP {status} - {text[:500]}', has_error=True)

    _, log, aborted = call_api(work)
    if aborted:
        result.update(message=MSG_NO_CREDS, has_error=True)
    result['has_error'] = result['has_error'] or any(
        line.startswith(('Error', 'Network error')) for line in log.splitlines()
    )
    result['accounts'] = dict(sorted(result['accounts'].items()))
    result['log'] = log
    return result


def _http(thunk):
    """Run a (status, text) API call. Returns (status, text, error); status is None on a network error."""
    try:
        status, text = thunk()
    except requests.RequestException as err:
        return None, '', f'Erreur réseau : {err}'
    return status, text, None


def _write_outcome(thunk, ok_message, ok_statuses=CREATED):
    """Run a write call; returns (ok, message)."""
    status, text, error = _http(thunk)
    if error:
        return False, error
    if status in ok_statuses:
        return True, ok_message
    return False, f'Échec : HTTP {status} - {text[:500]}'


def _run_write(thunk, ok_message, ok_statuses=CREATED):
    outcome, log, aborted = call_api(lambda: _write_outcome(thunk, ok_message, ok_statuses))
    ok, message = (False, MSG_NO_CREDS) if aborted else outcome
    return {'ok': ok, 'message': message, 'log': log}


def _text(body, key):
    return str(body.get(key, '')).strip()


def _valid_account(body):
    account = _text(body, 'account')
    return account if ACCOUNT_RE.match(account) else None


def _valid_meta(value):
    """Optional x-Account-Meta-name. Returns (value_or_None, error)."""
    meta = str(value or '').strip()
    if meta and not LABEL_RE.match(meta):
        return None, "Valeur invalide pour l'en-tête x-Account-Meta-name."
    return meta or None, None


def _parse_quota(value):
    """Quota in GB from an int or a numeric string. Returns (int, error)."""
    if isinstance(value, bool):
        return None, 'Quota invalide (nombre entier de Go).'
    try:
        quota = int(str(value).strip())
    except ValueError:
        return None, 'Quota invalide (nombre entier de Go).'
    if not 1 <= quota <= MAX_QUOTA_GB:
        return None, f'Quota invalide (entre 1 et {MAX_QUOTA_GB} Go).'
    return quota, None


def _parse_ips(value):
    """IPs / CIDRs from a list or a separated string. Returns (list, error); an empty list is not an error."""
    if isinstance(value, str):
        raws = re.split(r'[\s,;]+', value.strip())
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        raws = [v.strip() for v in value]
    else:
        return None, 'Liste d\'IP invalide.'
    ips = []
    for raw in filter(None, raws):
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            return None, f'Adresse IP invalide : {raw[:64]}'
        if raw not in ips:
            ips.append(raw)
    return ips, None


def _parse_location(value):
    location = str(value or '').strip()
    return (location, None) if LABEL_RE.match(location) else (None, 'Storage location invalide.')


def parse_create_bucket(body):
    """Validate the JSON body of POST /api/bucket/create. Returns (params, error)."""
    account = _valid_account(body)
    if not account:
        return None, 'Compte invalide (attendu : sa-<tenant>-<suffixe>).'
    bucket = _text(body, 'bucket')
    if not BUCKET_RE.match(bucket):
        return None, 'Nom de bucket invalide (3 à 63 caractères : minuscules, chiffres, - et .).'
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
        return None, 'Au moins une IP autorisée est requise.'
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
        f"Bucket {params['bucket_name']} créé dans {params['account_id']}.",
    )


def _existing_bucket(body):
    name = _text(body, 'bucket')
    return name if BUCKET_NAME_RE.match(name) else None


def handle_get_bucket(config, body):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Nom de bucket invalide.'}
    result, log, aborted = call_api(lambda: _http(lambda: hm.get_bucket(config, bucket)))
    if aborted:
        return 200, {'ok': False, 'message': MSG_NO_CREDS, 'log': log}
    status, text, error = result
    if error or status != 200:
        return 200, {'ok': False, 'log': log, 'message': error or f'Échec : HTTP {status} - {text[:500]}'}
    try:
        details = json.loads(text)
    except json.JSONDecodeError:
        details = None
    return 200, {'ok': True, 'details': details, 'raw': text[:20000], 'log': log}


def parse_update_bucket(body):
    """Validate the JSON body of POST /api/bucket/update (a PATCH: only the fields given are changed)."""
    bucket = _existing_bucket(body)
    if not bucket:
        return None, 'Nom de bucket invalide.'
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
            return None, 'Au moins une IP autorisée est requise.'
        params['allowed_ips'] = ips
    if _text(body, 'storage_location'):
        params['storage_location'], error = _parse_location(body['storage_location'])
        if error:
            return None, error
    if len(params) == 1:
        return None, 'Rien à modifier.'
    return params, None


def handle_update_bucket(config, body):
    params, error = parse_update_bucket(body)
    if error:
        return 400, {'error': error}
    return 200, _run_write(
        lambda: hm.update_bucket(config, **params),
        f"Bucket {params['bucket_name']} modifié.",
        DONE,
    )


def handle_delete_bucket(config, body):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Nom de bucket invalide.'}
    if _text(body, 'confirm') != bucket:
        return 400, {'error': 'Confirmation incorrecte : tape le nom exact du bucket.'}
    return 200, _run_write(
        lambda: hm.delete_bucket(config, bucket),
        f'Bucket {bucket} supprimé.',
        DONE,
    )


def handle_create_account(config, body):
    account = _valid_account(body)
    if not account:
        return 400, {'error': 'Compte invalide (attendu : sa-<tenant>-<suffixe>).'}
    meta, error = _valid_meta(body.get('account_meta_name'))
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
        return plan, ['Le fichier doit être un objet JSON : {"accounts": [...], "buckets": [...]}.']
    errors = [f'Clé inconnue à la racine : {str(key)[:40]}' for key in document if key not in BULK_KEYS]

    defaults = document.get('defaults') or {}
    if not isinstance(defaults, dict):
        errors.append('"defaults" doit être un objet.')
        defaults = {}
    errors += [f'Clé inconnue dans defaults : {str(key)[:40]}' for key in defaults if key not in DEFAULT_KEYS]
    defaults = {k: v for k, v in defaults.items() if k in DEFAULT_KEYS and v is not None}

    accounts_in = document.get('accounts') or []
    buckets_in = document.get('buckets') or []
    if not isinstance(accounts_in, list) or not isinstance(buckets_in, list):
        return plan, errors + ['"accounts" et "buckets" doivent être des listes.']
    if not accounts_in and not buckets_in:
        return plan, errors + ['Le fichier ne contient ni comptes ni buckets.']

    seen_accounts, seen_buckets, targets = set(), set(), []

    def add_target(account_id):
        if account_id not in targets:
            targets.append(account_id)

    for i, entry in enumerate(accounts_in):
        where = f'accounts[{i}]'
        if isinstance(entry, str):
            entry = {'id': entry}
        if not isinstance(entry, dict):
            errors.append(f'{where} : attendu un objet {{"id": ...}} ou un identifiant.')
            continue
        unknown = [str(k)[:40] for k in entry if k not in ACCOUNT_KEYS]
        if unknown:
            errors.append(f'{where} : clé inconnue {", ".join(unknown)}.')
            continue
        account_id = str(entry.get('id', '')).strip()
        if not ACCOUNT_RE.match(account_id):
            errors.append(f'{where} : identifiant de compte invalide "{account_id[:64]}" (attendu : sa-<tenant>-<suffixe>).')
            continue
        if account_id in seen_accounts:
            errors.append(f'{where} : compte {account_id} en double.')
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
            errors.append(f'{where} : attendu un objet.')
            continue
        name = str(entry.get('name', '')).strip()
        where = f'{where} ({name[:64]})' if name else where
        unknown = [str(k)[:40] for k in entry if k not in BUCKET_KEYS]
        if unknown:
            errors.append(f'{where} : clé inconnue {", ".join(unknown)}.')
            continue
        merged = {**defaults, **{k: v for k, v in entry.items() if v is not None}}
        account_id = str(merged.get('account', '')).strip()
        if not ACCOUNT_RE.match(account_id):
            errors.append(f'{where} : compte invalide "{account_id[:64]}" (attendu : sa-<tenant>-<suffixe>).')
            continue
        if not BUCKET_RE.match(name):
            errors.append(f'{where} : nom de bucket invalide (3 à 63 caractères : minuscules, chiffres, - et .).')
            continue
        if name in seen_buckets:
            errors.append(f'{where} : bucket en double.')
            continue
        for key in ('quota_gb', 'storage_location', 'allowed_ips'):
            if key not in merged:
                errors.append(f'{where} : "{key}" manquant (ni dans le bucket, ni dans defaults).')
        if any(key not in merged for key in ('quota_gb', 'storage_location', 'allowed_ips')):
            continue
        quota_gb, error = _parse_quota(merged['quota_gb'])
        location, error2 = _parse_location(merged['storage_location'])
        ips, error3 = _parse_ips(merged['allowed_ips'])
        meta, error4 = _valid_meta(merged.get('account_meta_name'))
        error = error or error2 or error3 or error4 or (None if ips else 'Au moins une IP autorisée est requise.')
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
        return None, None, [], 'Option credentials invalide.'
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
        return 400, {'error': 'Choisis au moins un environnement.'}
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
        return 400, {'error': f'Le fichier contient {len(errors)} erreur(s) : prévisualise-le et corrige-les.'}
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
                       ('générées' if create else 'récupérées') if cred else 'échec (voir les détails)')

    _, log, aborted = call_api(work)
    if aborted:
        record('Abandon', '', False, MSG_NO_CREDS)
    return 200, {'results': results, 'credentials': credentials, 'log': log}


POST_ROUTES = {
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
            })
        elif url.path == '/api/tenants':
            self._json(200, list_tenants(self.config))
        elif url.path == '/api/bucket/find':
            query = parse_qs(url.query)
            name = (query.get('name') or [''])[0].strip()
            if not FIND_RE.match(name):
                self._json(400, {'error': 'Nom de bucket invalide (lettres, chiffres, . - et _ uniquement).'})
                return
            self._json(200, find_bucket(self.config, name, scan=(query.get('scan') or [''])[0] == '1'))
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
        if origin and not host_allowed(urlparse(origin).netloc, self.allowed_hosts):
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
