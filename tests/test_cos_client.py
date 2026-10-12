from cos2pag.cos_client import _to_http_date, acl_map_to_pairs, merge_acl, merge_allowed_ip


def test_acl_map_to_pairs_expands_multiple_permissions():
    acl_map = {"user1": ["WRITE", "READ"], "user2": ["FULL_CONTROL"]}
    pairs = acl_map_to_pairs(acl_map)
    assert {"grantee": "user1", "permission": "WRITE"} in pairs
    assert {"grantee": "user1", "permission": "READ"} in pairs
    assert {"grantee": "user2", "permission": "FULL_CONTROL"} in pairs
    assert len(pairs) == 3


def test_acl_map_to_pairs_handles_none():
    assert acl_map_to_pairs(None) == []


def test_merge_acl_adds_new_grantee():
    acl_map = {"user1": ["WRITE"]}
    merged = merge_acl(acl_map, "backup-sa", "READ")
    assert {"grantee": "user1", "permission": "WRITE"} in merged
    assert {"grantee": "backup-sa", "permission": "READ"} in merged
    assert len(merged) == 2


def test_merge_acl_is_idempotent():
    acl_map = {"backup-sa": ["READ"]}
    merged = merge_acl(acl_map, "backup-sa", "READ")
    assert merged == [{"grantee": "backup-sa", "permission": "READ"}]


def test_merge_acl_adds_extra_permission_for_existing_grantee():
    acl_map = {"backup-sa": ["READ"]}
    merged = merge_acl(acl_map, "backup-sa", "WRITE")
    assert {"grantee": "backup-sa", "permission": "READ"} in merged
    assert {"grantee": "backup-sa", "permission": "WRITE"} in merged
    assert len(merged) == 2


def test_merge_allowed_ip_appends_to_existing_whitelist():
    firewall = {"allowed_ip": ["192.168.1.0/24"], "denied_ip": ["10.0.0.1/32"]}
    new_list, changed = merge_allowed_ip(firewall, "203.0.113.10/32")
    assert changed is True
    assert new_list == ["192.168.1.0/24", "203.0.113.10/32"]


def test_merge_allowed_ip_idempotent_when_already_present():
    firewall = {"allowed_ip": ["203.0.113.10/32"]}
    new_list, changed = merge_allowed_ip(firewall, "203.0.113.10/32")
    assert changed is False
    assert new_list == ["203.0.113.10/32"]


def test_merge_allowed_ip_refuses_to_create_whitelist_by_default():
    # No firewall / no allowed_ip at all => bucket is currently open.
    new_list, changed = merge_allowed_ip(None, "203.0.113.10/32")
    assert new_list is None
    assert changed is False

    new_list, changed = merge_allowed_ip({"denied_ip": ["10.0.0.1/32"]}, "203.0.113.10/32")
    assert new_list is None
    assert changed is False


def test_merge_allowed_ip_can_be_forced_to_create_whitelist():
    new_list, changed = merge_allowed_ip(None, "203.0.113.10/32", allow_create_whitelist=True)
    assert changed is True
    assert new_list == ["203.0.113.10/32"]


def test_to_http_date_converts_rfc3339_with_fractional_seconds():
    assert _to_http_date("2026-09-18T15:02:44.609Z") == "Fri, 18 Sep 2026 15:02:44 GMT"


def test_to_http_date_converts_rfc3339_without_fractional_seconds():
    assert _to_http_date("2019-04-15T08:23:42Z") == "Mon, 15 Apr 2019 08:23:42 GMT"
