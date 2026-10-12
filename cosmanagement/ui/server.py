"""
Local web UI to manage IBM COS storage accounts, buckets and credentials.

Usage:
    python3 cos_ui.py                        # http://127.0.0.1:8765, opens the browser
    python3 cos_ui.py --port 9000 --no-browser
    python3 -m cosmanagement.ui --no-browser

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
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from .. import hive_management as hm
from .buckets import (
    handle_create_account, handle_create_bucket, handle_credentials, handle_delete_bucket, handle_get_bucket,
    handle_update_bucket,
)
from .bulk import handle_bulk_expand, handle_bulk_plan, handle_bulk_run
from .common import MAX_BODY, PATTERN_RE, SEARCH_KINDS
from .replication import (
    handle_replication, handle_replication_partitions, handle_replication_status, handle_teardown, replication_settings,
)
from .search import list_tenants, load_locations, location_labels, search, tenant_locations

STATIC_DIR = Path(__file__).parent / 'static'
STATIC_TYPES = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'"



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
            CSP,
        )
        self.end_headers()
        self.wfile.write(payload)

    def _send_static(self, path, content_type):
        self._send(200, path.read_text(encoding='utf-8'), content_type)

    def _static(self, name):
        """Serve a .js or .css file from the static folder (nothing outside it, nothing of another type)."""
        path = (STATIC_DIR / name).resolve()
        if path.suffix in STATIC_TYPES and path.is_file() and STATIC_DIR.resolve() in path.parents:
            self._send_static(path, STATIC_TYPES[path.suffix])
        else:
            self._json(404, {'error': 'not found'})

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
            self._send_static(STATIC_DIR / 'index.html', 'text/html; charset=utf-8')
        elif url.path.startswith('/static/'):
            self._static(url.path[len('/static/'):])
        elif url.path == '/api/info':
            by_tenant = tenant_locations(self.config)
            known = {name for names in by_tenant.values() for name in names}
            known.update(self.locations)
            self._json(200, {
                'environments': list(hm.hive_config(self.config).get('environments', [])),
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
    hm.add_hive_argument(parser)
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
    Handler.config = hm.load_config(args.config, args.hive_config)
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
