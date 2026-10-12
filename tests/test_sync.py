from unittest.mock import MagicMock

import pytest

from cos2pag import sync as sync_module
from cos2pag.config import ConfigError
from cos2pag.http_client import ApiError
from cos2pag.sync import (
    _build_persistent_buffer_body,
    _duration_to_seconds,
    _size_to_bytes,
    _weekend_spread_schedule,
    sync_bucket,
)

BUCKET = "my-bucket"
TENANT = "MY-TENANT"


def test_duration_to_seconds_converts_units():
    assert _duration_to_seconds({"value": 6, "unit": "hours"}) == 21600
    assert _duration_to_seconds({"value": 1, "unit": "days"}) == 86400
    assert _duration_to_seconds({"value": 30, "unit": "minutes"}) == 1800
    assert _duration_to_seconds({"value": 10, "unit": "seconds"}) == 10


def test_duration_to_seconds_rejects_unknown_unit():
    with pytest.raises(Exception):
        _duration_to_seconds({"value": 1, "unit": "fortnights"})


def test_size_to_bytes_converts_units():
    assert _size_to_bytes({"value": 100, "unit": "gb"}) == 100 * 1024**3
    assert _size_to_bytes({"value": 1, "unit": "tb"}) == 1024**4
    assert _size_to_bytes({"value": 0, "unit": "byte"}) == 0


def test_build_persistent_buffer_body_maps_fields():
    body = _build_persistent_buffer_body(
        {
            "path": "/PoINT/PAG/PBUFFER",
            "enabled": True,
            "flush_gradually": False,
            "trigger_on_max_age": {"value": 6, "unit": "hours"},
            "trigger_on_max_size": {"value": 100, "unit": "gb"},
            "object_size_limit": {"value": 0, "unit": "byte"},
        }
    )
    assert body == {
        "bufPath": "/PoINT/PAG/PBUFFER",
        "bufEnabled": True,
        "bufFlushGradually": False,
        "bufFlushOnMaxAge": 21600,
        "bufFlushOnMaxSize": 100 * 1024**3,
        "bufThreshold": 0,
    }


def test_weekend_spread_schedule_is_deterministic_for_same_bucket():
    spread_cfg = {"days": ["Saturday", "Sunday"], "start_hour": 0, "interval_hours": 2}

    first = _weekend_spread_schedule("my-bucket", spread_cfg)
    second = _weekend_spread_schedule("my-bucket", spread_cfg)

    assert first == second


def test_weekend_spread_schedule_returns_valid_dow_mask_and_hour():
    spread_cfg = {"days": ["Saturday", "Sunday"], "start_hour": 0, "interval_hours": 2}

    dow_mask, hour = _weekend_spread_schedule("my-bucket", spread_cfg)

    assert dow_mask in (64, 1)  # Saturday=64, Sunday=1
    assert hour in range(0, 24, 2)


def test_weekend_spread_schedule_spreads_different_buckets_across_slots():
    spread_cfg = {"days": ["Saturday", "Sunday"], "start_hour": 0, "interval_hours": 2}

    slots = {_weekend_spread_schedule(f"bucket-{i}", spread_cfg) for i in range(24)}

    # 24 distinctly-named buckets across 24 slots should not all collapse
    # onto a single slot (proves the hash actually varies with input).
    assert len(slots) > 1


def test_weekend_spread_schedule_defaults_to_saturday_sunday_every_2_hours():
    dow_mask, hour = _weekend_spread_schedule("my-bucket", {})

    assert dow_mask in (64, 1)
    assert hour in range(0, 24, 2)


def test_weekend_spread_schedule_rejects_non_positive_interval():
    with pytest.raises(ConfigError):
        _weekend_spread_schedule("my-bucket", {"interval_hours": 0})


def test_weekend_spread_schedule_rejects_unknown_day():
    with pytest.raises(ConfigError):
        _weekend_spread_schedule("my-bucket", {"days": ["Someday"]})


def test_build_persistent_buffer_body_only_path_required():
    assert _build_persistent_buffer_body({"path": "/x"}) == {"bufPath": "/x"}


