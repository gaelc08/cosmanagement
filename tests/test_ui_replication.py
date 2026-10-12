import types

import pytest

from cosmanagement.ui import replication
from cosmanagement.ui.replication import job_wait_seconds, run_replication, wait_for_source_access


class Denied(Exception):
    response = {'Error': {'Code': 'AccessDenied'}, 'ResponseMetadata': {'HTTPStatusCode': 403}}


class FakeS3:
    def __init__(self, denied_times):
        self.denied_times, self.calls = denied_times, 0

    def list_objects_v2(self, **kwargs):
        self.calls += 1
        if self.calls <= self.denied_times:
            raise Denied()


def test_wait_returns_at_once_when_access_is_granted():
    assert wait_for_source_access(FakeS3(0), 'b', 60, sleep=lambda s: None) == (True, 'COS accepted reading the bucket')


def test_wait_retries_while_access_is_denied():
    sleeps = []
    ready, detail = wait_for_source_access(FakeS3(3), 'b', 60, sleep=sleeps.append)
    assert ready and sleeps == [5, 5, 5] and '15 s' in detail


def test_wait_gives_up_when_still_denied_after_the_wait():
    ready, detail = wait_for_source_access(FakeS3(99), 'b', 10, sleep=lambda s: None)
    assert not ready and 'AccessDenied' in detail


def test_other_errors_do_not_block_the_job():
    class Broken:
        def list_objects_v2(self, **kwargs):
            raise OSError('tls')

    ready, detail = wait_for_source_access(Broken(), 'b', 10, sleep=lambda s: None)
    assert ready and 'not verified' in detail


def test_job_wait_seconds_default_and_override():
    assert job_wait_seconds({}) == 60
    assert job_wait_seconds({'cos2pag': {'job_wait_seconds': 0}}) == 0
    assert job_wait_seconds({'cos2pag': {'job_wait_seconds': -1}}) == 60


class Step:
    def __init__(self, name, changed, detail, skipped=False):
        self.name, self.changed, self.detail, self.skipped = name, changed, detail, skipped


@pytest.fixture
def fake_cos2pag(monkeypatch):
    seen = {}

    class Err(Exception):
        pass

    def sync_bucket(cfg, bucket, tenant, dry_run=False, **kwargs):
        seen['auto_start_job'] = cfg['pdr']['auto_start_job']
        return types.SimpleNamespace(steps=[Step('pdr.task', True, "created replication task alias='b'")])

    monkeypatch.setattr(replication, '_cos2pag', lambda: types.SimpleNamespace(
        ConfigError=Err, ApiError=Err, load_config=lambda path: {'pdr': {'auto_start_job': True}}, sync_bucket=sync_bucket))
    monkeypatch.setattr(replication, '_check_partition', lambda cfg, tenant: None)
    return seen


CONFIG = {'cos2pag': {'config': 'config.yaml'}}


def test_cos2pag_does_not_start_the_job_itself_and_a_failed_wait_is_reported(fake_cos2pag, monkeypatch):
    def failing_start(config, cfg, bucket, steps):
        steps.append({'name': 'pdr.job', 'status': 'FAILED', 'detail': 'initial copy not started'})

    monkeypatch.setattr(replication, '_start_initial_job', failing_start)
    result = run_replication(CONFIG, 'b', 'T', dry_run=False)
    assert fake_cos2pag['auto_start_job'] is False
    assert not result['ok'] and 'initial copy did not start' in result['message']


def test_a_simulation_leaves_cos2pag_alone(fake_cos2pag):
    result = run_replication(CONFIG, 'b', 'T', dry_run=True)
    assert fake_cos2pag['auto_start_job'] is True and result['ok']
