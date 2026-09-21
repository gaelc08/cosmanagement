#!/usr/bin/env python3
"""
Hive Management Script
=====================

This script provides modular functions to manage IBM COS Hive buckets, storage accounts, and credentials.
It includes:
- Creating storage accounts
- Creating buckets with quotas and firewall rules
- Generating and exporting credentials
- Retrieving existing credentials
- Listing every bucket across all Hive storage accounts

Usage:
------
1. Configure `config.json` (structure/quotas/firewall) using config.example.json as a template.
2. Provide credentials via a local, gitignored `.env` file (HIVE_USERNAME /
   HIVE_PASSWORD, or a pre-encoded HIVE_AUTH_TOKEN), via those same
   environment variables directly, or via a local, gitignored secrets.json
   (see load_auth_header()).
3. Run the script with the desired action:
   - Full flow:              python hive_management.py --action create_all
   - Storage accounts only:  python hive_management.py --action create_sa
   - Buckets only:           python hive_management.py --action create_buckets
   - New credentials only:   python hive_management.py --action create_creds
   - Retrieve existing:      python hive_management.py --action get_creds
   - List all Hive buckets:  python hive_management.py --action list_buckets
"""

import argparse
import base64
import json
import os
import sys

import requests
from dotenv import load_dotenv

DEFAULT_HEADERS = {
    'Accept': '*/*',
    'Content-Type': 'application/json',
    'Connection': 'keep-alive',
}


def load_config(path='config.json'):
    """Load configuration from config.json file (no secrets in here)."""
    try:
        with open(path, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print(f"Error: {path} not found. Copy config.example.json to {path} and adjust it.")
        sys.exit(1)
    except json.JSONDecodeError as err:
        print(f"Error: invalid JSON in {path}: {err}")
        sys.exit(1)


def _basic_auth_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode('utf-8')).decode('ascii')
    return f"Basic {token}"


def load_auth_header():
    """
    Resolve the Authorization header without ever storing it in config.json.

    Order of precedence (each source can come from a gitignored `.env`
    file, loaded via load_dotenv() in main(), or from the real
    environment):
    1. HIVE_USERNAME + HIVE_PASSWORD environment variables -- the header
       is computed here in Python, so passwords with shell-special
       characters (`$`, `=`, `&`, ...) never need manual escaping.
    2. HIVE_AUTH_TOKEN environment variable, already-encoded (e.g. "Basic xxxxxxxx==")
    3. A local, gitignored secrets.json: {"username": ..., "password": ...}
       or {"authorization": "Basic xxxxxxxx=="}
    """
    username = os.environ.get('HIVE_USERNAME')
    password = os.environ.get('HIVE_PASSWORD')
    if username and password:
        return _basic_auth_header(username, password)

    env_token = os.environ.get('HIVE_AUTH_TOKEN')
    if env_token:
        return env_token

    try:
        with open('secrets.json', 'r') as f:
            secrets = json.load(f)
        if secrets.get('username') and secrets.get('password'):
            return _basic_auth_header(secrets['username'], secrets['password'])
        token = secrets.get('authorization')
        if token:
            return token
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as err:
        print(f"Error: invalid JSON in secrets.json: {err}")
        sys.exit(1)

    print(
        "Error: no credentials found.\n"
        "Set HIVE_USERNAME + HIVE_PASSWORD (in a .env file or the environment), "
        "or HIVE_AUTH_TOKEN, or create a local secrets.json (gitignored) with "
        '{"username": "...", "password": "..."} or {"authorization": "Basic <base64 user:pass>"}.'
    )
    sys.exit(1)


def build_headers(config):
    """Fresh headers dict for a single call site (never mutate a shared dict)."""
    headers = dict(DEFAULT_HEADERS)
    headers['Authorization'] = load_auth_header()
    return headers


def get_ssl_verify(config):
    """Determine SSL verification settings based on config."""
    api = config['api']
    if not api.get('ssl_verify', True):
        return False
    if api.get('ca_bundle_path'):
        return api['ca_bundle_path']
    return True


