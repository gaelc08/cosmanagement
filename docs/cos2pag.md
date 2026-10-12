# cos2pag

Sets up replication of a single IBM Cloud Object Storage (COS) bucket to a
PoINT Archival Gateway (PAG) repository, driven by PoINT Data Replicator
(PDR).

For a given bucket name, running the tool does:

1. **COS** (`<accesser>:8338/container/{bucket}`)
   - `GET` the bucket to read its current ACL and IP whitelist (`firewall`).
   - `PATCH` the bucket, adding the configured backup service account to
     the ACL and the PDR IP to the firewall whitelist (existing entries are
     preserved — ACL and firewall are re-sent in full, since the API
     replaces them wholesale on PATCH).
   - `PATCH` the bucket's `notifications.topic` to the bucket's own name.
2. **PAG** (`/api/partitions`, `/api/partitions/{partitionUuid}/repositories`)
   - Use the partition named by the required `--tenant` argument (one
     partition per tenant). This is never guessed from the bucket name:
     real tenant codes share common prefixes (`ME`, `ME-SR`, `ME-SRE`,
     `ME-SRE2`; `ACT`, `ACT-GEOPORTAL`, `ACT-ILDG`; ...), so deriving it
     automatically risks silently picking the wrong tenant. If that
     partition doesn't exist yet, it's created with the settings in
     `pag.new_partition_defaults` (a static config block you fill in
     once, e.g. copied from an existing reference partition like
     "costotape") — only the name differs.
   - If `pag.persistent_buffer` is configured, apply it to that partition
     (`/api/partitions/{uuid}/pst_buffer_config`) — the "Persistent
     Buffer" settings in the PAG GUI (path, flush mode, age/size
     triggers, object size limit). Applied whether the partition was just
     created or already existed; skipped entirely if not configured.
   - Create an object repository with the same name as the COS bucket in
     that partition, owned by the configured PDR user (or reuse it and
     update its owner if it already exists), enabling object versioning
     (`pag.object_versioning`) if configured.
   - If `pag.lifecycle` is configured, set the bucket's S3 Lifecycle
     Configuration on PAG's own S3 endpoint (`PUT .../?lifecycle`, via
     `boto3`, reusing `pdr.target_s3`'s credentials). This is the same
     mechanism behind the "Rules" tab in the PAG GUI (Delete Incomplete
     Multipart Uploads, Expiration of Current/Noncurrent Object Versions,
     Delete Expired Delete Markers, Transition of Current/Noncurrent
     Object Versions to Tape) — that GUI tab isn't part of the documented
     Administration REST API (it posts to an internal, session/CSRF-based
     `/Storage/ChangeRules` endpoint), so this goes through the standard
     S3 Bucket Lifecycle API instead. Omit `pag.lifecycle` to leave the
     repository's lifecycle unmanaged.
3. **PDR** (`/api/tasks`, `/api/tasks/{id}/jobs`)
   - Create a replication task from the COS bucket (S3 source) to the PAG
     repository (S3 target), including copy/deletion options
     (`pdr.copy_options`), a schedule (`pdr.schedule`), and Kafka/SQS
     object-change notifications for the task itself (`pdr.notifications`,
     separate from `cos.notifications`) if configured. The Kafka topic
     defaults to the bucket's own name, same convention as COS. Deletion
     Delay is always 0 (deletions mirror immediately) — object
     retention/expiry is governed entirely by PAG's S3 lifecycle
     configuration (`pag.lifecycle`) instead.
   - When the task is newly created, immediately start an initial `Copy`
     job on it (`pdr.auto_start_job`, default `true`). Notifications/
     schedule only pick up *future* object changes, so without this,
     anything already written to the bucket before replication was set
     up would never get copied. Set `pdr.auto_start_job: false` to opt out.

Every step is idempotent: re-running the tool on a bucket that is already
fully set up is a no-op (each step is reported as `SKIPPED`/`OK`).

## Spreading the weekly copy schedule (`pdr.schedule.weekend_spread`)

