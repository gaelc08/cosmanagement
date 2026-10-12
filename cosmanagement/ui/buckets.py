import ipaddress
import json
import re


from .. import hive_management as hm
from .common import ACCOUNT_RE, BUCKET_NAME_RE, BUCKET_RE, DONE, LABEL_RE, MAX_QUOTA_GB, MSG_NO_CREDS, _http, _run_write, _text, call_api


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