def base_config():
    return {
        "cos": {
            "base_url": "https://cos.example.com:8338",
            "auth": {"type": "bearer", "token": "t"},
            "backup_service_account": {"grantee": "backup-sa", "permission": "READ"},
            "pdr_ip": "203.0.113.10/32",
        },
        "pag": {
            "base_url": "https://pag.example.com",
            "auth": {"type": "basic", "username": "u", "password": "p"},
            "new_partition_defaults": {"devType": "Tape", "storageClassTypes": ["Standard"]},
            "owner": {"uuid": "pdr-user-uuid", "type": "User"},
        },
        "pdr": {
            "base_url": "https://pdr.example.com",
            "auth": {"type": "basic", "username": "u", "password": "p"},
            "source_s3": {"server_url": "https://cos.example.com:8338", "access_key": "ak", "secret_key": "sk"},
            "target_s3": {"server_url": "https://pag.example.com:8443", "access_key": "ak2", "secret_key": "sk2"},
        },
    }


@pytest.fixture
def fake_clients(monkeypatch):
    cos_client = MagicMock()
    cos_client.get_bucket.return_value = {
        "acl": {"existing-user": ["READ"]},
        "firewall": {"allowed_ip": ["192.168.1.0/24"]},
        "notifications": {},
        "time_updated": "2024-01-01T00:00:00Z",
    }

    pag_client = MagicMock()
    pag_client.ensure_partition.return_value = ({"uuid": "part-uuid", "name": TENANT}, "found")
    pag_client.ensure_repository.return_value = ({"uuid": "repo-uuid", "name": BUCKET}, "created")

    pdr_client = MagicMock()
    pdr_client.ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "created")

    monkeypatch.setattr(sync_module, "_build_cos_client", lambda cfg, dry_run: cos_client)
    monkeypatch.setattr(sync_module, "_build_pag_client", lambda cfg, dry_run: pag_client)
    monkeypatch.setattr(sync_module, "_build_pdr_client", lambda cfg, dry_run: pdr_client)

    return {"cos": cos_client, "pag": pag_client, "pdr": pdr_client}


def test_sync_bucket_full_happy_path(fake_clients):
    report = sync_bucket(base_config(), BUCKET, TENANT)

    cos_client = fake_clients["cos"]
    patch_calls = cos_client.patch_bucket.call_args_list
    assert len(patch_calls) == 2

    acl_firewall_call = patch_calls[0]
    body = acl_firewall_call.args[1]
    assert {"grantee": "backup-sa", "permission": "READ"} in body["acl"]
    assert {"grantee": "existing-user", "permission": "READ"} in body["acl"]
    assert body["firewall"]["allowed_ip"] == ["192.168.1.0/24", "203.0.113.10/32"]

    notif_call = patch_calls[1]
    assert notif_call.args[1] == {"notifications": {"topic": BUCKET}}

    pag_client = fake_clients["pag"]
    pag_client.ensure_partition.assert_called_once()
    partition_args, _ = pag_client.ensure_partition.call_args
    assert partition_args[0] == TENANT

    pag_client.ensure_repository.assert_called_once()
    args, kwargs = pag_client.ensure_repository.call_args
    assert args[0] == "part-uuid"
    assert args[1] == BUCKET
    assert args[2] == {"uuid": "pdr-user-uuid", "type": "User"}

    pdr_client = fake_clients["pdr"]
    pdr_client.ensure_task.assert_called_once()
    (task_body,) = pdr_client.ensure_task.call_args.args
    assert task_body["alias"] == BUCKET
    assert task_body["source"]["s3"]["bucketName"] == BUCKET
    assert task_body["source"]["s3"]["serverURL"] == "https://cos.example.com:8338"
    assert task_body["target"]["s3"]["bucketName"] == BUCKET
    assert task_body["target"]["s3"]["serverURL"] == "https://pag.example.com:8443"

    step_names = [s.name for s in report.steps]
    assert "cos.acl_and_firewall" in step_names
    assert "cos.notifications" in step_names
    assert "pag.partition" in step_names
    assert "pag.repository" in step_names
    assert "pdr.task" in step_names


def test_sync_bucket_skips_cos_patch_when_already_up_to_date(fake_clients):
    fake_clients["cos"].get_bucket.return_value = {
        "acl": {"backup-sa": ["READ"]},
        "firewall": {"allowed_ip": ["203.0.113.10/32"]},
        "notifications": {"topic": BUCKET},
        "time_updated": "2024-01-01T00:00:00Z",
    }

    report = sync_bucket(base_config(), BUCKET, TENANT)

    fake_clients["cos"].patch_bucket.assert_not_called()
    step = next(s for s in report.steps if s.name == "cos.acl_and_firewall")
    assert step.skipped is True


