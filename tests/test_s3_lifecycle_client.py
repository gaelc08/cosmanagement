from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from cos2pag.s3_lifecycle_client import build_lifecycle_rules, ensure_bucket_lifecycle, get_bucket_lifecycle

BUCKET = "my-bucket"


def test_build_lifecycle_rules_empty_config_produces_no_rules():
    assert build_lifecycle_rules({}) == []


def test_build_lifecycle_rules_abort_incomplete_multipart_upload():
    rules = build_lifecycle_rules({"abort_incomplete_multipart_upload_days": 7})
    assert rules == [
        {
            "ID": "abort-incomplete-multipart-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }
    ]


def test_build_lifecycle_rules_current_version_expiration():
    rules = build_lifecycle_rules({"current_version_expiration_days": 90})
    assert rules == [
        {
            "ID": "expire-current-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"Days": 90},
        }
    ]


def test_build_lifecycle_rules_delete_expired_delete_markers():
    rules = build_lifecycle_rules({"delete_expired_delete_markers": True})
    assert rules == [
        {
            "ID": "delete-expired-delete-markers",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        }
    ]


def test_build_lifecycle_rules_delete_expired_delete_markers_omitted_when_falsy():
    assert build_lifecycle_rules({"delete_expired_delete_markers": False}) == []


def test_build_lifecycle_rules_noncurrent_version_expiration_with_newer_versions():
    rules = build_lifecycle_rules({"noncurrent_version_expiration_days": 30, "newer_noncurrent_versions": 2})
    assert rules == [
        {
            "ID": "expire-noncurrent-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30, "NewerNoncurrentVersions": 2},
        }
    ]


def test_build_lifecycle_rules_noncurrent_version_expiration_without_newer_versions():
    rules = build_lifecycle_rules({"noncurrent_version_expiration_days": 30})
    assert rules[0]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


def test_build_lifecycle_rules_current_version_transition_defaults_storage_class():
    rules = build_lifecycle_rules({"current_version_transition_days": 30})
    assert rules == [
        {
            "ID": "transition-current-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Transitions": [{"Days": 30, "StorageClass": "DEEP_ARCHIVE"}],
        }
    ]


def test_build_lifecycle_rules_noncurrent_version_transition_custom_storage_class():
    rules = build_lifecycle_rules({"noncurrent_version_transition_days": 60, "transition_storage_class": "GLACIER"})
    assert rules == [
        {
            "ID": "transition-noncurrent-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionTransitions": [{"NoncurrentDays": 60, "StorageClass": "GLACIER"}],
        }
    ]


def test_build_lifecycle_rules_combines_multiple_rules():
    rules = build_lifecycle_rules(
        {
            "abort_incomplete_multipart_upload_days": 7,
            "delete_expired_delete_markers": True,
            "noncurrent_version_expiration_days": 30,
        }
    )
    ids = {rule["ID"] for rule in rules}
    assert ids == {"abort-incomplete-multipart-uploads", "delete-expired-delete-markers", "expire-noncurrent-versions"}


def test_get_bucket_lifecycle_returns_rules():
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.return_value = {"Rules": [{"ID": "x"}]}

    assert get_bucket_lifecycle(s3_client, BUCKET) == [{"ID": "x"}]


def test_get_bucket_lifecycle_returns_empty_list_on_no_such_configuration():
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration"
    )

    assert get_bucket_lifecycle(s3_client, BUCKET) == []


def test_get_bucket_lifecycle_reraises_other_client_errors():
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "GetBucketLifecycleConfiguration"
    )

    try:
        get_bucket_lifecycle(s3_client, BUCKET)
        assert False, "expected ClientError to propagate"
    except ClientError:
        pass


def test_ensure_bucket_lifecycle_creates_when_absent():
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration"
    )
    desired = [{"ID": "expire-current-versions", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 90}}]

    rules, action = ensure_bucket_lifecycle(s3_client, BUCKET, desired)

    assert action == "updated"
    s3_client.put_bucket_lifecycle_configuration.assert_called_once_with(
        Bucket=BUCKET, LifecycleConfiguration={"Rules": desired}
    )


def test_ensure_bucket_lifecycle_unchanged_when_matching():
    existing = [{"ID": "expire-current-versions", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 90}}]
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.return_value = {"Rules": existing}
    desired = [{"ID": "expire-current-versions", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 90}}]

    rules, action = ensure_bucket_lifecycle(s3_client, BUCKET, desired)

    assert action == "unchanged"
    s3_client.put_bucket_lifecycle_configuration.assert_not_called()


def test_ensure_bucket_lifecycle_updates_when_differs():
    existing = [{"ID": "expire-current-versions", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 30}}]
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.return_value = {"Rules": existing}
    desired = [{"ID": "expire-current-versions", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 90}}]

    rules, action = ensure_bucket_lifecycle(s3_client, BUCKET, desired)

    assert action == "updated"
    s3_client.put_bucket_lifecycle_configuration.assert_called_once_with(
        Bucket=BUCKET, LifecycleConfiguration={"Rules": desired}
    )


def test_ensure_bucket_lifecycle_dry_run_skips_put():
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration"
    )
    desired = [{"ID": "expire-current-versions", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 90}}]

    rules, action = ensure_bucket_lifecycle(s3_client, BUCKET, desired, dry_run=True)

    assert action == "updated"
    assert rules == desired
    s3_client.put_bucket_lifecycle_configuration.assert_not_called()


def test_ensure_bucket_lifecycle_unchanged_when_server_omits_empty_prefix_filter():
    # Real-world server behavior (confirmed against a live PAG S3 endpoint):
    # GetBucketLifecycleConfiguration omits "Filter" entirely for a rule
    # that was PUT with Filter: {"Prefix": ""} -- comparing byte-for-byte
    # would report "updated" on every single run, forever, even though
    # nothing changed.
    existing = [
        {
            "ID": "abort-incomplete-multipart-uploads",
            "Status": "Enabled",
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        },
        {
            "ID": "delete-expired-delete-markers",
            "Status": "Enabled",
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        },
        {
            "ID": "expire-noncurrent-versions",
            "Status": "Enabled",
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        },
    ]
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.return_value = {"Rules": existing}
    desired = [
        {
            "ID": "abort-incomplete-multipart-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        },
        {
            "ID": "delete-expired-delete-markers",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        },
        {
            "ID": "expire-noncurrent-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        },
    ]

    rules, action = ensure_bucket_lifecycle(s3_client, BUCKET, desired)

    assert action == "unchanged"
    s3_client.put_bucket_lifecycle_configuration.assert_not_called()


def test_ensure_bucket_lifecycle_still_detects_real_changes_despite_filter_normalization():
    existing = [
        {"ID": "expire-noncurrent-versions", "Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}
    ]
    s3_client = MagicMock()
    s3_client.get_bucket_lifecycle_configuration.return_value = {"Rules": existing}
    desired = [
        {
            "ID": "expire-noncurrent-versions",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 90},
        }
    ]

    rules, action = ensure_bucket_lifecycle(s3_client, BUCKET, desired)

    assert action == "updated"
    s3_client.put_bucket_lifecycle_configuration.assert_called_once_with(
        Bucket=BUCKET, LifecycleConfiguration={"Rules": desired}
    )
