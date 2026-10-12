"""Client for the IBM COS Container Mode Service API (bucket management).

Reference: "IBM Cloud Object Storage System - Container Mode Service API
Guide - Bucket Management" (v3.20.x). Base command is
``<accesser>:8338/container/{bucket.name}`` with GET/PATCH/PUT/DELETE.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any

import requests

from .http_client import request_json


def _to_http_date(iso_timestamp: str) -> str:
    """Convert an RFC3339 timestamp (as returned in `time_updated`, e.g.
    "2026-09-18T15:02:44.609Z") to the RFC7231 HTTP-date format the
    `If-Unmodified-Since` request header requires (e.g.
    "Wed, 18 Sep 2026 15:02:44 GMT"). Sending the RFC3339 string as-is
    makes the server reject the PATCH with a generic 400 Bad Request.
    """
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def acl_map_to_pairs(acl_map: dict[str, list[str]] | None) -> list[dict[str, str]]:
    """Convert the GET response ACL shape (grantee -> [permission, ...])
    into the array-of-pairs shape the PATCH/PUT request body expects.
    """
    pairs: list[dict[str, str]] = []
    for grantee, permissions in (acl_map or {}).items():
        for permission in permissions:
            pairs.append({"grantee": grantee, "permission": permission})
    return pairs


def merge_acl(acl_map: dict[str, list[str]] | None, grantee: str, permission: str) -> list[dict[str, str]]:
    """Add (grantee, permission) to the ACL if not already present.

    A bucket PATCH with an ``acl`` array replaces the *entire* ACL, so the
    full existing ACL must always be resent alongside the new grant.
    """
    pairs = acl_map_to_pairs(acl_map)
    already_present = any(p["grantee"] == grantee and p["permission"] == permission for p in pairs)
    if not already_present:
        pairs.append({"grantee": grantee, "permission": permission})
    return pairs


def merge_allowed_ip(
    firewall: dict[str, Any] | None,
    new_ip: str,
    allow_create_whitelist: bool = False,
) -> tuple[list[str] | None, bool]:
    """Add ``new_ip`` to the bucket's ``firewall.allowed_ip`` whitelist.

    Returns ``(new_allowed_ip_list_or_None, changed)``.

    Important safety rule from the API doc: when ``allowed_ip`` is absent,
    the bucket is reachable from *any* IP not explicitly denied. Sending a
    PATCH with ``allowed_ip: [new_ip]`` in that situation does not "add" an
    entry, it *creates* a whitelist and instantly locks the bucket down to
    only that one IP. So by default, if there is no pre-existing whitelist
    to extend, this returns ``(None, False)`` (no-op) unless the caller
    explicitly opts in via ``allow_create_whitelist``.
    """
    existing = (firewall or {}).get("allowed_ip")
    if existing is None:
        if not allow_create_whitelist:
            return None, False
        existing = []
    if new_ip in existing:
        return existing, False
    return [*existing, new_ip], True


class CosClient:
    def __init__(self, base_url: str, session: requests.Session, timeout: int = 30, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.dry_run = dry_run

    def _bucket_url(self, bucket_name: str) -> str:
        return f"{self.base_url}/container/{bucket_name}"

    def get_bucket(self, bucket_name: str) -> dict[str, Any]:
        return request_json(self.session, "GET", self._bucket_url(bucket_name), timeout=self.timeout)

    def patch_bucket(
        self,
        bucket_name: str,
        body: dict[str, Any],
        if_unmodified_since: str | None = None,
    ) -> dict[str, Any] | None:
        headers = {}
        if if_unmodified_since:
            headers["If-Unmodified-Since"] = _to_http_date(if_unmodified_since)
        return request_json(
            self.session,
            "PATCH",
            self._bucket_url(bucket_name),
            timeout=self.timeout,
            json=body,
            headers=headers,
            dry_run=self.dry_run,
        )
