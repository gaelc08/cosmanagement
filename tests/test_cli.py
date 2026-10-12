import os
from unittest.mock import MagicMock

import pytest

from cos2pag import cli


def test_tenant_argument_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["my-bucket"])


def test_main_loads_env_file_before_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MY_TEST_SECRET=from-dotenv\n")
    (tmp_path / "config.yaml").write_text(
        "cos:\n  token: \"${MY_TEST_SECRET}\"\n"
    )
    monkeypatch.delenv("MY_TEST_SECRET", raising=False)

    captured = {}

    def fake_sync_bucket(config, bucket_name, tenant, dry_run=False, noncurrent_expiration_days=None):
        captured["config"] = config
        report = MagicMock()
        report.steps = []
        return report

    monkeypatch.setattr(cli, "sync_bucket", fake_sync_bucket)

    exit_code = cli.main(["my-bucket", "--tenant", "MY-TENANT"])

    assert exit_code == 0
    assert captured["config"]["cos"]["token"] == "from-dotenv"
    assert os.environ["MY_TEST_SECRET"] == "from-dotenv"


def test_main_env_file_overrides_stale_shell_export(tmp_path, monkeypatch):
    # A variable already exported in the shell (e.g. from an earlier
    # `source .env` used to test with curl) must not shadow a
    # since-corrected value in the .env file.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MY_TEST_SECRET=corrected-value\n")
    (tmp_path / "config.yaml").write_text(
        "cos:\n  token: \"${MY_TEST_SECRET}\"\n"
    )
    monkeypatch.setenv("MY_TEST_SECRET", "stale-shell-value")

    captured = {}

    def fake_sync_bucket(config, bucket_name, tenant, dry_run=False, noncurrent_expiration_days=None):
        captured["config"] = config
        report = MagicMock()
        report.steps = []
        return report

    monkeypatch.setattr(cli, "sync_bucket", fake_sync_bucket)

    cli.main(["my-bucket", "--tenant", "MY-TENANT"])

    assert captured["config"]["cos"]["token"] == "corrected-value"


def test_main_passes_noncurrent_expiration_days_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("cos:\n  x: 1\n")

    captured = {}

    def fake_sync_bucket(config, bucket_name, tenant, dry_run=False, noncurrent_expiration_days=None):
        captured["noncurrent_expiration_days"] = noncurrent_expiration_days
        report = MagicMock()
        report.steps = []
        return report

    monkeypatch.setattr(cli, "sync_bucket", fake_sync_bucket)

    cli.main(["my-bucket", "--tenant", "MY-TENANT", "--noncurrent-expiration-days", "45"])

    assert captured["noncurrent_expiration_days"] == 45


def test_main_noncurrent_expiration_days_defaults_to_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("cos:\n  x: 1\n")

    captured = {}

    def fake_sync_bucket(config, bucket_name, tenant, dry_run=False, noncurrent_expiration_days=None):
        captured["noncurrent_expiration_days"] = noncurrent_expiration_days
        report = MagicMock()
        report.steps = []
        return report

    monkeypatch.setattr(cli, "sync_bucket", fake_sync_bucket)

    cli.main(["my-bucket", "--tenant", "MY-TENANT"])

    assert captured["noncurrent_expiration_days"] is None