def create_storage_accounts(config):
    """Create storage accounts for each environment."""
    base_sa_url = f"{config['api']['base_url']}/accounts/"
    buckets = config['buckets']
    environments = config['environments']
    ssl_verify = get_ssl_verify(config)

    with requests.Session() as s:
        for env in environments:
            headers = build_headers(config)
            headers['x-Account-Meta-name'] = f'ctie-hive-{env}'
            for bucket in buckets:
                sa_name = f'sa-ctie-hive-{env}-{bucket["sa"]}'
                url = f"{base_sa_url}{sa_name}"
                try:
                    response = s.put(url, headers=headers, data={}, verify=ssl_verify, timeout=30)
                except requests.RequestException as err:
                    print(f"Network error creating storage account {sa_name}: {err}")
                    continue
                if response.status_code != 201:
                    print(f"Error creating storage account {sa_name}: {response.status_code} - {response.text}")


def create_buckets(config):
    """Create buckets with quotas and firewall rules."""
    base_bucket_url = f"{config['api']['base_url']}/container/"
    buckets = config['buckets']
    environments = config['environments']
    storage_locations = config['storage_locations']
    firewall_rules = config['firewall_rules']
    ssl_verify = get_ssl_verify(config)

    with requests.Session() as s:
        for env in environments:
            headers = build_headers(config)
            headers['x-Account-Meta-name'] = f'ctie-hive-{env}'
            for bucket in buckets:
                quota_key = f'quota-{env}'
                if quota_key not in bucket:
                    print(f"Error: quota for env '{env}' not defined for bucket '{bucket['bucket']}'.")
                    continue

                bucket_payload = {
                    "hard_quota": bucket[quota_key] * 1000000000,
                    "firewall": {"allowed_ip": firewall_rules[env]},
                    "storage_location": storage_locations[env],
                    "service_instance": f'sa-ctie-hive-{env}-{bucket["sa"]}',
                }

                bucket_name = f'ctie-hive-{env}-{bucket["bucket"]}'
                url = f"{base_bucket_url}{bucket_name}"
                try:
                    response = s.put(
                        url, headers=headers, data=json.dumps(bucket_payload),
                        verify=ssl_verify, timeout=30,
                    )
                except requests.RequestException as err:
                    print(f"Network error creating bucket {bucket_name}: {err}")
                    continue
                if response.status_code != 201:
                    print(f"Error creating bucket {bucket_name}: {response.status_code} - {response.text}")


def _request_credential(session, base_cred_url, headers, project_id, ssl_verify):
    """POST a new credential for project_id. Returns the export dict or None."""
    creds_body = {"credential": {"project_id": project_id, "type": "ec2"}}
    try:
        response = session.post(
            base_cred_url, headers=headers, data=json.dumps(creds_body),
            verify=ssl_verify, timeout=30,
        )
    except requests.RequestException as err:
        print(f"Network error creating credentials for {project_id}: {err}")
        return None

    if response.status_code != 201:
        print(f"Error creating credentials for {project_id}: {response.status_code} - {response.text}")
        return None

    try:
        decoded = response.json()
        return {
            'name': decoded['credential']['project_id'],
            'access_key_id': decoded['credential']['blob']['access'],
            'secret_key': decoded['credential']['blob']['secret'],
        }
    except (KeyError, TypeError, json.JSONDecodeError) as err:
        print(f"Error parsing credentials for {project_id}: {err}")
        return None


def _fetch_credential(session, base_cred_url, headers, project_id, ssl_verify):
    """GET an existing credential for project_id. Returns the export dict or None."""
    params = {'project_id': project_id}
    try:
        response = session.get(
            base_cred_url, params=params, headers=headers, data={},
            verify=ssl_verify, timeout=30,
        )
    except requests.RequestException as err:
        print(f"Network error retrieving credentials for {project_id}: {err}")
        return None

    if response.status_code != 200:
        print(f"Error retrieving credentials for {project_id}: {response.status_code} - {response.text}")
        return None

    try:
        decoded = response.json()
        return {
            'name': decoded['credentials'][0]['project_id'],
            'access_key_id': decoded['credentials'][0]['blob']['access'],
            'secret_key': decoded['credentials'][0]['blob']['secret'],
        }
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as err:
        print(f"Error parsing credentials for {project_id}: {err}")
        return None


def _write_creds_file(bucket_name, env_to_creds, environments):
    """Write the unified {"credentials": [...]} format, one entry per env, env order preserved."""
    file_to_create = f'ctie-hive-{bucket_name}.json'
    entries = []
    for env in environments:
        cred = env_to_creds.get(env)
        if cred:
            entry = dict(cred)
            entry['env'] = env
            entries.append(entry)
    with open(file_to_create, 'w') as f:
        json.dump({'credentials': entries}, f, indent=2)
        f.write('\n')
    return file_to_create


