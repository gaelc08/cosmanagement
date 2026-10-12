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


def write(path, data):
    path.write_text(__import__('json').dumps(data), encoding='utf-8')


def test_load_config_reads_hive_json_next_to_config_json(tmp_path):
    write(tmp_path / 'config.json', {'api': {'base_url': 'https://x'}})
    write(tmp_path / 'hive.json', HIVE)
    config = hm.load_config(str(tmp_path / 'config.json'))
    assert hm.hive_config(config) == HIVE and 'buckets' not in config


def test_hive_json_is_optional_by_default(tmp_path):
    write(tmp_path / 'config.json', {'api': {}, **HIVE})
    assert hm.hive_config(hm.load_config(str(tmp_path / 'config.json'))) == HIVE      # older config.json


def test_hive_json_wins_over_the_hive_settings_in_config_json(tmp_path):
    write(tmp_path / 'config.json', {'api': {}, 'hive': {'environments': ['prd']}})
    write(tmp_path / 'hive.json', HIVE)
    assert hm.hive_config(hm.load_config(str(tmp_path / 'config.json')))['environments'] == ['dev']


def test_an_explicit_hive_path_must_exist(tmp_path):
    import pytest
    write(tmp_path / 'config.json', {'api': {}})
    with pytest.raises(SystemExit):
        hm.load_config(str(tmp_path / 'config.json'), str(tmp_path / 'missing.json'))
    write(tmp_path / 'other.json', HIVE)
    assert hm.hive_config(hm.load_config(str(tmp_path / 'config.json'), str(tmp_path / 'other.json'))) == HIVE