If every bucket's task uses the same `dow_mask`/`hour`, every bucket's
weekly consolidation copy starts at the exact same moment — undesirable
once you have more than a handful of buckets. Set
`pdr.schedule.weekend_spread` instead of `dow_mask`/`hour` directly to
spread jobs across an evenly-spaced grid of slots (by default: every 2
hours across Saturday and Sunday). Each bucket's slot is derived from a
stable hash of its own name, not an incrementing counter or a state
file: the same bucket always lands on the same slot (safe to re-run
without moving an existing task's schedule), while different buckets
land on different slots across the grid. PDR only supports one hour per
task (applied to whichever days are selected), so each slot is a single
day + hour, not a set of days.

## Note on `sosapi_enabled`

`sosapiEnabled` on a PAG repository does **not** gate plain S3 access —
the repository is reachable over standard S3 (what PDR uses) regardless
of this flag. "SOSAPI" here is a Veeam-specific protocol extension on top
of S3 (for Veeam Backup & Replication's object storage integration),
gated by a separate "Veeam SOSAPI" license option. This tool defaults
`pag.sosapi_enabled` to `false` since it isn't needed for PDR replication;
only set it `true` if you specifically need that Veeam extension and have
it licensed (otherwise it fails with a 500 "product license does not
cover this function" / `ModifyObjectRepository`).

## Install

```
pip install .                     # installs a `cos2pag` command on PATH
# or, to run from the checkout without installing:
pip install -r requirements.txt   # then use `python -m cos2pag`
```

## Configure

```
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml` for anything environment-specific (endpoints,
partition, owner, etc. — see below), and edit `.env` for secrets (never
committed, listed in `.gitignore`). `config.yaml`'s `${VAR_NAME}`
placeholders are resolved from the environment at load time, and
`python -m cos2pag` loads `.env` into the environment automatically
before that happens — no need to `export` anything by hand each time.
Use `--env-file <path>` if you keep secrets somewhere other than `.env`.

`config.example.yaml` is pre-filled with this environment's known
endpoints:

| System | Admin/Service API endpoint | S3 data-plane endpoint | Auth |
| --- | --- | --- | --- |
| COS | `osiris-109-a-fe.ctie.etat.lu:8338` | `s3.govcloud.etat.lu:443` | basic |
| PAG | `scrat-1.ctie.etat.lu:4200` | `scrat-1.ctie.etat.lu:4443` | basic |
| PDR | `hathor-1.ctie.etat.lu:3601` | n/a | basic |

The admin/service endpoints are used for bucket ACL/firewall/notifications
(COS) and repository/task management (PAG/PDR). The S3 data-plane
endpoints are what PDR actually connects to as `source_s3`/`target_s3` to
copy objects — they run on different ports than the admin APIs, so don't
mix them up.

## Run

```
python -m cos2pag my-bucket-name --tenant CTIE-001 --config config.yaml
```

`--tenant` is required (the PAG partition/tenant name) — it's never
guessed from the bucket name, since real tenant codes are ambiguous
prefixes of each other (see above). If that partition doesn't exist yet,
this run creates it.

Add `--dry-run` to see what would be sent without making any mutating
request (`GET`s are still executed so the plan reflects real current
state), `-v` for debug logging, and `--noncurrent-expiration-days <N>` to
override `pag.lifecycle.noncurrent_version_expiration_days` for this run
only (the rest of `pag.lifecycle` is unaffected).

## Important safety note on the IP whitelist

If a bucket currently has **no** `allowed_ip` list at all, it is reachable
from any IP not explicitly denied. Sending a PATCH with
`firewall.allowed_ip: [<pdr ip>]` in that situation does not "add" an
entry — it **creates** a whitelist and immediately locks the bucket down
to that single IP. To avoid silently breaking existing access, the tool
refuses to do this by default and reports the `cos.firewall` step as
skipped with a warning. Set `cos.allow_create_whitelist: true` in the
config if that lock-down is actually what you want.

## Notes on API references used

- COS: *IBM Cloud Object Storage System — Container Mode Service API Guide
  — Bucket Management* (v3.20.x). Confirmed to use HTTP basic auth in this
  environment; the tool also supports `bearer` auth if that ever changes
  (set `cos.auth.type`).
- PAG: `pag-swagger.json` (PoINT Archival Gateway Administration API v1).
  A "bucket" is an Object Repository; the owner assigned to it is set via
  the `owner` field (`uuid` + `type`: `User`/`Group`/`ESP`).
- PDR: `pdr-swagger.json` (PoINT Data Replicator Administration API V1).
  A replication task's `source`/`target` are configured as S3 endpoints
  (`ApiTaskOptions.s3`).

## Tests

```
pip install -e '.[dev]'
ruff check .
pytest
```

CI (`.github/workflows/ci.yml`) runs the same on every push and pull
request.