def create_credentials(config):
    """Generate new credentials for buckets and export them (unified format)."""
    base_cred_url = f"{config['api']['base_url']}/credentials"
    buckets = config['buckets']
    environments = config['environments']
    ssl_verify = get_ssl_verify(config)
    headers = build_headers(config)

    with requests.Session() as s:
        for bucket in buckets:
            env_to_creds = {}
            for env in environments:
                project_id = f'sa-ctie-hive-{env}-{bucket["sa"]}'
                cred = _request_credential(s, base_cred_url, headers, project_id, ssl_verify)
                if cred:
                    env_to_creds[env] = cred
            out_file = _write_creds_file(bucket['bucket'], env_to_creds, environments)
            print(f"Wrote {out_file}")


def get_credentials(config):
    """Retrieve existing credentials for buckets and export them (unified format)."""
    base_cred_url = f"{config['api']['base_url']}/credentials/"
    buckets = config['buckets']
    environments = config['environments']
    ssl_verify = get_ssl_verify(config)
    headers = build_headers(config)

    with requests.Session() as s:
        for bucket in buckets:
            env_to_creds = {}
            for env in environments:
                project_id = f'sa-ctie-hive-{env}-{bucket["sa"]}'
                cred = _fetch_credential(s, base_cred_url, headers, project_id, ssl_verify)
                if cred:
                    env_to_creds[env] = cred
            out_file = _write_creds_file(bucket['bucket'], env_to_creds, environments)
            print(f"Wrote {out_file}")


def _fetch_paginated_entries(session, base_url, headers, ssl_verify, wrap_key, marker_key, error_label):
    """GET a paginated collection endpoint and return its raw entries.

    The API has been observed wrapping the array under a key (e.g.
    ``{"accounts": [...], "is_truncated": ..., "limit": ...}`` or
    ``{"containers": [...], "is_truncated": ..., "limit": ...}``) rather
    than returning a bare array -- ``wrap_key`` names that key. Entries
    themselves may be plain strings or dicts; callers normalize those.
    Pagination prefers the response's own ``is_truncated`` flag and
    falls back to a short-page heuristic when that field is absent.
    """
    entries = []
    marker = None
    while True:
        params = {'limit': 1000}
        if marker:
            params['marker'] = marker
        try:
            response = session.get(base_url, params=params, headers=headers, verify=ssl_verify, timeout=30)
        except requests.RequestException as err:
            print(f"Network error {error_label}: {err}")
            return entries

        if response.status_code == 204:
            break
        if response.status_code != 200:
            print(f"Error {error_label}: {response.status_code} - {response.text}")
            return entries

        try:
            page = response.json()
        except json.JSONDecodeError as err:
            print(f"Error parsing response while {error_label}: {err}")
            return entries

        is_truncated = None
        if isinstance(page, dict):
            is_truncated = page.get('is_truncated')
            page = page.get(wrap_key, [])

        if not page:
            break
        entries.extend(page)

        if is_truncated is False:
            break
        if is_truncated is None and len(page) < 1000:
            break

        last = page[-1]
        marker = last.get(marker_key) if isinstance(last, dict) else last

    return entries


def list_accounts(config, prefix=None):
    """List storage accounts on the API, optionally filtered client-side
    by id prefix (e.g. "sa-ctie-hive-" to keep only Hive accounts out of
    every account on the cluster).

    Returns a list of {"id": str} dicts.
    """
    base_url = f"{config['api']['base_url']}/accounts"
    ssl_verify = get_ssl_verify(config)
    headers = build_headers(config)

    with requests.Session() as s:
        raw_entries = _fetch_paginated_entries(
            s, base_url, headers, ssl_verify, wrap_key='accounts', marker_key='id', error_label='listing accounts',
        )

    accounts = []
    for entry in raw_entries:
        account_id = entry.get('id') if isinstance(entry, dict) else entry
        if account_id:
            accounts.append({'id': account_id})

    raw_count = len(accounts)
    if prefix:
        accounts = [a for a in accounts if a['id'].startswith(prefix)]
        print(f"Fetched {raw_count} account(s) from the API, {len(accounts)} matched prefix '{prefix}'.")
    else:
        print(f"Fetched {raw_count} account(s) from the API.")
    return accounts


