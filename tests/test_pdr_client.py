from unittest.mock import MagicMock

from cos2pag.pdr_client import PdrClient

ALIAS = "my-bucket"


def make_client(monkeypatch, existing_task=None):
    client = PdrClient.__new__(PdrClient)  # skip __init__, no real session needed
    monkeypatch.setattr(client, "find_task_by_alias", lambda alias: existing_task)
    monkeypatch.setattr(client, "create_task", MagicMock(return_value={"id": 99, "alias": ALIAS}))
    monkeypatch.setattr(client, "update_task", MagicMock(return_value={"id": 1, "alias": ALIAS}))
    return client


def test_ensure_task_creates_when_missing(monkeypatch):
    client = make_client(monkeypatch, existing_task=None)
    body = {"alias": ALIAS, "options": {"acl": False}}

    task, action = client.ensure_task(body)

    assert action == "created"
    client.create_task.assert_called_once_with(body)
    client.update_task.assert_not_called()


def test_ensure_task_unchanged_when_options_schedule_notifications_already_match(monkeypatch):
    existing = {
        "id": 1,
        "alias": ALIAS,
        "options": {"acl": False, "tags": True},
        "schedule": {"enabled": True, "hour": 2},
        "notifications": {"enabled": True, "type": "kafka", "kafka": {"topic": ALIAS}},
        "source": {"s3": {"secretKey": None}},  # deliberately different/masked, must be ignored
    }
    client = make_client(monkeypatch, existing_task=existing)
    body = {
        "alias": ALIAS,
        "source": {"s3": {"secretKey": "real-secret"}},
        "options": {"acl": False, "tags": True},
        "schedule": {"enabled": True, "hour": 2},
        "notifications": {"enabled": True, "type": "kafka", "kafka": {"topic": ALIAS}},
    }

    task, action = client.ensure_task(body)

    assert action == "unchanged"
    assert task is existing
    client.create_task.assert_not_called()
    client.update_task.assert_not_called()


def test_ensure_task_updates_when_notifications_missing_on_existing_task(monkeypatch):
    existing = {
        "id": 11,
        "alias": ALIAS,
        "options": {"acl": False},
        "schedule": {"enabled": False},
        # no notifications at all on the existing task
    }
    client = make_client(monkeypatch, existing_task=existing)
    body = {
        "alias": ALIAS,
        "options": {"acl": False},
        "schedule": {"enabled": False},
        "notifications": {"enabled": True, "type": "kafka", "kafka": {"topic": ALIAS}},
    }

    task, action = client.ensure_task(body)

    assert action == "updated"
    client.update_task.assert_called_once_with(11, body)
    client.create_task.assert_not_called()
