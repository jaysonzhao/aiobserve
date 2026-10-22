"""Bounded in-memory storage for immutable dashboard snapshots and export jobs."""

from __future__ import annotations

import secrets
import time
import copy
from dataclasses import dataclass
from typing import Any

from .settings import settings


@dataclass(frozen=True)
class SnapshotEntry:
    id: str
    owner: str
    created_at: float
    expires_at: float
    data: dict[str, Any]


@dataclass(frozen=True)
class ExportEntry:
    id: str
    owner: str
    namespace: str
    created_at: float
    expires_at: float
    filename: str
    media_type: str
    content: bytes


class SnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, SnapshotEntry] = {}
        self._exports: dict[str, ExportEntry] = {}

    def _prune(self) -> None:
        now = time.time()
        self._snapshots = {key: item for key, item in self._snapshots.items() if item.expires_at > now}
        self._exports = {key: item for key, item in self._exports.items() if item.expires_at > now}
        while len(self._snapshots) > settings.max_snapshots:
            oldest = min(self._snapshots, key=lambda key: self._snapshots[key].created_at)
            del self._snapshots[oldest]
        while len(self._exports) > 5:
            oldest = min(self._exports, key=lambda key: self._exports[key].created_at)
            del self._exports[oldest]

    def create_snapshot(self, owner: str, data: dict[str, Any]) -> SnapshotEntry:
        self._prune()
        now = time.time()
        entry = SnapshotEntry(
            id=secrets.token_urlsafe(24),
            owner=owner,
            created_at=now,
            expires_at=now + settings.snapshot_ttl_seconds,
            data=copy.deepcopy(data),
        )
        self._snapshots[entry.id] = entry
        self._prune()
        return entry

    def get_snapshot(self, snapshot_id: str, owner: str) -> SnapshotEntry:
        self._prune()
        entry = self._snapshots.get(snapshot_id)
        if not entry:
            raise KeyError("Snapshot has expired or is unavailable.")
        if entry.owner != owner:
            raise PermissionError("Snapshot belongs to another authenticated user.")
        return entry

    def create_export(self, owner: str, namespace: str, filename: str, media_type: str, content: bytes) -> ExportEntry:
        if len(content) > 30 * 1024 * 1024:
            raise OverflowError("Export exceeds the 30 MiB response limit.")
        self._prune()
        now = time.time()
        entry = ExportEntry(
            id=secrets.token_urlsafe(24),
            owner=owner,
            namespace=namespace,
            created_at=now,
            expires_at=now + settings.snapshot_ttl_seconds,
            filename=filename,
            media_type=media_type,
            content=content,
        )
        self._exports[entry.id] = entry
        self._prune()
        return entry

    def get_export(self, export_id: str, owner: str) -> ExportEntry:
        self._prune()
        entry = self._exports.get(export_id)
        if not entry:
            raise KeyError("Export has expired or is unavailable.")
        if entry.owner != owner:
            raise PermissionError("Export belongs to another authenticated user.")
        return entry


snapshots = SnapshotStore()
