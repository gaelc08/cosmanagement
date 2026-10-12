"""Edit a COS bucket's lifecycle rules (expire objects older than X days) through the bucket's S3 API.

The management API only shows a bucket's lifecycle policy in the listing; the rules themselves are read and written
with the S3 calls GET / PUT / DELETE ?lifecycle on the S3 endpoint, signed with the credentials of the account that owns
the bucket (the ones "Get the credentials" returns). config.json needs an "s3" section:

    "s3": {"endpoint_url": "https://s3.example.lu", "ssl_verify": true, "ca_bundle_path": "/path/to/ca.pem"}

`ssl_verify` and `ca_bundle_path` default to the "api" section's. A PUT replaces the whole configuration, so the rules
this page manages (ids below) are merged with the other rules of the bucket, which are kept as they are.
"""

import os
import socket
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError, SSLError

from .. import hive_management as hm
from ..cos2pag.s3_lifecycle_client import build_s3_client, get_bucket_lifecycle
from .buckets import _existing_bucket, _valid_account
from .common import MSG_NO_CREDS, _text, call_api

MAX_DAYS = 36500
MAX_PREFIX = 1024
RULE_EXPIRE = 'cosmanagement-expire-current'
RULE_NONCURRENT = 'cosmanagement-expire-noncurrent'
RULE_MULTIPART = 'cosmanagement-abort-multipart'
MANAGED_IDS = (RULE_EXPIRE, RULE_NONCURRENT, RULE_MULTIPART)
MSG_NO_S3 = ('S3 endpoint not configured: add "s3": {"endpoint_url": "https://<S3 endpoint of COS>"} to config.json '
             '(optional "ssl_verify" and "ca_bundle_path", which default to those of "api").')


def s3_settings(config):
    """{server_url, verify_ssl} for the S3 endpoint, or None when config.json has no usable "s3" section."""
    section = config.get('s3')
    url = section.get('endpoint_url') if isinstance(section, dict) else None
    if not isinstance(url, str) or not url.strip().startswith(('http://', 'https://')):
        return None
    if section.get('ssl_verify') is False:
        verify = False
    else:
        verify = section.get('ca_bundle_path') or hm.get_ssl_verify(config)
    return {'server_url': url.strip(), 'verify_ssl': verify}


def _parse_days(value, label):
    """Days from a form field: empty means "no such rule". Returns (days_or_None, error)."""
    text = '' if value is None else str(value).strip()
    if not text:
        return None, None
    try:
        days = int(text)
    except ValueError:
        return None, f'{label}: a whole number of days is expected.'
    if not 1 <= days <= MAX_DAYS:
        return None, f'{label}: between 1 and {MAX_DAYS} days.'
    return days, None


def parse_lifecycle_request(body):
    """The form's values: {expire_days, noncurrent_days, abort_days, prefix}. Returns (params, error)."""
    params = {}
    for key, label in (('expire_days', 'Expire objects after'), ('noncurrent_days', 'Expire noncurrent versions after'),
                       ('abort_days', 'Abort incomplete uploads after')):
        params[key], error = _parse_days(body.get(key), label)
        if error:
            return None, error
    prefix = _text(body, 'prefix')
    if len(prefix) > MAX_PREFIX or any(ord(c) < 32 for c in prefix):
        return None, 'Invalid prefix.'
    params['prefix'] = prefix
    return params, None


def managed_rules(params):
    """The rules for the form's values; a value left empty produces no rule."""
    def rule(rule_id, **action):
        return {'ID': rule_id, 'Status': 'Enabled', 'Filter': {'Prefix': params['prefix']}, **action}

    rules = []
    if params['expire_days']:
        rules.append(rule(RULE_EXPIRE, Expiration={'Days': params['expire_days']}))
    if params['noncurrent_days']:
        rules.append(rule(RULE_NONCURRENT, NoncurrentVersionExpiration={'NoncurrentDays': params['noncurrent_days']}))
    if params['abort_days']:
        rules.append(rule(RULE_MULTIPART, AbortIncompleteMultipartUpload={'DaysAfterInitiation': params['abort_days']}))
    return rules


def current_values(rules):
    """What the form shows for the rules already on the bucket: the managed rules' days, and the prefix they share."""
    by_id = {r.get('ID'): r for r in rules}
    expire = (by_id.get(RULE_EXPIRE) or {}).get('Expiration', {}).get('Days')
    noncurrent = (by_id.get(RULE_NONCURRENT) or {}).get('NoncurrentVersionExpiration', {}).get('NoncurrentDays')
    abort = (by_id.get(RULE_MULTIPART) or {}).get('AbortIncompleteMultipartUpload', {}).get('DaysAfterInitiation')
    prefixes = [(by_id[i].get('Filter') or {}).get('Prefix', by_id[i].get('Prefix', '')) for i in MANAGED_IDS if i in by_id]
    return {'expire_days': expire, 'noncurrent_days': noncurrent, 'abort_days': abort, 'prefix': prefixes[0] if prefixes else ''}


def merge_rules(existing, params):
    """The rules to PUT: every rule of the bucket this page does not manage, then the managed ones."""
    return [r for r in existing if r.get('ID') not in MANAGED_IDS] + managed_rules(params)


