import json
import re


from .. import hive_management as hm
from .common import ACCOUNT_RE, ACCOUNT_TENANT_RE, BUCKET_NAME_RE, CV_PREFIX, LABEL_RE, LABEL_SUFFIX_RE, MSG_NO_CREDS, VAULT_TENANT_RE, _error_in, _http, call_api


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
