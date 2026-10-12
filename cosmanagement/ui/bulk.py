

from .. import hive_management as hm
from .common import ACCOUNT_KEYS, ACCOUNT_RE, BUCKET_KEYS, BUCKET_RE, BULK_KEYS, DEFAULT_KEYS, MSG_NO_CREDS, _write_outcome, call_api
from .buckets import _parse_ips, _parse_location, _parse_quota, _valid_meta


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
    """Expand the Hive settings of config.json (hive_management.py format) into a bulk file."""
    known = list(hm.hive_config(config).get('environments', []))
    envs = body.get('environments')
    if not isinstance(envs, list) or not envs or not all(isinstance(e, str) for e in envs):
        return 400, {'error': 'Choose at least one environment.'}
    unknown = [e for e in envs if e not in known]
    if unknown:
        return 400, {'error': f"Unknown environment: {', '.join(unknown)[:64]}"}
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
