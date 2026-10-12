"""Client for the PoINT Data Replicator (PDR) Administration API."""
from __future__ import annotations

from typing import Any

import requests

from .http_client import request_json


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


def _matches(desired: Any, existing: Any) -> bool:
    """True if every key/value present in ``desired`` is also present
    (and equal) in ``existing``. Extra keys on ``existing`` are ignored,
    so this only flags real drift on fields we actually configure.
    """
    if isinstance(desired, dict):
        if not isinstance(existing, dict):
            return False
        return all(_matches(v, existing.get(k)) for k, v in desired.items())
    if isinstance(desired, list):
        if not isinstance(existing, list) or len(desired) != len(existing):
            return False
        return all(_matches(d, e) for d, e in zip(desired, existing))
    return desired == existing


class PdrClient:
    def __init__(self, base_url: str, session: requests.Session, timeout: int = 30, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.dry_run = dry_run

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def list_tasks(self) -> list[dict[str, Any]]:
        return request_json(self.session, "GET", self._url("/api/tasks"), timeout=self.timeout) or []

    def find_task_by_alias(self, alias: str) -> dict[str, Any] | None:
        for task in self.list_tasks():
            if task.get("alias") == alias:
                return task
        return None

    def create_task(self, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "POST",
            self._url("/api/tasks"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def update_task(self, task_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "PATCH",
            self._url(f"/api/tasks/{task_id}"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def ensure_task(self, body: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Create the task if missing, or patch it only if `options`,
        `schedule` or `notifications` actually differ from an existing
        task with the same alias.

        Returns ``(task, action)`` where action is one of "created",
        "updated", "unchanged". `source`/`target` are deliberately not
        compared: PDR doesn't echo S3 secret keys back on GET, so
        comparing them would flag a spurious mismatch on every run.
        """
        alias = body.get("alias")
        existing = self.find_task_by_alias(alias) if alias else None
        if existing is None:
            return self.create_task(body), "created"

        desired = _strip_none({k: body.get(k) for k in ("options", "schedule", "notifications")})
        if _matches(desired, existing):
            return existing, "unchanged"
        return self.update_task(existing["id"], body), "updated"

    def start_job(self, task_id: int, job_type: str = "Copy", filter_path: str | None = None) -> dict[str, Any] | None:
        job_body: dict[str, Any] = {"jobType": job_type}
        if filter_path:
            job_body["filterPath"] = filter_path
        return request_json(
            self.session,
            "POST",
            self._url(f"/api/tasks/{task_id}/jobs"),
            timeout=self.timeout,
            json=job_body,
            dry_run=self.dry_run,
        )
