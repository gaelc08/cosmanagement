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

## Repository layout
```
cos_ui.py, hive_management.py   thin launchers (python3 cos_ui.py / python3 hive_management.py ...)
cosmanagement/
  hive_management.py            Hive API client and command line (storage accounts, buckets, credentials)
  cos2pag/                      COS -> PAG replication setup through PDR (its own docs: docs/cos2pag.md)
    teardown.py                 suspend / undo a replication
  ui/                           the web UI
    server.py                   HTTP server, routes, command line
    search.py buckets.py bulk.py replication.py common.py   the API behind each tab
    static/                     index.html, style.css, js/*.js (loaded in the order listed in index.html)
tests/                          pytest (cos2pag, UI search / server / replication)
docs/cos2pag.md                 cos2pag documentation
cos2pag.example.yaml, cos2pag.env.example   configuration templates for the replication
pyproject.toml                  packaging (pip install . gives cos-ui, cos2pag and hive-management commands)
```
`cos2pag` used to be a separate repository (`gaelc08/cos2pag`); it now lives here, with its history, and the web UI calls it directly.

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
     python3 hive_management.py --action create_all
     ```
   - To retrieve existing credentials:
     ```bash
     python3 hive_management.py --action get_creds
     ```
   - To list every bucket across all Hive storage accounts:
     ```bash
     python3 hive_management.py --action list_buckets
     ```
     By default this lists accounts whose id starts with `sa-ctie-hive-`
     (override with `--account-prefix`), and prints each account's
     buckets to stdout. Add `--output buckets.json` to also write the
     result as `{"account-id": ["bucket1", "bucket2", ...]}`.

### Web UI (`cos_ui.py`)
A local web interface to manage IBM COS storage accounts, buckets and credentials. It is not tied to the Hive naming: accounts, buckets and their settings are whatever you type or load.
```bash
python3 cos_ui.py                   # opens http://127.0.0.1:8765
python3 cos_ui.py --port 9000 --no-browser
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

- **Search**: pick what you search by (**Tenant**, **Storage account** or **Bucket**) and type a full name or a pattern with `*` (`fina`, `fi*`, `*-0001`, `*docs*`; case-insensitive). A tenant is matched exactly (`sa-act-0001` belongs to `act`, `sa-act-geoportal-0001` to `act-geoportal`); the `sa-` prefix may be left out for an account. A full bucket name costs a single `GET /container/<name>` (its owner account is read from `service_instance`); a bucket pattern lists every account, one API call per account. Results are grouped by account and show, for every bucket, its **object count**, **size used**, **quota** and **percentage of quota** (with a bar, orange from 80 %, red at 100 %), straight from the bucket listing (`GET /accounts/<id>/containers`: `hard_quota`, `bytes_used`, `object_count`...), so there is no extra call. **List tenants** lists the tenants (read from `storage_locations.txt` or `config.json` with no API call, else from a single `GET /accounts`); tick one or several (with a filter box and *Tick all*) and click **Show the buckets**. You can also type several tenants separated by commas (`fina,act`). A filter box, **Copy displayed buckets**, **Export as CSV** (semicolon-separated, UTF-8 with a BOM so Excel opens it, exact values in bytes) and **Print** work on the displayed results. **View** shows a bucket's details (usage summary first, then storage location, creation date, versioning, lifecycle, replication, object lock..., the firewall IPs from a `GET /container/<name>`, and the raw responses). Each account has **Get the credentials** and **Generate new credentials**.
- **Edit**: **Edit** on a result opens this tab for that bucket: its usage summary, then quota and allowed IPs (shown as the API returns them; only the fields you change are sent, as a `PATCH`; the storage location is not changed), **Replicate to PAG**, the account's credentials and **Delete the bucket** (irreversible, you must type the bucket name).
  **Lifecycle (expire objects after X days)**, also in this tab: shows the bucket's lifecycle rules and edits three of them: *expire objects after N days*, *expire noncurrent versions after N days* and *abort incomplete uploads after N days*, optionally limited to a prefix. It acts on the COS source bucket (the retention of the PAG copy is the separate *Retention (days)* of the replication panel). The rules are read and written with the S3 API of the bucket (`GET/PUT/DELETE ?lifecycle`), signed with the credentials of the account that owns it, so `config.json` needs the S3 endpoint of COS: `"s3": {"endpoint_url": "https://s3.example.lu"}` (optional `ssl_verify` and `ca_bundle_path`, which default to those of `api`), and the account must already have credentials (nothing is generated). A PUT replaces the whole configuration, so the rules this page manages (ids `cosmanagement-expire-current`, `cosmanagement-expire-noncurrent`, `cosmanagement-abort-multipart`) are merged with the other rules of the bucket, which are kept untouched. **Preview** shows the resulting rules and sends nothing; **Save** is only possible after a preview of the same values and asks you to type the bucket name. Emptying the form removes only the managed rules. If the bucket is replicated, a warning says whether its PDR task mirrors deletions (`enableDeletion`): if it does, objects expired on COS are also deleted from PAG.
