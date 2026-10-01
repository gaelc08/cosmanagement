# IBM COS Hive Management Script

## Overview
This script provides modular functions to manage IBM Cloud Object Storage (COS) Hive buckets, storage accounts, and credentials. It is designed to streamline the setup and management process for Hive buckets across different environments (dev, tst, qa, prd).

## Features
- **Storage Accounts Creation**: Automatically creates storage accounts for each environment.
- **Buckets Creation**: Sets up buckets with predefined quotas and firewall rules for each environment.
- **Credentials Generation**: Generates and exports credentials for accessing the buckets.
- **Credentials Retrieval**: Retrieves existing credentials for buckets.
- **Bucket Listing**: Lists every bucket across all Hive storage accounts (`sa-ctie-hive-*`), driven by the API's own account list rather than a guessed sequence.

## Prerequisites
- Python 3.x
- Dependencies installed (`pip install -r requirements.txt`)
- Access to the IBM COS API with appropriate permissions
- Valid credentials for the IBM COS API
- SSL Certificate (if the API uses a self-signed or custom certificate)

## Credentials
Credentials are never stored in `config.json`. `load_auth_header()` resolves them in this order:

1. **`HIVE_AUTH_TOKEN`** -- an already base64-encoded `Basic <base64 user:pass>` string, e.g. in
   a local, gitignored `.env` file:
   ```
   HIVE_AUTH_TOKEN=Basic c3RvcmFnZWFkbWluOnlvdXItcGFzc3dvcmQ=
   ```
   Use this when you already have a known-good token (e.g. from a working `curl`/`base64` test),
   or when the exact byte sequence matters and can't be safely retyped as plain text (some
   accounts' passwords were provisioned with a trailing/embedded control character, e.g. from an
   `echo` without `-n` at creation time -- reconstructing the header from the visible characters
   alone then produces a *different*, invalid token).
2. **`HIVE_USERNAME` + `HIVE_PASSWORD`** -- put them in `.env` instead if you don't already have a
   working token:
   ```
   HIVE_USERNAME=storageadmin
   HIVE_PASSWORD=your-password-here
   ```
   The Basic auth header is computed in Python from these, so passwords with shell-special
   characters (`$`, `=`, `&`, `#`, ...) never need manual escaping. Don't set this alongside
   `HIVE_AUTH_TOKEN` -- the token always wins, so a stale password here would just be ignored
   silently rather than causing confusing auth failures.
3. **A local, gitignored `secrets.json`**: `{"authorization": "Basic <base64 user:pass>"}` or
   `{"username": "...", "password": "..."}`.

`.env` is loaded automatically on every run (via `--env-file`, default `.env` in the current
directory; silently skipped if absent), with `override=True` -- so a corrected `.env` value always
wins over a stale variable left exported in your shell from earlier manual testing.

## Configuration
The script uses a `config.json` file to store all configurations. Modify this file to define:

1. **Buckets**: List of buckets with their storage accounts, names, and quotas for each environment.
2. **Environments**: List of environments (e.g., dev, tst, qa, prd).
3. **API Settings**: Base URL and headers for the IBM COS API.
4. **Storage Locations**: Storage locations for each environment.
5. **Firewall Rules**: Allowed IPs for each environment.
6. **SSL Settings**: Configure SSL verification and optionally specify a custom CA bundle path if the API uses a self-signed or custom certificate.

### SSL Configuration
If the IBM COS API uses a self-signed or custom certificate, you need to:
1. **Disable SSL Verification (Not Recommended for Production)**:
   ```json
   "ssl_verify": false
   ```

2. **Use a Custom CA Bundle (Recommended)**:
   - Obtain the CA certificate and save it to a file (e.g., `osiris-ca.pem`).
   - Update the `config.json` file to specify the path to the CA bundle:
     ```json
     "ssl_verify": true,
     "ca_bundle_path": "/path/to/osiris-ca.pem"
     ```

## Usage
1. Clone the repository or download the script.
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Update the `config.json` file with your settings, and set up credentials (see Credentials above).
4. Run the script with the desired action:
   - To create storage accounts, buckets, and credentials:
     ```bash
     python hive_management.py --action create_all
     ```
   - To retrieve existing credentials:
     ```bash
     python hive_management.py --action get_creds
     ```
   - To list every bucket across all Hive storage accounts:
     ```bash
     python hive_management.py --action list_buckets
     ```
     By default this lists accounts whose id starts with `sa-ctie-hive-`
     (override with `--account-prefix`), and prints each account's
     buckets to stdout. Add `--output buckets.json` to also write the
     result as `{"account-id": ["bucket1", "bucket2", ...]}`.

