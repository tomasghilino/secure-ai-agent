"""Object storage for uploaded PDFs (ADR-0003): a local directory now, S3 in staging.

Keys are content-addressed (``uploads/<tenant>/<sha256>.pdf``): nothing the client sends
ends up in a path, and storing the same file twice writes the same object.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import Settings


class ObjectNotFoundError(LookupError):
    """No object is stored under this key."""


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...


def upload_key(tenant_id: str, sha256: str) -> str:
    return f"uploads/{tenant_id}/{sha256}.pdf"


class LocalFileStorage:
    """Files under a root directory. File I/O blocks, so it runs in a worker thread."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):  # defense in depth against "../" keys
            raise ValueError(f"storage key escapes the storage root: {key!r}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(_write_atomically, self._path(key), data)

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc


def _write_atomically(path: Path, data: bytes) -> None:
    # Write a temporary file, then rename it: a reader never sees half a file.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def build_storage(settings: Settings) -> ObjectStorage:
    return LocalFileStorage(settings.storage_root)  # S3 adapter: Day 4