def test_sync_bucket_reuses_existing_pdr_task(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "unchanged")
    report = sync_bucket(base_config(), BUCKET, TENANT)
    step = next(s for s in report.steps if s.name == "pdr.task")
    assert step.skipped is True
    assert step.changed is False


def test_sync_bucket_reports_updated_pdr_task_as_changed(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "updated")
    report = sync_bucket(base_config(), BUCKET, TENANT)
    step = next(s for s in report.steps if s.name == "pdr.task")
    assert step.skipped is False
    assert step.changed is True


def test_sync_bucket_tolerates_notifications_rejected_by_cos(fake_clients):
    fake_clients["cos"].patch_bucket.side_effect = [
        None,
        ApiError("PATCH", "https://cos.example.com/container/x", 400, "MalformedNotificationsError"),
    ]

    report = sync_bucket(base_config(), BUCKET, TENANT)

    step = next(s for s in report.steps if s.name == "cos.notifications")
    assert step.skipped is True


def test_sync_bucket_refuses_to_create_whitelist_from_scratch(fake_clients):
    fake_clients["cos"].get_bucket.return_value = {
        "acl": {"backup-sa": ["READ"]},
        "firewall": {},
        "notifications": {"topic": BUCKET},
        "time_updated": "2024-01-01T00:00:00Z",
    }

    report = sync_bucket(base_config(), BUCKET, TENANT)

    fake_clients["cos"].patch_bucket.assert_not_called()
    step = next(s for s in report.steps if s.name == "cos.firewall")
    assert step.skipped is True


def test_sync_bucket_skips_pag_patch_when_repository_unchanged(fake_clients):
    fake_clients["pag"].ensure_repository.return_value = (
        {"uuid": "repo-uuid", "name": BUCKET},
        "unchanged",
    )

    report = sync_bucket(base_config(), BUCKET, TENANT)

    step = next(s for s in report.steps if s.name == "pag.repository")
    assert step.skipped is True
    assert step.changed is False


def test_sync_pdr_task_includes_max_error_retry_when_configured(fake_clients):
    config = base_config()
    config["pdr"]["source_s3"]["max_error_retry"] = 2
    config["pdr"]["target_s3"]["max_error_retry"] = 2

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["source"]["maxErrorRetry"] == 2
    assert task_body["target"]["maxErrorRetry"] == 2


