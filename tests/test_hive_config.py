from cosmanagement import hive_management as hm

HIVE = {
    'buckets': [{'sa': '0026', 'bucket': 'fina', 'quota-dev': 60}],
    'environments': ['dev'],
    'storage_locations': {'dev': 'cv-ctie-hive-dev-01'},
    'firewall_rules': {'dev': ['10.0.0.1']},
}


def test_hive_settings_under_the_hive_key():
    config = {'api': {'base_url': 'https://x'}, 'hive': HIVE}
    assert hm.hive_config(config) == HIVE
    items, errors = hm.plan_buckets(config)
    assert not errors and items[0]['bucket_name'] == 'ctie-hive-dev-fina' and items[0]['allowed_ips'] == ['10.0.0.1']
    assert [a['account_id'] for a in hm.plan_storage_accounts(config)] == ['sa-ctie-hive-dev-0026']


def test_older_config_with_the_keys_at_the_top_level_still_works():
    config = {'api': {'base_url': 'https://x'}, **HIVE}
    assert hm.hive_config(config) == HIVE
    assert hm.plan_buckets(config)[0][0]['storage_location'] == 'cv-ctie-hive-dev-01'


def test_the_hive_key_wins_over_top_level_keys():
    config = {'environments': ['prd'], 'hive': HIVE}
    assert hm.hive_config(config)['environments'] == ['dev']


def test_no_hive_settings_at_all():
    assert hm.hive_config({'api': {}}) == {}
