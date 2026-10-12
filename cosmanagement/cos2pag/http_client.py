"""Small HTTP helper shared by the COS / PAG / PDR clients."""
from __future__ import annotations

import logging
from typing import Any

import requests
import urllib3

from .config import AuthConfig

logger = logging.getLogger(__name__)

_REDACTED = "***REDACTED***"
_SENSITIVE_KEYS = {"password", "secret", "secretkey", "accesskey", "token", "apikey"}


def _normalize_key(key: str) -> str:
    return key.lower().replace("_", "")


def redact_secrets(value: Any) -> Any:
    """Recursively mask values of sensitive-looking keys (password, secret
    key, access key, token, ...) so request bodies can be logged (e.g. in
    --dry-run) without leaking credentials to the terminal/log files.
    """
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _normalize_key(k) in _SENSITIVE_KEYS else redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


class ApiError(Exception):
    def __init__(self, method: str, url: str, status_code: int, body: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status_code}: {body[:500]}")


def build_session(auth: AuthConfig, verify_ssl: bool | str = True) -> requests.Session:
    session = requests.Session()
    session.verify = verify_ssl
    if verify_ssl is False:
        # Internal appliances here run with self-signed certs; don't spam
        # the logs with a warning for a choice the config made on purpose.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    if auth.type == "basic":
        session.auth = (auth.username, auth.password)
    elif auth.type == "bearer":
        session.headers["Authorization"] = f"Bearer {auth.token}"
    return session


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    timeout: int = 30,
    dry_run: bool = False,
    **kwargs: Any,
) -> dict | list | None:
    """Perform a request and return the parsed JSON body (or None for 204/empty).

    In dry-run mode, mutating requests (anything but GET) are logged and not
    actually sent.
    """
    if dry_run and method.upper() != "GET":
        logger.info("[dry-run] %s %s json=%s", method, url, redact_secrets(kwargs.get("json")))
        return None

    resp = session.request(method, url, timeout=timeout, **kwargs)
    if resp.status_code >= 400:
        raise ApiError(method, url, resp.status_code, resp.text)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None