def test_sync_pdr_task_omits_max_error_retry_when_not_configured(fake_clients):
    sync_bucket(base_config(), BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert "maxErrorRetry" not in task_body["source"]
    assert "maxErrorRetry" not in task_body["target"]


def test_sync_pdr_task_includes_kafka_notifications_defaulting_topic_to_bucket_name(fake_clients):
    config = base_config()
    config["pdr"]["notifications"] = {
        "enabled": True,
        "type": "kafka",
        "kafka": {
            "schema": "IBMCOS",
            "server_url": "thoth-1:9092,thoth-2:9092",
            "connection_type": "None",
        },
    }

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["notifications"] == {
        "enabled": True,
        "type": "kafka",
        "kafka": {
            "schema": "IBMCOS",
            "serverURL": "thoth-1:9092,thoth-2:9092",
            "connectionType": "None",
            "topic": BUCKET,
        },
    }


def test_sync_pdr_task_notifications_topic_can_be_overridden(fake_clients):
    config = base_config()
    config["pdr"]["notifications"] = {
        "enabled": True,
        "type": "kafka",
        "kafka": {"topic": "fixed-topic"},
    }

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["notifications"]["kafka"]["topic"] == "fixed-topic"


def test_sync_pdr_task_omits_notifications_when_disabled(fake_clients):
    config = base_config()
    config["pdr"]["notifications"] = {"enabled": False}

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert "notifications" not in task_body


def test_sync_pdr_task_uses_fixed_dow_mask_and_hour_when_no_weekend_spread(fake_clients):
    config = base_config()
    config["pdr"]["schedule"] = {"enabled": True, "week_multiplier": 1, "dow_mask": 64, "hour": 2}

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["schedule"] == {"enabled": True, "weekMultiplier": 1, "dowMask": 64, "hour": 2}


def test_sync_pdr_task_uses_weekend_spread_when_configured(fake_clients):
    config = base_config()
    config["pdr"]["schedule"] = {
        "enabled": True,
        "week_multiplier": 1,
        "weekend_spread": {"days": ["Saturday", "Sunday"], "start_hour": 0, "interval_hours": 2},
    }

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    schedule = task_body["schedule"]
    assert schedule["enabled"] is True
    assert schedule["weekMultiplier"] == 1
    assert schedule["dowMask"] in (64, 1)
    assert schedule["hour"] in range(0, 24, 2)


def test_sync_pdr_task_weekend_spread_gives_same_bucket_same_schedule_across_runs(fake_clients):
    config = base_config()
    config["pdr"]["schedule"] = {"enabled": True, "weekend_spread": {}}

    sync_bucket(config, BUCKET, TENANT)
    (first_task_body,) = fake_clients["pdr"].ensure_task.call_args.args

    fake_clients["pdr"].ensure_task.reset_mock()
    sync_bucket(config, BUCKET, TENANT)
    (second_task_body,) = fake_clients["pdr"].ensure_task.call_args.args

    assert first_task_body["schedule"] == second_task_body["schedule"]


def test_sync_bucket_requires_tenant_argument():
    with pytest.raises(TypeError):
        sync_bucket(base_config(), BUCKET)


def test_sync_bucket_uses_given_tenant_verbatim(fake_clients):
    sync_bucket(base_config(), "ctie-001-gael-nextcloud", "CTIE-001")

    args, kwargs = fake_clients["pag"].ensure_partition.call_args
    assert args[0] == "CTIE-001"
    assert args[1] == {"devType": "Tape", "storageClassTypes": ["Standard"]}


def test_sync_bucket_reports_partition_creation_as_changed(fake_clients):
    fake_clients["pag"].ensure_partition.return_value = (
        {"uuid": "new-part-uuid", "name": TENANT},
        "created",
    )

    report = sync_bucket(base_config(), BUCKET, TENANT)

    step = next(s for s in report.steps if s.name == "pag.partition")
    assert step.changed is True
    assert step.skipped is False


def test_sync_bucket_reports_existing_partition_as_skipped(fake_clients):
    report = sync_bucket(base_config(), BUCKET, TENANT)  # fixture default action is "found"

    step = next(s for s in report.steps if s.name == "pag.partition")
    assert step.changed is False
    assert step.skipped is True


def test_sync_bucket_handles_dry_run_partition_creation_without_uuid(fake_clients):
    fake_clients["pag"].ensure_partition.return_value = (None, "created")

    report = sync_bucket(base_config(), BUCKET, TENANT, dry_run=True)

    fake_clients["pag"].ensure_repository.assert_not_called()
    repo_step = next(s for s in report.steps if s.name == "pag.repository")
    assert repo_step.changed is True


def test_sync_bucket_skips_persistent_buffer_when_not_configured(fake_clients):
    report = sync_bucket(base_config(), BUCKET, TENANT)

    step_names = [s.name for s in report.steps]
    assert "pag.persistent_buffer" not in step_names
    fake_clients["pag"].ensure_persistent_buffer.assert_not_called()


def test_sync_bucket_configures_persistent_buffer_with_unit_conversion(fake_clients):
    config = base_config()
    config["pag"]["persistent_buffer"] = {
        "path": "/PoINT/PAG/PBUFFER",
        "enabled": True,
        "flush_gradually": False,
        "trigger_on_max_age": {"value": 6, "unit": "hours"},
        "trigger_on_max_size": {"value": 100, "unit": "gb"},
        "object_size_limit": {"value": 0, "unit": "byte"},
    }
    fake_clients["pag"].ensure_persistent_buffer.return_value = ({"bufPath": "/PoINT/PAG/PBUFFER"}, "created")

    report = sync_bucket(config, BUCKET, TENANT)

    fake_clients["pag"].ensure_persistent_buffer.assert_called_once()
    args, kwargs = fake_clients["pag"].ensure_persistent_buffer.call_args
    assert args[0] == "part-uuid"
    body = args[1]
    assert body["bufPath"] == "/PoINT/PAG/PBUFFER"
    assert body["bufEnabled"] is True
    assert body["bufFlushGradually"] is False
    assert body["bufFlushOnMaxAge"] == 6 * 3600
    assert body["bufFlushOnMaxSize"] == 100 * 1024**3
    assert body["bufThreshold"] == 0

    step = next(s for s in report.steps if s.name == "pag.persistent_buffer")
    assert step.changed is True


def test_sync_bucket_reports_unchanged_persistent_buffer_as_skipped(fake_clients):
    config = base_config()
    config["pag"]["persistent_buffer"] = {"path": "/PoINT/PAG/PBUFFER"}
    fake_clients["pag"].ensure_persistent_buffer.return_value = ({"bufPath": "/PoINT/PAG/PBUFFER"}, "unchanged")

    report = sync_bucket(config, BUCKET, TENANT)

    step = next(s for s in report.steps if s.name == "pag.persistent_buffer")
    assert step.skipped is True
    assert step.changed is False


def test_sync_bucket_skips_persistent_buffer_call_when_partition_not_yet_created(fake_clients):
    config = base_config()
    config["pag"]["persistent_buffer"] = {"path": "/PoINT/PAG/PBUFFER"}
    fake_clients["pag"].ensure_partition.return_value = (None, "created")

    report = sync_bucket(config, BUCKET, TENANT, dry_run=True)

    fake_clients["pag"].ensure_persistent_buffer.assert_not_called()
    step = next(s for s in report.steps if s.name == "pag.persistent_buffer")
    assert step.changed is True


def test_sync_pdr_task_delete_delay_days_is_always_zero(fake_clients):
    config = base_config()
    config["pdr"]["copy_options"] = {"delete_delay_days": 30}  # ignored on purpose

    sync_bucket(config, BUCKET, TENANT)

    (task_body,) = fake_clients["pdr"].ensure_task.call_args.args
    assert task_body["options"]["deleteDelayDays"] == 0


def test_sync_bucket_starts_initial_copy_job_when_task_created(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "created")

    report = sync_bucket(base_config(), BUCKET, TENANT)

    fake_clients["pdr"].start_job.assert_called_once_with(42)
    step = next(s for s in report.steps if s.name == "pdr.job")
    assert step.changed is True


def test_sync_bucket_auto_start_job_can_be_disabled(fake_clients):
    config = base_config()
    config["pdr"]["auto_start_job"] = False
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "created")

    report = sync_bucket(config, BUCKET, TENANT)

    fake_clients["pdr"].start_job.assert_not_called()
    assert not any(s.name == "pdr.job" for s in report.steps)


