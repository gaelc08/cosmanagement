import pytest

from cosmanagement.ui import lifecycle
from cosmanagement.ui.lifecycle import (
    RULE_EXPIRE, RULE_NONCURRENT, current_values, describe_rule, handle_lifecycle_get, handle_lifecycle_update,
    managed_rules, merge_rules, parse_lifecycle_request, s3_settings,
)

OTHER = {'ID': 'keep-me', 'Status': 'Enabled', 'Filter': {'Prefix': 'logs/'}, 'Expiration': {'Days': 7}}


def params(**kwargs):
    base = {'expire_days': None, 'noncurrent_days': None, 'abort_days': None, 'prefix': ''}
    base.update(kwargs)
    return base


def test_parse_request():
    assert parse_lifecycle_request({'expire_days': ' 30 ', 'noncurrent_days': '', 'prefix': 'a/'})[0] == params(expire_days=30, prefix='a/')
    for bad in ({'expire_days': 'x'}, {'expire_days': '0'}, {'abort_days': '99999'}, {'prefix': 'a\nb'}):
        assert parse_lifecycle_request(bad)[1]


def test_managed_rules_only_for_filled_values():
    assert managed_rules(params()) == []
    rules = managed_rules(params(expire_days=30, noncurrent_days=10, prefix='data/'))
    assert [r['ID'] for r in rules] == [RULE_EXPIRE, RULE_NONCURRENT]
    assert rules[0]['Expiration'] == {'Days': 30} and rules[0]['Filter'] == {'Prefix': 'data/'}


def test_merge_keeps_the_other_rules_and_replaces_ours():
    existing = [OTHER, *managed_rules(params(expire_days=90))]
    merged = merge_rules(existing, params(expire_days=30))
    assert merged[0] == OTHER and merged[1]['Expiration'] == {'Days': 30} and len(merged) == 2
    assert merge_rules(existing, params()) == [OTHER]          # emptying the form removes only our rule


def test_current_values_round_trip():
    assert current_values([OTHER, *managed_rules(params(expire_days=30, abort_days=2, prefix='x/'))]) == {
        'expire_days': 30, 'noncurrent_days': None, 'abort_days': 2, 'prefix': 'x/'}
    assert current_values([]) == params()


def test_describe_rule():
    assert describe_rule(OTHER) == "expire objects 7 days after creation (objects under 'logs/')"
    assert describe_rule({'ID': 'x', 'Status': 'Enabled'}).startswith('no known action')


def test_s3_settings(tmp_path):
    api = {'api': {'base_url': 'https://x', 'ssl_verify': True, 'ca_bundle_path': 'ca.pem'}}
    assert s3_settings(api) is None
    assert s3_settings({**api, 's3': {'endpoint_url': 'https://s3.example.lu'}}) == {'server_url': 'https://s3.example.lu', 'verify_ssl': 'ca.pem'}
    assert s3_settings({**api, 's3': {'endpoint_url': 'https://s3', 'ssl_verify': False}})['verify_ssl'] is False
    assert s3_settings({**api, 's3': {'endpoint_url': 'ftp://s3'}}) is None


class FakeS3:
    def __init__(self, rules):
        self.rules, self.puts, self.deletes = rules, [], 0

    def get_bucket_lifecycle_configuration(self, Bucket):
        return {'Rules': self.rules}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self.puts.append(LifecycleConfiguration['Rules'])
        self.rules = LifecycleConfiguration['Rules']

    def delete_bucket_lifecycle(self, Bucket):
        self.deletes += 1
        self.rules = []


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3([OTHER])
    monkeypatch.setattr(lifecycle, '_client', lambda config, account: (fake, None))
    return fake


BODY = {'account': 'sa-fina-0001', 'bucket': 'fina-docs', 'expire_days': '30'}


def test_get_returns_the_values_and_the_other_rules(s3):
    status, out = handle_lifecycle_get({}, BODY)
    assert status == 200 and out['ok'] and out['values']['expire_days'] is None
    assert [r['id'] for r in out['others']] == ['keep-me']


def test_preview_sends_nothing(s3):
    status, out = handle_lifecycle_update({}, BODY, dry_run=True)
    assert out['ok'] and out['changed'] and s3.puts == [] and len(out['resulting']) == 2


def test_save_needs_the_bucket_name_and_puts_the_merged_rules(s3):
    assert handle_lifecycle_update({}, BODY, dry_run=False)[0] == 400 and s3.puts == []
    status, out = handle_lifecycle_update({}, {**BODY, 'confirm': 'fina-docs'}, dry_run=False)
    assert out['ok'] and s3.puts[0][0] == OTHER and s3.puts[0][1]['Expiration'] == {'Days': 30}


def test_save_with_nothing_changed_sends_nothing(s3):
    s3.rules = [OTHER, *managed_rules(params(expire_days=30))]
    status, out = handle_lifecycle_update({}, {**BODY, 'confirm': 'fina-docs'}, dry_run=False)
    assert out['ok'] and not out['changed'] and s3.puts == []


def test_removing_the_last_rule_deletes_the_configuration(s3):
    s3.rules = managed_rules(params(expire_days=30))
    status, out = handle_lifecycle_update({}, {**BODY, 'expire_days': '', 'confirm': 'fina-docs'}, dry_run=False)
    assert out['ok'] and s3.deletes == 1 and s3.puts == []


def test_missing_s3_section_is_reported(monkeypatch):
    status, out = handle_lifecycle_get({'api': {'base_url': 'https://x'}}, BODY)
    assert status == 200 and not out['ok'] and 'S3 endpoint not configured' in out['message']


def test_invalid_names_are_refused():
    assert handle_lifecycle_get({}, {'account': 'nope', 'bucket': 'b'})[0] == 400


def test_an_ssl_error_says_which_setting_to_change():
    from botocore.exceptions import SSLError
    message = lifecycle._s3_error(SSLError(endpoint_url='https://s3.example.lu', error='self-signed certificate'))
    assert 'ca_bundle_path' in message and 'self-signed certificate' in message


def test_a_missing_ca_bundle_is_named(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client, error = lifecycle._client({'api': {'base_url': 'https://x'}, 's3': {'endpoint_url': 'https://s3', 'ca_bundle_path': './s3-ca.pem'}}, 'sa-a-0001')
    assert client is None and str(tmp_path / 's3-ca.pem') in error and 'cos_ui.py was started' in error
