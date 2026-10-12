from unittest.mock import MagicMock

from cos2pag.pag_client import PagClient

PARTITION_UUID = "part-uuid"
BUCKET = "my-bucket"
OWNER = {"uuid": "pdr-uuid", "type": "User"}


def make_client(monkeypatch, existing_repo=None):
    client = PagClient.__new__(PagClient)  # skip __init__, no real session needed
    monkeypatch.setattr(client, "find_repository", lambda partition_uuid, name: existing_repo)
    monkeypatch.setattr(client, "create_repository", MagicMock(return_value={"uuid": "new-uuid"}))
    monkeypatch.setattr(client, "update_repository", MagicMock(return_value={"uuid": "existing-uuid"}))
    return client


def test_ensure_repository_creates_when_missing(monkeypatch):
    client = make_client(monkeypatch, existing_repo=None)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER)

    assert action == "created"
    client.create_repository.assert_called_once()
    client.update_repository.assert_not_called()


def test_ensure_repository_skips_patch_when_owner_already_matches(monkeypatch):
    existing = {"uuid": "existing-uuid", "name": BUCKET, "owner": {"uuid": "pdr-uuid", "type": "User"}}
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER)

    assert action == "unchanged"
    assert repo is existing
    client.create_repository.assert_not_called()
    client.update_repository.assert_not_called()


def test_ensure_repository_patches_when_owner_differs(monkeypatch):
    existing = {"uuid": "existing-uuid", "name": BUCKET, "owner": {"uuid": "someone-else", "type": "User"}}
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER)

    assert action == "updated"
    client.update_repository.assert_called_once()
    client.create_repository.assert_not_called()


def test_ensure_repository_patches_when_sosapi_flag_differs(monkeypatch):
    existing = {
        "uuid": "existing-uuid",
        "name": BUCKET,
        "owner": {"uuid": "pdr-uuid", "type": "User"},
        "sosapiEnabled": False,
    }
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER, sosapi_enabled=True)

    assert action == "updated"
    client.update_repository.assert_called_once()


PARTITION_DEFAULTS = {
    "writeProtected": False,
    "storageClassTypes": ["Standard"],
    "devType": "Tape",
    "devCodeRate": "None",
    "devAllocStrat": "RoundRobin",
    "devAllocThreshold": 10,
    "devAllocParallelism": 1,
    "devAllocMediaPref": "Any",
    "devAllocMediaAlt": "Any",
    "devAllocSrcPrio": 0,
    "devAllocSrcLimit": ["dev-uuid-1", "dev-uuid-2"],
    "cryptLocked": False,
    "cryptMode": "Off",
    "cryptAlgo": "None",
    "pstBufCfgPresent": False,
    "optionForceHighAvailability": False,
    "optionAllowReducedRedundancy": False,
}


def make_partition_client(monkeypatch, existing_tenant=None):
    client = PagClient.__new__(PagClient)
    partitions = [existing_tenant] if existing_tenant else []
    monkeypatch.setattr(client, "list_partitions", lambda: partitions)
    monkeypatch.setattr(client, "create_partition", MagicMock(return_value={"uuid": "new-part-uuid", "name": "ctie-001"}))
    return client


def test_ensure_partition_returns_existing_when_found(monkeypatch):
    existing = {"uuid": "tenant-uuid", "name": "ctie-001"}
    client = make_partition_client(monkeypatch, existing_tenant=existing)

    partition, action = client.ensure_partition("ctie-001", PARTITION_DEFAULTS)

    assert action == "found"
    assert partition is existing
    client.create_partition.assert_not_called()


def test_ensure_partition_uses_configured_defaults_when_creating(monkeypatch):
    client = make_partition_client(monkeypatch, existing_tenant=None)

    partition, action = client.ensure_partition("ctie-001", PARTITION_DEFAULTS)

    assert action == "created"
    client.create_partition.assert_called_once()
    (body,), kwargs = client.create_partition.call_args
    assert body["name"] == "ctie-001"
    assert body["devType"] == "Tape"
    assert body["devAllocSrcLimit"] == ["dev-uuid-1", "dev-uuid-2"]
    assert body["storageClassTypes"] == ["Standard"]
    assert kwargs.get("encryption_password") is None