def test_sync_bucket_does_not_start_job_when_task_already_existed(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "unchanged")

    report = sync_bucket(base_config(), BUCKET, TENANT)

    fake_clients["pdr"].start_job.assert_not_called()
    assert not any(s.name == "pdr.job" for s in report.steps)


def test_sync_bucket_does_not_start_job_when_task_updated(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = ({"id": 42, "alias": BUCKET}, "updated")

    report = sync_bucket(base_config(), BUCKET, TENANT)

    fake_clients["pdr"].start_job.assert_not_called()
    assert not any(s.name == "pdr.job" for s in report.steps)


def test_sync_bucket_skips_job_start_in_dry_run_creation(fake_clients):
    fake_clients["pdr"].ensure_task.return_value = (None, "created")

    sync_bucket(base_config(), BUCKET, TENANT, dry_run=True)

    fake_clients["pdr"].start_job.assert_not_called()


def test_sync_bucket_passes_object_versioning_to_ensure_repository(fake_clients):
    config = base_config()
    config["pag"]["object_versioning"] = "Enabled"

    sync_bucket(config, BUCKET, TENANT)

    args, kwargs = fake_clients["pag"].ensure_repository.call_args
    assert kwargs["obj_ver_state"] == "Enabled"


def test_sync_bucket_skips_lifecycle_when_not_configured(fake_clients):
    report = sync_bucket(base_config(), BUCKET, TENANT)

    assert not any(s.name == "pag.lifecycle" for s in report.steps)


def test_sync_bucket_sets_lifecycle_rules_via_s3_api(monkeypatch, fake_clients):
    config = base_config()
    config["pag"]["lifecycle"] = {"noncurrent_version_expiration_days": 30, "delete_expired_delete_markers": True}

    s3_client = MagicMock()
    monkeypatch.setattr(sync_module, "_build_s3_client", lambda cfg: s3_client)
    monkeypatch.setattr(
        sync_module,
        "ensure_bucket_lifecycle",
        MagicMock(return_value=([{"ID": "expire-noncurrent-versions"}], "updated")),
    )

    report = sync_bucket(config, BUCKET, TENANT)

    sync_module.ensure_bucket_lifecycle.assert_called_once()
    call_args = sync_module.ensure_bucket_lifecycle.call_args.args
    assert call_args[0] is s3_client
    assert call_args[1] == BUCKET
    rule_ids = {rule["ID"] for rule in call_args[2]}
    assert rule_ids == {"expire-noncurrent-versions", "delete-expired-delete-markers"}

    step = next(s for s in report.steps if s.name == "pag.lifecycle")
    assert step.changed is True


def test_sync_bucket_reports_unchanged_lifecycle_as_skipped(monkeypatch, fake_clients):
    config = base_config()
    config["pag"]["lifecycle"] = {"noncurrent_version_expiration_days": 30}

    monkeypatch.setattr(sync_module, "_build_s3_client", lambda cfg: MagicMock())
    monkeypatch.setattr(
        sync_module, "ensure_bucket_lifecycle", MagicMock(return_value=([{"ID": "expire-noncurrent-versions"}], "unchanged"))
    )

    report = sync_bucket(config, BUCKET, TENANT)

    step = next(s for s in report.steps if s.name == "pag.lifecycle")
    assert step.skipped is True
    assert step.changed is False


def test_sync_bucket_skips_lifecycle_call_when_partition_not_yet_created(monkeypatch, fake_clients):
    config = base_config()
    config["pag"]["lifecycle"] = {"noncurrent_version_expiration_days": 30}
    fake_clients["pag"].ensure_partition.return_value = (None, "created")

    ensure_bucket_lifecycle_mock = MagicMock()
    monkeypatch.setattr(sync_module, "ensure_bucket_lifecycle", ensure_bucket_lifecycle_mock)

    report = sync_bucket(config, BUCKET, TENANT, dry_run=True)

    ensure_bucket_lifecycle_mock.assert_not_called()
    step = next(s for s in report.steps if s.name == "pag.lifecycle")
    assert step.changed is True


def test_sync_bucket_noncurrent_expiration_days_overrides_config(monkeypatch, fake_clients):
    config = base_config()
    config["pag"]["lifecycle"] = {"noncurrent_version_expiration_days": 30, "delete_expired_delete_markers": True}

    monkeypatch.setattr(sync_module, "_build_s3_client", lambda cfg: MagicMock())
    ensure_bucket_lifecycle_mock = MagicMock(return_value=([], "updated"))
    monkeypatch.setattr(sync_module, "ensure_bucket_lifecycle", ensure_bucket_lifecycle_mock)

    sync_bucket(config, BUCKET, TENANT, noncurrent_expiration_days=90)

    rules = ensure_bucket_lifecycle_mock.call_args.args[2]
    noncurrent_rule = next(r for r in rules if r["ID"] == "expire-noncurrent-versions")
    assert noncurrent_rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 90
    assert any(r["ID"] == "delete-expired-delete-markers" for r in rules)
    # the original config dict passed in must not be mutated
    assert config["pag"]["lifecycle"]["noncurrent_version_expiration_days"] == 30


def test_sync_bucket_noncurrent_expiration_days_override_works_without_existing_lifecycle_config(monkeypatch, fake_clients):
    config = base_config()

    monkeypatch.setattr(sync_module, "_build_s3_client", lambda cfg: MagicMock())
    ensure_bucket_lifecycle_mock = MagicMock(return_value=([], "updated"))
    monkeypatch.setattr(sync_module, "ensure_bucket_lifecycle", ensure_bucket_lifecycle_mock)

    report = sync_bucket(config, BUCKET, TENANT, noncurrent_expiration_days=45)

    rules = ensure_bucket_lifecycle_mock.call_args.args[2]
    assert rules[0]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 45
    assert any(s.name == "pag.lifecycle" for s in report.steps)
