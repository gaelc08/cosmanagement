import json

import pytest

from cosmanagement import hive_management as hm
from cosmanagement.ui.search import _wildcard_regex, search, tenant_of

ACCOUNTS = ['sa-fina-0001', 'sa-fina-0002', 'sa-act-0001', 'sa-act-geoportal-0001']


def entry(name, used, quota, objects):
    return {'name': name, 'bytes_used': used, 'hard_quota': quota, 'object_count': objects}


BUCKETS = {
    'sa-fina-0001': [entry('fina-docs', 30, 60, 12), entry('fina-archive', 59, 60, 99)],
    'sa-fina-0002': [entry('fina-tmp', 0, 10, 0)],
    'sa-act-0001': [entry('act-data', 5, 100, 7)],
    'sa-act-geoportal-0001': [],
}


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    monkeypatch.setattr(hm, 'list_accounts', lambda config, prefix=None: [{'id': a} for a in ACCOUNTS if not prefix or a.startswith(prefix)])
    monkeypatch.setattr(hm, 'list_buckets_info', lambda config, account: BUCKETS[account])

    def get_bucket(config, name):
        for account, entries in BUCKETS.items():
            if any(e['name'] == name for e in entries):
                return 200, json.dumps({'service_instance': account})
        return 404, ''

    monkeypatch.setattr(hm, 'get_bucket', get_bucket)


def test_tenant_of():
    assert tenant_of('sa-act-0001') == 'act'
    assert tenant_of('sa-act-geoportal-0001') == 'act-geoportal'


def test_wildcard_regex_is_case_insensitive_and_anchored():
    rx = _wildcard_regex('Fi*A')
    assert rx.match('fina') and not rx.match('finab')
    assert not _wildcard_regex('fina').match('fina-docs')


def test_tenant_search_is_exact():
    result = search({}, 'tenant', 'act')
    assert list(result['accounts']) == ['sa-act-0001']          # not act-geoportal


def test_tenant_search_with_pattern_and_several_tenants():
    assert list(search({}, 'tenant', 'fi*')['accounts']) == ['sa-fina-0001', 'sa-fina-0002']
    assert list(search({}, 'tenant', 'fina,act-geoportal')['accounts']) == ['sa-act-geoportal-0001', 'sa-fina-0001', 'sa-fina-0002']


def test_account_search_prefix_may_be_left_out():
    assert list(search({}, 'account', 'fina-*')['accounts']) == ['sa-fina-0001', 'sa-fina-0002']
    assert list(search({}, 'account', '*-0001')['accounts']) == ['sa-act-0001', 'sa-act-geoportal-0001', 'sa-fina-0001']


def test_bucket_search_with_pattern_scans_every_account():
    result = search({}, 'bucket', '*tmp*')
    assert result['accounts'] == {'sa-fina-0002': ['fina-tmp']} and result['scanned'] == 4


def test_bucket_search_by_exact_name_keeps_the_listing_entry():
    result = search({}, 'bucket', 'fina-docs')
    assert result['accounts'] == {'sa-fina-0001': ['fina-docs']}
    assert result['info']['sa-fina-0001']['fina-docs']['object_count'] == 12


def test_unknown_bucket_is_reported_without_error():
    result = search({}, 'bucket', 'nope')
    assert result['accounts'] == {} and result['status'] == 404 and not result['has_error']
