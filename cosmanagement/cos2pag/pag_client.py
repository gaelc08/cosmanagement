"""Client for the PoINT Archival Gateway (PAG) Administration API.

A "bucket" on PAG is an Object Repository, created under a Partition:
``POST /api/partitions/{partitionUuid}/repositories``.
"""
from __future__ import annotations

from typing import Any

import requests

from .http_client import ApiError, request_json


class PagClient:
    def __init__(self, base_url: str, session: requests.Session, timeout: int = 30, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.dry_run = dry_run

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def list_partitions(self) -> list[dict[str, Any]]:
        partitions: list[dict[str, Any]] = []
        prev_index = 0
        while True:
            page = request_json(
                self.session,
                "GET",
                self._url("/api/partitions"),
                timeout=self.timeout,
                params={"prevIndex": prev_index, "maxCount": 100},
            ) or []
            if not page:
                break
            partitions.extend(page)
            if len(page) < 100:
                break
            prev_index = page[-1]["index"]
        return partitions

    def find_partition(self, name_or_uuid: str) -> dict[str, Any]:
        partition = self.find_partition_optional(name_or_uuid)
        if partition is None:
            raise LookupError(f"PAG partition '{name_or_uuid}' not found")
        return partition

    def find_partition_optional(self, name_or_uuid: str) -> dict[str, Any] | None:
        for partition in self.list_partitions():
            if partition.get("uuid") == name_or_uuid or partition.get("name") == name_or_uuid:
                return partition
        return None

    def create_partition(self, body: dict[str, Any], encryption_password: str | None = None) -> dict[str, Any] | None:
        params = {"encryptionPassword": encryption_password} if encryption_password else None
        return request_json(
            self.session,
            "POST",
            self._url("/api/partitions"),
            timeout=self.timeout,
            json=body,
            params=params,
            dry_run=self.dry_run,
        )

    def ensure_partition(
        self,
        tenant_name: str,
        defaults: dict[str, Any],
        encryption_password: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Find the tenant's partition by name, creating it with the given
        settings (a static, pre-filled `PartitionInformationPatch`-shaped
        dict, e.g. copied once from an existing reference partition) if it
        doesn't exist yet.

        Returns ``(partition, action)`` where action is "found" or
        "created". In dry-run mode, on "created", the returned partition
        is ``None`` since no request is actually sent.
        """
        existing = self.find_partition_optional(tenant_name)
        if existing is not None:
            return existing, "found"

        body: dict[str, Any] = {"name": tenant_name, **defaults}
        password = encryption_password if defaults.get("cryptMode") == "PrivKey" else None
        return self.create_partition(body, encryption_password=password), "created"

    def get_persistent_buffer(self, partition_uuid: str) -> dict[str, Any] | None:
        try:
            return request_json(
                self.session,
                "GET",
                self._url(f"/api/partitions/{partition_uuid}/pst_buffer_config"),
                timeout=self.timeout,
            )
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    def set_persistent_buffer(self, partition_uuid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "PUT",
            self._url(f"/api/partitions/{partition_uuid}/pst_buffer_config"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def ensure_persistent_buffer(self, partition_uuid: str, desired: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Set the partition's persistent buffer configuration (PUT
        replaces it wholesale) unless it already matches every field in
        ``desired``.

        Returns ``(config, action)`` where action is one of "created"
        (none was configured yet), "updated", "unchanged".
        """
        existing = self.get_persistent_buffer(partition_uuid)
        if existing is not None and all(existing.get(k) == v for k, v in desired.items()):
            return existing, "unchanged"
        action = "created" if existing is None else "updated"
        return self.set_persistent_buffer(partition_uuid, desired), action

    def list_repositories(self, partition_uuid: str) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        prev_index = 0
        while True:
            page = request_json(
                self.session,
                "GET",
                self._url(f"/api/partitions/{partition_uuid}/repositories"),
                timeout=self.timeout,
                params={"prevIndex": prev_index, "maxCount": 100},
            ) or []
            if not page:
                break
            repositories.extend(page)
            if len(page) < 100:
                break
            prev_index = page[-1]["index"]
        return repositories

    def find_repository(self, partition_uuid: str, name: str) -> dict[str, Any] | None:
        for repo in self.list_repositories(partition_uuid):
            if repo.get("name") == name:
                return repo
        return None

    def create_repository(self, partition_uuid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "POST",
            self._url(f"/api/partitions/{partition_uuid}/repositories"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def update_repository(self, partition_uuid: str, repository_uuid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return request_json(
            self.session,
            "PATCH",
            self._url(f"/api/partitions/{partition_uuid}/repositories/{repository_uuid}"),
            timeout=self.timeout,
            json=body,
            dry_run=self.dry_run,
        )

    def ensure_repository(
        self,
        partition_uuid: str,
        name: str,
        owner: dict[str, Any],
        hidden: bool | None = None,
        write_protected: bool | None = None,
        sosapi_enabled: bool | None = None,
        obj_ver_state: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Create the repository if missing, or patch it only if something
        actually differs from an existing one.

        Returns ``(repository, action)`` where action is one of
        "created", "updated", "unchanged". Some PAG licenses don't cover
        `PATCH` on an existing repository (`ModifyObjectRepository`), so
        this avoids calling it at all when there's nothing to change.
        """
        existing = self.find_repository(partition_uuid, name)
        body: dict[str, Any] = {"name": name, "owner": owner}
        if hidden is not None:
            body["hidden"] = hidden
        if write_protected is not None:
            body["writeProtected"] = write_protected
        if sosapi_enabled is not None:
            body["sosapiEnabled"] = sosapi_enabled
        if obj_ver_state is not None:
            body["objVerState"] = obj_ver_state

        if existing is None:
            return self.create_repository(partition_uuid, body), "created"

        existing_owner = existing.get("owner") or {}
        needs_update = (
            existing_owner.get("uuid") != owner.get("uuid")
            or existing_owner.get("type") != owner.get("type")
            or (hidden is not None and existing.get("hidden") != hidden)
            or (write_protected is not None and existing.get("writeProtected") != write_protected)
            or (sosapi_enabled is not None and existing.get("sosapiEnabled") != sosapi_enabled)
            or (obj_ver_state is not None and existing.get("objVerState") != obj_ver_state)
        )
        if not needs_update:
            return existing, "unchanged"
        return self.update_repository(partition_uuid, existing["uuid"], body), "updated"


__all__ = ["PagClient", "ApiError"]
