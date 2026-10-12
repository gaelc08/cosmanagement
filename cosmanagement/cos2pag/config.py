"""Configuration loading for cos2pag.

Config is plain YAML. Any string value of the form ``${VAR_NAME}`` is
replaced with the environment variable ``VAR_NAME`` at load time, so
credentials never have to live in the YAML file itself.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import yaml

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"Environment variable '{name}' referenced in config is not set")
            return os.environ[name]

        return _ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file '{path}' does not contain a YAML mapping")
    return _interpolate(raw)


@dataclass(frozen=True)
class AuthConfig:
    type: str  # "basic" | "bearer"
    username: str | None = None
    password: str | None = None
    token: str | None = None

    @staticmethod
    def from_dict(d: dict) -> "AuthConfig":
        auth_type = d.get("type", "basic")
        if auth_type == "basic":
            if not d.get("username") or not d.get("password"):
                raise ConfigError("Basic auth requires 'username' and 'password'")
        elif auth_type == "bearer":
            if not d.get("token"):
                raise ConfigError("Bearer auth requires 'token'")
        else:
            raise ConfigError(f"Unsupported auth type: {auth_type}")
        return AuthConfig(
            type=auth_type,
            username=d.get("username"),
            password=d.get("password"),
            token=d.get("token"),
        )


def require(d: dict, key: str, section: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"Missing required config key '{section}.{key}'")
    return d[key]