def test_ensure_partition_passes_encryption_password_only_for_privkey_mode(monkeypatch):
    privkey_defaults = {**PARTITION_DEFAULTS, "cryptMode": "PrivKey"}
    client = make_partition_client(monkeypatch, existing_tenant=None)

    client.ensure_partition("ctie-001", privkey_defaults, encryption_password="s3cr3t")

    (_body,), kwargs = client.create_partition.call_args
    assert kwargs.get("encryption_password") == "s3cr3t"


def test_ensure_partition_omits_encryption_password_for_non_privkey_mode(monkeypatch):
    client = make_partition_client(monkeypatch, existing_tenant=None)

    client.ensure_partition("ctie-001", PARTITION_DEFAULTS, encryption_password="s3cr3t")

    (_body,), kwargs = client.create_partition.call_args
    assert kwargs.get("encryption_password") is None


def make_pst_buf_client(monkeypatch, existing=None):
    client = PagClient.__new__(PagClient)
    monkeypatch.setattr(client, "get_persistent_buffer", lambda partition_uuid: existing)
    monkeypatch.setattr(client, "set_persistent_buffer", MagicMock(return_value={"bufPath": "/new"}))
    return client


def test_ensure_persistent_buffer_creates_when_absent(monkeypatch):
    client = make_pst_buf_client(monkeypatch, existing=None)
    desired = {"bufPath": "/PoINT/PAG/PBUFFER", "bufEnabled": True}

    config, action = client.ensure_persistent_buffer(PARTITION_UUID, desired)

    assert action == "created"
    client.set_persistent_buffer.assert_called_once_with(PARTITION_UUID, desired)


def test_ensure_persistent_buffer_unchanged_when_matching(monkeypatch):
    existing = {"bufPath": "/PoINT/PAG/PBUFFER", "bufEnabled": True, "bufThreshold": 0}
    client = make_pst_buf_client(monkeypatch, existing=existing)
    desired = {"bufPath": "/PoINT/PAG/PBUFFER", "bufEnabled": True}

    config, action = client.ensure_persistent_buffer(PARTITION_UUID, desired)

    assert action == "unchanged"
    client.set_persistent_buffer.assert_not_called()


def test_ensure_persistent_buffer_updates_when_differs(monkeypatch):
    existing = {"bufPath": "/old/path", "bufEnabled": True}
    client = make_pst_buf_client(monkeypatch, existing=existing)
    desired = {"bufPath": "/PoINT/PAG/PBUFFER", "bufEnabled": True}

    config, action = client.ensure_persistent_buffer(PARTITION_UUID, desired)

    assert action == "updated"
    client.set_persistent_buffer.assert_called_once_with(PARTITION_UUID, desired)


def test_get_persistent_buffer_returns_none_on_404(monkeypatch):
    from cos2pag.http_client import ApiError

    client = PagClient.__new__(PagClient)
    client.session = MagicMock()
    client.timeout = 30
    client.base_url = "https://pag.example.com"

    def fake_request_json(*args, **kwargs):
        raise ApiError("GET", "https://pag.example.com/x", 404, "not found")

    monkeypatch.setattr("cos2pag.pag_client.request_json", fake_request_json)

    assert client.get_persistent_buffer(PARTITION_UUID) is None


def test_ensure_repository_patches_when_obj_ver_state_differs(monkeypatch):
    existing = {
        "uuid": "existing-uuid",
        "name": BUCKET,
        "owner": {"uuid": "pdr-uuid", "type": "User"},
        "objVerState": "Disabled",
    }
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER, obj_ver_state="Enabled")

    assert action == "updated"
    client.update_repository.assert_called_once()
    (call_partition, call_uuid, call_body) = client.update_repository.call_args.args
    assert call_body["objVerState"] == "Enabled"


def test_ensure_repository_unchanged_when_obj_ver_state_already_matches(monkeypatch):
    existing = {
        "uuid": "existing-uuid",
        "name": BUCKET,
        "owner": {"uuid": "pdr-uuid", "type": "User"},
        "objVerState": "Enabled",
    }
    client = make_client(monkeypatch, existing_repo=existing)

    repo, action = client.ensure_repository(PARTITION_UUID, BUCKET, OWNER, obj_ver_state="Enabled")

    assert action == "unchanged"
    client.update_repository.assert_not_called()
