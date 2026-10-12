import pytest

from cos2pag.config import ConfigError, load_config


def test_load_config_interpolates_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
        pag:
          auth:
            username: admin
            password: "${MY_SECRET}"
        """
    )
    config = load_config(str(config_file))
    assert config["pag"]["auth"]["password"] == "s3cr3t"
    assert config["pag"]["auth"]["username"] == "admin"


def test_load_config_raises_on_missing_env_var(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
        pag:
          auth:
            password: "${DOES_NOT_EXIST_VAR}"
        """
    )
    with pytest.raises(ConfigError):
        load_config(str(config_file))
