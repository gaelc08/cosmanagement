"""Sets a bucket's S3 Lifecycle Configuration directly on PAG's own S3
endpoint (not the Administration REST API).

PAG's "Lifecycle Rules" GUI tab (Delete Incomplete Multipart Uploads,
Expiration of Current/Noncurrent Object Versions, Delete Expired Delete
Markers, Transition of Current/Noncurrent Object Versions to Tape) is a
wrapper around this same standard S3 API (confirmed against the GUI's own
network traffic and rule-type names, e.g. ``PAG.ApiWrapper.ExpirationRule``,
``NonCurExpirationRule``, ``ExpiredDeleteMarkersRule``, ``TransitionRule``).
Using the real S3 API (``PUT/GET .../?lifecycle``) instead of the GUI's
internal, session/CSRF-based ``/Storage/ChangeRules`` form endpoint keeps
this on a documented, stable interface.
"""
from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def build_s3_client(s3_cfg: dict[str, Any]):
    return boto3.client(
        "s3",
        endpoint_url=s3_cfg["server_url"],
        aws_access_key_id=s3_cfg.get("access_key"),
        aws_secret_access_key=s3_cfg.get("secret_key"),
        verify=s3_cfg.get("verify_ssl", True),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if s3_cfg.get("force_path_style", True) else "auto"},
        ),
    )


def build_lifecycle_rules(lifecycle_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate cos2pag's friendly config keys into S3 LifecycleConfiguration
    Rules, one-for-one with PAG's "Lifecycle Rules" GUI:

    - ``abort_incomplete_multipart_upload_days`` -> Delete Incomplete
      Multipart Uploads
    - ``current_version_expiration_days``        -> Expiration of Current
      Object Versions
    - ``delete_expired_delete_markers``           -> Delete Expired Delete
      Markers
    - ``noncurrent_version_expiration_days`` /
      ``newer_noncurrent_versions``                -> Expiration of
      Noncurrent Object Versions
    - ``current_version_transition_days`` /
      ``transition_storage_class``                 -> Transition of
      Current Object Versions to Tape
    - ``noncurrent_version_transition_days`` /
      ``transition_storage_class``                 -> Transition of
      Noncurrent Object Versions to Tape

    Each configured knob becomes its own rule (S3 doesn't allow mixing
    ``ExpiredObjectDeleteMarker`` with ``Days``/``Date`` in one
    ``Expiration`` block), applying to every object (``Filter: {Prefix:
    ""}``). Omitted keys produce no rule at all.
    """
    rules: list[dict[str, Any]] = []

    if lifecycle_cfg.get("abort_incomplete_multipart_upload_days") is not None:
        rules.append(
            {
                "ID": "abort-incomplete-multipart-uploads",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": lifecycle_cfg["abort_incomplete_multipart_upload_days"]
                },
            }
        )

    if lifecycle_cfg.get("current_version_expiration_days") is not None:
        rules.append(
            {
                "ID": "expire-current-versions",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Expiration": {"Days": lifecycle_cfg["current_version_expiration_days"]},
            }
        )

    if lifecycle_cfg.get("delete_expired_delete_markers"):
        rules.append(
            {
                "ID": "delete-expired-delete-markers",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Expiration": {"ExpiredObjectDeleteMarker": True},
            }
        )

    if lifecycle_cfg.get("noncurrent_version_expiration_days") is not None:
        noncurrent_expiration: dict[str, Any] = {
            "NoncurrentDays": lifecycle_cfg["noncurrent_version_expiration_days"]
        }
        if lifecycle_cfg.get("newer_noncurrent_versions") is not None:
            noncurrent_expiration["NewerNoncurrentVersions"] = lifecycle_cfg["newer_noncurrent_versions"]
        rules.append(
            {
                "ID": "expire-noncurrent-versions",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "NoncurrentVersionExpiration": noncurrent_expiration,
            }
        )

    if lifecycle_cfg.get("current_version_transition_days") is not None:
        rules.append(
            {
                "ID": "transition-current-versions",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Transitions": [
                    {
                        "Days": lifecycle_cfg["current_version_transition_days"],
                        "StorageClass": lifecycle_cfg.get("transition_storage_class", "DEEP_ARCHIVE"),
                    }
                ],
            }
        )

    if lifecycle_cfg.get("noncurrent_version_transition_days") is not None:
        rules.append(
            {
                "ID": "transition-noncurrent-versions",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "NoncurrentVersionTransitions": [
                    {
                        "NoncurrentDays": lifecycle_cfg["noncurrent_version_transition_days"],
                        "StorageClass": lifecycle_cfg.get("transition_storage_class", "DEEP_ARCHIVE"),
                    }
                ],
            }
        )

    return rules


def _matches(desired: Any, existing: Any) -> bool:
    """True if every key/value present in ``desired`` is also present
    (and equal) in ``existing``, recursively. Extra keys on ``existing``
    (e.g. server-added defaults) are ignored.
    """
    if isinstance(desired, dict):
        if not isinstance(existing, dict):
            return False
        return all(_matches(v, existing.get(k)) for k, v in desired.items())
    if isinstance(desired, list):
        if not isinstance(existing, list) or len(desired) != len(existing):
            return False
        return all(_matches(d, e) for d, e in zip(desired, existing))
    return desired == existing


def _rule_matches(desired_rule: dict[str, Any], existing_rule: dict[str, Any] | None) -> bool:
    """Like ``_matches``, but treats the ``Filter`` field specially: this
    API has been observed omitting ``Filter`` entirely from
    ``GetBucketLifecycleConfiguration`` when it's an empty-prefix filter
    (``{"Prefix": ""}``, meaning "applies to every object") rather than
    echoing it back as sent or as ``{}`` -- so a byte-for-byte comparison
    would report every rule as changed, forever, even right after
    setting them. Both are treated as the same "applies to everything"
    filter here; every other field still compares exactly.
    """
    if existing_rule is None:
        return False
    desired_prefix = (desired_rule.get("Filter") or {}).get("Prefix", "")
    existing_prefix = (existing_rule.get("Filter") or {}).get("Prefix", "")
    if desired_prefix != existing_prefix:
        return False
    other_fields = {k: v for k, v in desired_rule.items() if k != "Filter"}
    return _matches(other_fields, existing_rule)


def get_bucket_lifecycle(s3_client, bucket_name: str) -> list[dict[str, Any]]:
    try:
        return s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name).get("Rules", [])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchLifecycleConfiguration":
            return []
        raise


def ensure_bucket_lifecycle(
    s3_client, bucket_name: str, desired_rules: list[dict[str, Any]], dry_run: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """Set the bucket's lifecycle configuration (PUT replaces it wholesale)
    unless every desired rule already matches an existing one with the
    same ID.

    Returns ``(rules, action)`` where action is "unchanged" or "updated".
    In dry-run mode, "updated" is reported without actually calling PUT.
    """
    existing = get_bucket_lifecycle(s3_client, bucket_name)
    existing_by_id = {rule.get("ID"): rule for rule in existing}
    if len(existing) == len(desired_rules) and all(
        _rule_matches(rule, existing_by_id.get(rule["ID"])) for rule in desired_rules
    ):
        return existing, "unchanged"

    if dry_run:
        return desired_rules, "updated"

    s3_client.put_bucket_lifecycle_configuration(Bucket=bucket_name, LifecycleConfiguration={"Rules": desired_rules})
    return desired_rules, "updated"