- **Replicate to PAG** (button on every bucket): sets up the replication of the bucket to a PoINT Archival Gateway repository by calling the `cos2pag` package (in this repository, `cosmanagement/cos2pag/`), which does the whole job and is idempotent: COS (ACL, IP whitelist, notifications of the bucket), PAG (the tenant's partition, the repository, persistent buffer, lifecycle) and PDR (the replication task and its first copy job). Nothing is reimplemented here: `cos2pag` keeps its own `config.yaml`, its secrets in its `.env`, and its logic. It is installed with the other dependencies (`pip install -r requirements.txt`); copy `cos2pag.example.yaml` to `config.yaml` and `cos2pag.env.example` to `.env`, fill them in, and point `config.json` at them:
  ```json
  "cos2pag": {"config": "/path/to/config.yaml", "env_file": "/path/to/cos2pag.env"}
  ```
  (`env_file` is optional; relative paths are relative to where you start `cos_ui.py`). The panel asks for the PAG tenant, which names the partition (one per tenant). `cos2pag` never guesses it, because several real tenants share a prefix (`ME`, `ME-SR`, `ME-SRE`...). The panel reads the partitions that exist on PAG, offers their names in the field, and says what will happen: the partition exists and will be used, **or** it does not exist and `cos2pag` will create it with the `new_partition_defaults` of its `config.yaml`. The field starts from the tenant of the account (`fina`), replaced by the partition that really exists when there is one (whatever its case).

  **First copy job.** `cos2pag` starts the initial `Copy` job of a new task right after patching the bucket's ACL, and PDR could then fail with `Failed to enumerate objects: Forbidden (AccessDenied)` although the grant was in place (COS does not always honour a new grant at once). So the UI asks `cos2pag` not to start it (`pdr.auto_start_job` forced to `false` for the run) and starts it itself once a `ListObjectsV2` with the credentials PDR uses (`pdr.source_s3`) succeeds, retrying for up to 60 s (`"cos2pag": {"job_wait_seconds": N}` in `config.json`, `0` to start at once). If COS still answers AccessDenied after the wait, the `pdr.job` step fails with the likely causes (the key of `pdr.source_s3` does not belong to the ACL grantee, or PDR's IP is not in the whitelist) and no job is started; fix it, then start a `Copy` from PDR. The job is only started when the task is created by this run.

  **Partition names and case.** `cos2pag` looks a partition up by *exact* name, case included, while PAG refuses to create a partition whose name differs from an existing one only by case (`'CreatePartition()' failed because the partition does already exist`). So when the tenant is not an exact partition name but one exists that differs only by case, the request is **refused before `cos2pag` runs**, with the exact name to use (and a button that puts it in the field). The same check protects the deletion of a PAG repository: a wrong case never makes it silently skip or hit the wrong partition. A tenant that resembles no existing partition is left to `cos2pag`, which creates it. **Simulate** is `cos2pag --dry-run`: it reads the current state and lists each step as *To do* or *Already in place*, sending nothing. **Run** is only unlocked after a successful simulation for the same tenant, asks for confirmation, and shows each step as done. `cos2pag`'s log is under "API call details". Run the simulation again to see what is still to do, or to check a bucket that is already replicated. When the panel opens it also reads the **current state** (read-only: the PDR task whose alias is the bucket name, its schedule, and the noncurrent-version retention found in the lifecycle rules of the PAG repository). The **Retention (days)** field is `cos2pag --noncurrent-expiration-days`: it is prefilled with the current value, and leaving it empty keeps whatever `pag.lifecycle` says in cos2pag's `config.yaml`. **Editing a replication is the same action with a new value**: `cos2pag` is idempotent and updates what differs (the lifecycle rule on PAG, the PDR task's options, schedule and notifications), so change the retention, simulate, and the steps that would change show as *To do*. Changing the tenant or the retention locks **Run** until you simulate again.

  **Suspend or undo** (same panel; `cosmanagement/cos2pag/teardown.py`, which reuses cos2pag's own clients and configuration). `cos2pag` only creates, so undoing means reversing what it did, in the reverse order of its pipeline (PDR, then PAG, then COS). Three levels:
  - **Suspend the task in PDR**: disables the schedule and the notifications of the PDR task (`PATCH /api/tasks/{id}`), deleting nothing. Reversible: **Run** in the replication part above runs `cos2pag` again, whose `ensure_task` sees the difference and re-enables them.
  - **Undo everything, keep the PAG repository**: deletes the PDR task (`DELETE /api/tasks/{id}`) and takes back from the COS bucket what `cos2pag` added: the backup service account's ACL grant (only that exact grant; the rest of the ACL is re-sent as it was) and the PDR IP in the firewall whitelist. The PAG repository, its data and its lifecycle rules are kept.
  - **Undo everything, including the PAG repository**: the same, plus deletion of the PAG repository (`DELETE /api/partitions/{partition}/repositories/{repository}`), so the archived copies are lost. **Irreversible.** It needs the tenant, which names the partition and is never guessed.

  Every level goes through **Simulate**, which only reads (the same steps as *To do* / *Nothing to do*, nothing sent), and **Run** is unlocked only after a successful simulation for the same level and tenant. The two destructive levels also ask you to type the bucket name (checked on the server too); purge warns first that it is irreversible. Steps run in order and **stop at the first failure**: the failing one shows as *Failed* with the API's answer and the others as *Not run*; run it again and only what is left is done, since every step reads the current state first.

  What is deliberately left alone: the PAG **partition** (shared by the whole tenant) and its persistent buffer; the COS **notifications topic** (its value before `cos2pag` is unknown, so it cannot be restored); and the firewall whitelist is **never emptied**: if the PDR IP is the only entry, it is left (an empty list could open the bucket to every IP) and the step says so. If the backup account's grant existed before `cos2pag` ran, undoing removes it as well.

  The deletion and suspension calls are not made by `cos2pag`, so they are assumptions to check against the products' API guides: `DELETE /api/tasks/{id}` (PDR, confirmed by you), `PATCH /api/tasks/{id}` with `{"schedule": {"enabled": false}, "notifications": {"enabled": false}}` (PDR) and the repository `DELETE` (PAG). If a product answers otherwise, the step shows the HTTP status and body. Try the levels on a test bucket first, starting with the simulation.
- **Create**: create one storage account (`PUT /accounts/<id>`) or one bucket (`PUT /container/<name>`) from typed-in values: account, exact bucket name, quota in GB, storage location, allowed IPs (one per line or comma-separated, CIDR accepted) and an optional `x-Account-Meta-name` value. Every value is validated server-side and shown in a confirmation dialog before the call is sent.
- **Create** (bottom of the tab, **Bulk creation**): create many accounts and buckets from a JSON file that you load (or paste) in the page. **Preview** lists exactly what will be sent and flags every problem in the file; a file with errors cannot be run. **Run the plan** asks you to type the number of operations, then shows one result per operation. Optionally fetch or generate credentials for the accounts of the file.

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
A bucket needs `account`, `name`, `quota_gb`, `storage_location` and `allowed_ips`, either of its own or from `defaults`. Accounts referenced by a bucket are not created unless they are listed under `accounts`. If you have a `config.json` in the `hive_management.py` format, **Generate from config.json** turns the ticked environments into a bulk file that you can then edit.

Credentials (access/secret keys) are shown in the page, secret masked until revealed, with copy buttons, and are never written to disk by the UI (unlike `create_creds`/`get_creds` on the command line). Errors reported by the API (bad credentials, unreachable host, SSL) are shown under "API call details". Write requests are only accepted as same-origin JSON `POST`s.

**Bucket view / edit / delete:** these, and the exact-name bucket search, use `GET`, `PATCH` and `DELETE` on `/container/<name>`. Editing sends only what changed, for instance `PATCH /container/<name>` with `{"firewall": {"allowed_ip": ["10.0.0.1", "10.0.0.2"]}}` for the IPs alone, or `{"hard_quota": <bytes>}` for the quota alone. Note that `PUT` on that URL only creates: on an existing bucket it answers `409 BucketAlreadyOwnedByYou`. The HTTP status and body of every call are shown in the page.

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
1. Add new functions to `cosmanagement/hive_management.py`.
2. Update the `config.json` file with any new configurations.
3. Add new actions to the `argparse` section in the `main` function.