### Web UI (`cos_ui.py`)
A local web interface to manage IBM COS storage accounts, buckets and credentials. It is not tied to the Hive naming: accounts, buckets and their settings are whatever you type or load.
```bash
python cos_ui.py                   # opens http://127.0.0.1:8765
python cos_ui.py --port 9000 --no-browser
```
It only listens on `127.0.0.1`, and refuses any request whose `Host` header is not `127.0.0.1` or `localhost` (on any port, so a port forward such as VS Code Remote or `ssh -L` that changes the local port works), as a protection against DNS rebinding. If the page answers `forbidden host: '<name>'` because you reach it under another name (machine name, tunnel, proxy), restart with `--allow-host <name>` (repeatable); the message and the console show the exact name to use. From `config.json` it only needs the `api` section (`base_url`, SSL settings); credentials come from `.env` / the environment / `secrets.json` as for `hive_management.py`.

Storage locations (container vaults) belong to a tenant, and their names cannot be read from the API (a bucket's `GET` only gives an internal id). Give the UI the list of your vaults in a local file, `storage_locations.txt` next to the script (or `--locations PATH`); it is git-ignored. You can paste the vault table as it comes, only the first word of each line that starts with `cv-` is kept:
```
cv-acd-01     storage-pool   6.46 kB   2025-06-16 05:54:32 GMT
cv-acd-02     storage-pool   3.36 kB   2026-06-11 08:04:50 GMT
cv-claas-lab  storage-pool   2.7 GB    2026-08-07 09:49:47 GMT
```
A tenant's locations are the vaults named `cv-<tenant>` or `cv-<tenant>-<number>` (`cv-act-01` and `cv-act-02` belong to `act`; `cv-act-geoportal-01` belongs to `act-geoportal`). The tenant of an account `sa-<tenant>-<number>` is what is between `sa-` and the number. To show what a vault is for, add optional labels by name ending to `config.json`, for instance `"storage_location_labels": {"-01": "interne", "-02": "externe S3"}` displays `cv-act-01 (interne)`. A tenant can also be given explicitly, which takes precedence over the deduction:
```json
"tenants": {
  "fina": {"storage_locations": ["cv-fina-01", "cv-fina-02"]}
}
```
In the bucket-creation form, the locations of the account's tenant appear as buttons; a single one is filled in, with several you choose, so an internal or external endpoint is never picked silently. Editing a bucket never changes its location. With no location known for a tenant, every known one is suggested in the creation form and you type the name.

- **Recherche**: **Lister les tenants** lists the tenants. With your list of container vaults loaded (`storage_locations.txt`, see below) or tenants declared in `config.json`, it reads them from the vault names with no API call: the tenant is what lies between `cv-` and the final number (`cv-ctie-hive-dev-01` and `-02` give `ctie-hive-dev`; a vault without a number such as `cv-claas-lab` is its own tenant). Without either, it falls back to the accounts (a single `GET /accounts`, tenant read from `sa-<tenant>-<number>`). The button shows how many vaults (or accounts) each tenant has; click a tenant to list its buckets. Or type a tenant yourself to list the buckets of every account starting with `sa-<tenant>-` (filter box, copy button). If you only know a bucket's name, use the bucket field: **Trouver** looks the exact name up with a single `GET /container/<name>` and reads its owner account from the response (`service_instance`); **Parcourir tous les comptes** lists every account and keeps the buckets whose name contains the text (case-insensitive, partial names allowed), at the cost of one API call per account. Found buckets appear under their owner account, with the same actions as below. Each bucket has **Voir** (its details from the listing, readable: storage location with its label, creation date, quota, usage and percentage, object count, versioning, lifecycle, notifications, replication, object lock, inventory, backup vault...; plus the firewall IPs from a `GET /container/<name>`, the raw listing entry and the raw `GET` response), **Modifier** (quota and allowed IPs, shown as the API returns them; only the fields you change are sent, as a `PATCH`, so the rest of the bucket is left alone; the storage location is not changed) and **Supprimer** (irreversible, you must type the bucket name). Each account has **Récupérer les credentials** and **Générer de nouvelles credentials**. Listing is resource-intensive on the API side (one call per account), so it only runs when a search is started.
- **Rapport**: type a tenant to get its report: every bucket of the tenant's accounts with its storage location, quota, usage, percentage used and object count, with a subtotal per account and a total (80 % and above is highlighted, 100 % in red). **Exporter en CSV** downloads it (semicolon-separated, UTF-8 with a BOM so Excel opens it; exact values in bytes, plus the creation date) and **Imprimer** prints just the report. The tenant is matched exactly (`sa-act-0001` belongs to `act`, `sa-act-geoportal-0001` to `act-geoportal`). Everything comes from the bucket listing of each account (`GET /accounts/<id>/containers` gives `hard_quota`, `bytes_used`, `object_count`, `storage_location`, `creation_time`...), so the cost is one call per account, not per bucket. Values the listing does not carry are shown as `n/d`, left out of the totals, and flagged.
- **Répliquer vers PAG** (button on every bucket): sets up the replication of the bucket to a PoINT Archival Gateway repository by calling the `cos2pag` tool (separate repository), which does the whole job and is idempotent: COS (ACL, IP whitelist, notifications of the bucket), PAG (the tenant's partition, the repository, persistent buffer, lifecycle) and PDR (the replication task and its first copy job). Nothing is reimplemented here: `cos2pag` keeps its own `config.yaml`, its secrets in its `.env`, and its logic. To enable it, install it in the same Python environment (`pip install <folder of cos2pag>`) and point `config.json` at its files:
  ```json
  "cos2pag": {"config": "/path/to/cos2pag/config.yaml", "env_file": "/path/to/cos2pag/.env"}
  ```
  (`env_file` is optional; relative paths are relative to where you start `cos_ui.py`). The panel asks for the PAG tenant, prefilled with the upper-cased tenant of the account (`fina` gives `FINA`); check it, because several real tenants share a prefix (`ME`, `ME-SR`, `ME-SRE`...) and `cos2pag` never guesses it. **Simuler** is `cos2pag --dry-run`: it reads the current state and lists each step as *À faire* or *Déjà en place*, sending nothing. **Exécuter** is only unlocked after a successful simulation for the same tenant, asks for confirmation, and shows each step as done. `cos2pag`'s log is under "Détails de l'appel API". Run the simulation again to see what is still to do, or to check a bucket that is already replicated.
- **Créer**: create one storage account (`PUT /accounts/<id>`) or one bucket (`PUT /container/<name>`) from typed-in values: account, exact bucket name, quota in GB, storage location, allowed IPs (one per line or comma-separated, CIDR accepted) and an optional `x-Account-Meta-name` value. Every value is validated server-side and shown in a confirmation dialog before the call is sent.
- **Création en masse**: create many accounts and buckets from a JSON file that you load (or paste) in the page. **Prévisualiser** lists exactly what will be sent and flags every problem in the file; a file with errors cannot be run. **Exécuter le plan** asks you to type the number of operations, then shows one result per operation. Optionally fetch or generate credentials for the accounts of the file.

Bulk file format; every key is optional and `defaults` fill what a bucket leaves out:
```json
{
  "defaults": {
    "storage_location": "cv-dev-01",
    "allowed_ips": ["10.0.0.1", "10.0.0.2"],
    "quota_gb": 60,
    "account_meta_name": "dev"
  },
  "accounts": [
    {"id": "sa-fina-0001"}
  ],
  "buckets": [
    {"account": "sa-fina-0001", "name": "fina-docs"},
    {"account": "sa-fina-0001", "name": "fina-archive", "quota_gb": 200}
  ]
}
```
A bucket needs `account`, `name`, `quota_gb`, `storage_location` and `allowed_ips`, either of its own or from `defaults`. Accounts referenced by a bucket are not created unless they are listed under `accounts`. If you have a `config.json` in the `hive_management.py` format, **Générer depuis config.json** turns the ticked environments into a bulk file that you can then edit.

Credentials (access/secret keys) are shown in the page, secret masked until revealed, with copy buttons, and are never written to disk by the UI (unlike `create_creds`/`get_creds` on the command line). Errors reported by the API (bad credentials, unreachable host, SSL) are shown under "Détails de l'appel API". Write requests are only accepted as same-origin JSON `POST`s.

**Bucket view / edit / delete:** these, and the exact-name **Trouver** lookup, use `GET`, `PATCH` and `DELETE` on `/container/<name>`. Editing sends only what changed, for instance `PATCH /container/<name>` with `{"firewall": {"allowed_ip": ["10.0.0.1", "10.0.0.2"]}}` for the IPs alone, or `{"hard_quota": <bytes>}` for the quota alone. Note that `PUT` on that URL only creates: on an existing bucket it answers `409 BucketAlreadyOwnedByYou`. The HTTP status and body of every call are shown in the page.

## Output
- **Credentials Files**: The script generates credential files for each bucket in the format `ctie-hive-<bucket_name>` or `ctie-hive-<bucket_name>.json`. Each file contains the access key ID and secret key for the respective environments.
- **Bucket Listing**: With `--action list_buckets`, a summary is printed for each Hive account, and optionally written as JSON via `--output`.

## Notes
- Ensure that the IBM COS API endpoint and ports are accessible from your environment.
- Verify that the firewall rules are correctly configured to allow access from the intended IP ranges.
- The script assumes the existence of specific storage locations (`cv-ctie-hive-dev-01`, `cv-ctie-hive-tst-01`, etc.). Adjust these as needed in the `config.json` file.
- `list_buckets` calls the IBM COS Container Mode Service API's container-listing operation (`GET <accesser>/accounts/{account-id}/containers`), which the API guide flags as resource-intensive on the system. Use it on demand for audits, not in a tight loop or a scheduled/automated job.

## License
This script is provided as-is. Ensure compliance with your organization's policies and IBM COS terms of service.

## Extending the Script
To extend the script for additional functionality:
1. Add new functions to the `hive_management.py` script.
2. Update the `config.json` file with any new configurations.
3. Add new actions to the `argparse` section in the `main` function.
