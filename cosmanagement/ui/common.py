import contextlib
import io
import re
import threading

import requests

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




def _error_in(log):
    return any(line.startswith(('Error', 'Network error')) for line in log.splitlines())




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