def describe_rule(rule):
    """One line saying what a rule does."""
    parts = []
    expiration = rule.get('Expiration') or {}
    if 'Days' in expiration:
        parts.append(f"expire objects {expiration['Days']} days after creation")
    if 'Date' in expiration:
        parts.append(f"expire objects on {expiration['Date']}")
    if expiration.get('ExpiredObjectDeleteMarker'):
        parts.append('delete expired delete markers')
    noncurrent = rule.get('NoncurrentVersionExpiration') or {}
    if 'NoncurrentDays' in noncurrent:
        parts.append(f"expire noncurrent versions {noncurrent['NoncurrentDays']} days after they became noncurrent")
    abort = rule.get('AbortIncompleteMultipartUpload') or {}
    if 'DaysAfterInitiation' in abort:
        parts.append(f"abort incomplete uploads after {abort['DaysAfterInitiation']} days")
    if rule.get('Transitions') or rule.get('NoncurrentVersionTransitions'):
        parts.append('transition to another storage class')
    flt = rule.get('Filter') or {}
    prefix = flt.get('Prefix', rule.get('Prefix', ''))
    scope = f"objects under '{prefix}'" if prefix else 'all objects'
    return ('; '.join(parts) or 'no known action') + f' ({scope})'


def _summaries(rules):
    return [{'id': r.get('ID', ''), 'status': r.get('Status', ''), 'summary': describe_rule(r)} for r in rules]


def _client(config, account):
    """(S3 client, None), or (None, error). Uses the account's existing credentials; nothing is generated."""
    settings = s3_settings(config)
    if settings is None:
        return None, MSG_NO_S3
    bundle = settings['verify_ssl']
    if isinstance(bundle, str) and not os.path.isfile(bundle):
        return None, (f'CA bundle not found: {os.path.abspath(bundle)}. A relative ca_bundle_path is resolved from the folder '
                      'where cos_ui.py was started: fix "ca_bundle_path" in the "s3" (or "api") section of config.json.')
    cred = hm.fetch_credential_for(config, account)
    if not cred:
        return None, f'No credentials found for {account}: use "Get the credentials" or generate some first.'
    return build_s3_client({**settings, 'access_key': cred['access_key_id'], 'secret_key': cred['secret_key']}), None


def source_ip(url):
    """The address this machine would use to reach `url` (nothing is sent), or None.

    Behind a NAT the bucket's firewall sees another address, so this is only a likely candidate.
    """
    parsed = urlparse(url)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)))
            return sock.getsockname()[0]
    except (OSError, TypeError):
        return None


def _is_access_denied(err):
    return isinstance(err, ClientError) and err.response.get('Error', {}).get('Code') in ('AccessDenied', 'Forbidden')


def _s3_error(err):
    if isinstance(err, ClientError):
        error = err.response.get('Error', {})
        return f"S3 error {error.get('Code', '')}: {error.get('Message', '')}".strip()
    if isinstance(err, SSLError):
        return (f'{type(err).__name__}: {err}. The S3 endpoint\'s certificate is not trusted by the CA bundle in use (by default the '
                'one of "api"): set "ca_bundle_path" in the "s3" section of config.json to the CA that signed it '
                '(or "ssl_verify": false to skip the check, for a test only).')
    return f'{type(err).__name__}: {err}'


def _run(config, body, work):
    """Run work(client, bucket, existing_rules) against the bucket's S3 API; returns (status, response)."""
    account, bucket = _valid_account(body), _existing_bucket(body)
    if not account or not bucket:
        return 400, {'error': 'Invalid account or bucket name.'}
    out = {}

    def call():
        client, error = _client(config, account)
        if error:
            out.update(ok=False, message=error)
            return
        try:
            out.update(work(client, bucket, get_bucket_lifecycle(client, bucket)))
        except (ClientError, BotoCoreError) as err:
            out.update(ok=False, message=_s3_error(err))
            if _is_access_denied(err):
                ip = source_ip(s3_settings(config)['server_url'])
                out['source_ip'] = ip
                out['message'] += ('. If the bucket has an IP allow list, this machine\'s address must be in it'
                                   + (f' (likely {ip})' if ip else '') + '.')

    _, log, aborted = call_api(call)
    if aborted:
        out.update(ok=False, message=MSG_NO_CREDS)
    out.setdefault('ok', False)
    out['log'] = log
    return 200, out


def handle_lifecycle_get(config, body):
    def work(client, bucket, rules):
        others = [r for r in rules if r.get('ID') not in MANAGED_IDS]
        return {'ok': True, 'message': '', 'values': current_values(rules), 'others': _summaries(others),
                'managed': _summaries([r for r in rules if r.get('ID') in MANAGED_IDS])}

    return _run(config, body, work)


def handle_lifecycle_update(config, body, dry_run):
    params, error = parse_lifecycle_request(body)
    if error:
        return 400, {'error': error}
    if not dry_run and _text(body, 'confirm') != _existing_bucket(body):
        return 400, {'error': 'Wrong confirmation: type the exact bucket name.'}

    def work(client, bucket, existing):
        desired = merge_rules(existing, params)
        changed = desired != existing
        result = {'ok': True, 'dry_run': dry_run, 'changed': changed, 'resulting': _summaries(desired)}
        if not changed:
            result['message'] = 'Nothing to change: the rules are already as requested.'
        elif dry_run:
            result['message'] = 'Preview only: nothing was sent.'
        else:
            if desired:
                client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={'Rules': desired})
            else:
                client.delete_bucket_lifecycle(Bucket=bucket)
            result['message'] = f'Lifecycle rules of {bucket} updated.'
        return result

    return _run(config, body, work)