def list_bucket_names(config, account_id):
    """List container/bucket names under one storage account.

    Per the API guide (Chapter 10, "Container / bucket listing"), this is
    a resource-intensive operation on the system -- fine to run on demand
    from this CLI, but it should not be called in a tight loop or wired
    into an automated/scheduled process.
    """
    base_url = f"{config['api']['base_url']}/accounts/{account_id}/containers"
    ssl_verify = get_ssl_verify(config)
    headers = build_headers(config)
    headers['Accept'] = 'application/json'

    with requests.Session() as s:
        raw_entries = _fetch_paginated_entries(
            s, base_url, headers, ssl_verify, wrap_key='containers', marker_key='name',
            error_label=f"listing buckets for {account_id}",
        )

    names = []
    for entry in raw_entries:
        name = entry.get('name') if isinstance(entry, dict) else entry
        if name:
            names.append(name)
    return names


def list_hive_buckets(config, account_prefix='sa-ctie-hive-'):
    """List every bucket across every Hive storage account.

    Driven by the real, current account list on the API (GET /accounts,
    filtered by account_prefix) rather than by regenerating the
    sa-ctie-hive-{env}-{seq} pattern locally: the sequence numbers in use
    aren't contiguous (e.g. 0009 then 0013, skipping 0010-0012), so
    walking the real account list avoids guessing which sequence numbers
    actually exist.

    Returns {account_id: [bucket_name, ...]}.
    """
    accounts = list_accounts(config, prefix=account_prefix)
    if not accounts:
        print(f"No accounts found with prefix '{account_prefix}'.")
        return {}

    result = {}
    for account in accounts:
        account_id = account.get('id')
        if not account_id:
            continue
        bucket_names = list_bucket_names(config, account_id)
        result[account_id] = bucket_names
        summary = f"{account_id}: {len(bucket_names)} bucket(s)"
        if bucket_names:
            summary += f" -> {', '.join(bucket_names)}"
        print(summary)
    return result


def main():
    parser = argparse.ArgumentParser(description='Hive Management Script')
    parser.add_argument(
        '--action', type=str, required=True,
        choices=['create_all', 'create_sa', 'create_buckets', 'create_creds', 'get_creds', 'list_buckets'],
        help=(
            'create_all: storage accounts + buckets + new credentials. '
            'create_sa / create_buckets / create_creds: run one step in isolation '
            '(useful to retry after a partial failure). '
            'get_creds: retrieve already-existing credentials. '
            'list_buckets: list every bucket for every Hive storage account on the API.'
        ),
    )
    parser.add_argument('--config', type=str, default='config.json', help='Path to config.json')
    parser.add_argument(
        '--env-file', type=str, default='.env',
        help='Path to a .env file with HIVE_USERNAME/HIVE_PASSWORD (or HIVE_AUTH_TOKEN). '
             'Default: .env in the current directory; silently skipped if absent.',
    )
    parser.add_argument(
        '--account-prefix', type=str, default='sa-ctie-hive-',
        help="Account id prefix used by 'list_buckets' to select Hive accounts (default: sa-ctie-hive-)",
    )
    parser.add_argument(
        '--output', type=str, default=None,
        help="Optional path to write 'list_buckets' results as JSON ({account_id: [bucket, ...]})",
    )
    args = parser.parse_args()

    # override=True: a variable already exported in the shell (e.g. from
    # earlier manual testing) must not shadow a since-corrected .env value.
    load_dotenv(args.env_file, override=True)

    config = load_config(args.config)

    if args.action == 'create_all':
        print("Creating storage accounts...")
        create_storage_accounts(config)
        print("Creating buckets...")
        create_buckets(config)
        print("Creating credentials...")
        create_credentials(config)
    elif args.action == 'create_sa':
        print("Creating storage accounts...")
        create_storage_accounts(config)
    elif args.action == 'create_buckets':
        print("Creating buckets...")
        create_buckets(config)
    elif args.action == 'create_creds':
        print("Creating credentials...")
        create_credentials(config)
    elif args.action == 'get_creds':
        print("Retrieving credentials...")
        get_credentials(config)
    elif args.action == 'list_buckets':
        print(f"Listing buckets for accounts matching prefix '{args.account_prefix}'...")
        result = list_hive_buckets(config, account_prefix=args.account_prefix)
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(result, f, indent=2)
                f.write('\n')
            print(f"Wrote {args.output}")

    print("Done.")


if __name__ == "__main__":
    main()