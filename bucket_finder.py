#!/usr/bin/env python3
"""
Small local web UI to look up the buckets of every storage account whose id
starts with ``sa-<tenant>-``.

Usage:
    python bucket_finder.py                 # http://127.0.0.1:8765, opens the browser
    python bucket_finder.py --port 9000 --no-browser

Same setup as hive_management.py: config.json next to the script, credentials
from .env / environment / secrets.json (see load_auth_header()). The server
only listens on 127.0.0.1 and only ever performs read-only listing calls.

Listing the buckets of an account is flagged as resource-intensive by the API
guide, so a search only runs when the "Rechercher" button is pressed, one
account at a time, and never more than one search at once.
"""

import argparse
import contextlib
import io
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

import hive_management as hm

TENANT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
_api_lock = threading.Lock()

PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hive bucket finder</title>
<style>
  :root { --bg:#fff; --fg:#1b1f24; --muted:#6a737d; --card:#f6f8fa; --line:#d0d7de; --accent:#0969da; --err:#cf222e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --card:#161b22; --line:#30363d; --accent:#58a6ff; --err:#ff7b72; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }
  main { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 0 0 16px; }
  form { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .prefix { font-family: ui-monospace, monospace; color:var(--muted); }
  input, button { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:6px;
                  background:var(--bg); color:var(--fg); }
  input[type=text] { min-width: 200px; }
  button { cursor:pointer; background:var(--accent); border-color:var(--accent); color:#fff; }
  button.secondary { background:var(--bg); color:var(--fg); border-color:var(--line); }
  button:disabled { opacity:.6; cursor:wait; }
  #status { margin:16px 0 8px; color:var(--muted); }
  #status.error { color:var(--err); }
  .toolbar { display:none; gap:8px; margin:8px 0 16px; flex-wrap:wrap; }
  .account { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 14px; margin-bottom:10px; }
  .account h2 { font:600 14px ui-monospace, monospace; margin:0 0 6px; display:flex; justify-content:space-between; }
  .account h2 span { color:var(--muted); font-weight:400; }
  .account ul { margin:0; padding-left:18px; font-family: ui-monospace, monospace; font-size:13px; }
  .empty { color:var(--muted); font-size:13px; }
  details { margin-top:16px; color:var(--muted); font-size:13px; }
  pre { white-space:pre-wrap; background:var(--card); border:1px solid var(--line); border-radius:6px; padding:8px; }
</style>
</head>
<body>
<main>
  <h1>Hive bucket finder</h1>
  <form id="search">
    <span class="prefix">sa-</span>
    <input type="text" id="tenant" placeholder="tenant" autocomplete="off" autofocus required
           pattern="[A-Za-z0-9][A-Za-z0-9_\\-]*" maxlength="64">
    <span class="prefix">-</span>
    <button type="submit" id="go">Rechercher</button>
  </form>
  <div id="status"></div>
  <div class="toolbar" id="toolbar">
    <input type="text" id="filter" placeholder="Filtrer les buckets..." autocomplete="off">
    <button type="button" class="secondary" id="copy">Copier les buckets affichés</button>
  </div>
  <div id="results"></div>
  <details id="logbox" hidden><summary>Détails de l'appel API</summary><pre id="log"></pre></details>
</main>
<script>
  const $ = id => document.getElementById(id);
  let data = {};

  function render() {
    const q = $('filter').value.trim().toLowerCase();
    const box = $('results');
    box.textContent = '';
    let shown = 0;
    for (const [account, buckets] of Object.entries(data)) {
      const list = buckets.filter(b => b.toLowerCase().includes(q));
      if (q && !list.length) continue;
      shown += list.length;
      const card = document.createElement('div');
      card.className = 'account';
      const h = document.createElement('h2');
      h.append(account);
      const count = document.createElement('span');
      count.textContent = list.length + (q ? ' / ' + buckets.length : '') + ' bucket(s)';
      h.append(count);
      card.append(h);
      if (list.length) {
        const ul = document.createElement('ul');
        for (const b of list) { const li = document.createElement('li'); li.textContent = b; ul.append(li); }
        card.append(ul);
      } else {
        const p = document.createElement('div'); p.className = 'empty'; p.textContent = 'Aucun bucket';
        card.append(p);
      }
      box.append(card);
    }
    box.dataset.shown = shown;
  }

  $('filter').addEventListener('input', render);

  $('copy').addEventListener('click', () => {
    const q = $('filter').value.trim().toLowerCase();
    const names = Object.values(data).flat().filter(b => b.toLowerCase().includes(q));
    navigator.clipboard.writeText(names.join('\\n'));
    $('status').className = '';
    $('status').textContent = names.length + ' bucket(s) copié(s).';
  });

  $('search').addEventListener('submit', async e => {
    e.preventDefault();
    const tenant = $('tenant').value.trim();
    const go = $('go'), status = $('status');
    go.disabled = true;
    status.className = '';
    status.textContent = 'Recherche en cours (un appel API par compte, ça peut prendre un moment)...';
    $('results').textContent = '';
    $('toolbar').style.display = 'none';
    try {
      const res = await fetch('/api/search?tenant=' + encodeURIComponent(tenant));
      const out = await res.json();
      $('logbox').hidden = !out.log;
      $('log').textContent = out.log || '';
      if (!res.ok || out.error) {
        status.className = 'error';
        status.textContent = out.error || ('Erreur HTTP ' + res.status);
        return;
      }
      data = out.accounts;
      const n = Object.keys(data).length;
      const total = Object.values(data).reduce((s, b) => s + b.length, 0);
      status.className = out.has_error ? 'error' : '';
      status.textContent = n
        ? n + ' compte(s) "' + out.prefix + '*", ' + total + ' bucket(s).' + (out.has_error ? ' Des erreurs ont eu lieu, voir les détails.' : '')
        : 'Aucun compte "' + out.prefix + '*" trouvé.' + (out.has_error ? ' Voir les détails ci-dessous.' : '');
      $('filter').value = '';
      $('toolbar').style.display = n ? 'flex' : 'none';
      render();
    } catch (err) {
      status.className = 'error';
      status.textContent = 'Erreur : ' + err;
    } finally {
      go.disabled = false;
    }
  });
</script>
</body>
</html>
"""


def search_tenant(config, tenant):
    """Return {prefix, accounts: {id: [buckets]}, log, has_error} for sa-<tenant>-*.

    hive_management reports API problems by printing and returning what it
    got, so its output is captured and handed to the UI instead of being
    lost on the server console.
    """
    prefix = f'sa-{tenant}-'
    accounts = {}
    log = io.StringIO()
    failed = False
    with _api_lock, contextlib.redirect_stdout(log):
        try:
            for account in hm.list_accounts(config, prefix=prefix):
                accounts[account['id']] = hm.list_bucket_names(config, account['id'])
        except SystemExit:  # e.g. load_auth_header() found no credentials
            failed = True
    text = log.getvalue().strip()
    has_error = failed or any(
        line.startswith(('Error', 'Network error')) for line in text.splitlines()
    )
    return {
        'prefix': prefix,
        'accounts': dict(sorted(accounts.items())),
        'log': text,
        'has_error': has_error,
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

    def do_GET(self):
        # Refuse foreign Host headers so a web page can't reach this server via DNS rebinding.
        if self.headers.get('Host', '') not in self.allowed_hosts:
            self._json(403, {'error': 'forbidden host'})
            return

        url = urlparse(self.path)
        if url.path == '/':
            self._send(200, PAGE, 'text/html; charset=utf-8')
        elif url.path == '/api/search':
            tenant = (parse_qs(url.query).get('tenant') or [''])[0].strip()
            if not TENANT_RE.match(tenant):
                self._json(400, {'error': 'Tenant invalide (lettres, chiffres, - et _ uniquement).'})
                return
            self._json(200, search_tenant(self.config, tenant))
        else:
            self._json(404, {'error': 'not found'})

    def log_message(self, fmt, *args):
        pass  # keep the terminal for the API messages


def main():
    parser = argparse.ArgumentParser(description='Local web UI to find buckets by tenant (sa-<tenant>-*)')
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
    print(f'Hive bucket finder on {url} (Ctrl+C to stop)')
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
