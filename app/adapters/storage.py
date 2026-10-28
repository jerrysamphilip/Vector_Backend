"""
File storage behind a Protocol (ADR 0009 adapter layer, chunk S2; prepares G4: S3 / EFS).

A ``FileStore`` holds opaque blobs under flat keys (e.g. ``<uuid>_<original name>`` for template
attachments, as stored in ``email_attachments.storage_path``). ``LocalFileStore`` keeps them in one
directory on disk, exactly where the code wrote them before (settings.ATTACHMENTS_DIR); an S3 store
implements the same five methods, returns None from ``local_path`` and a presigned link from ``url``.

Keys never leave the store's root: a key with a path separator or ``..`` component is refused on
write and reported missing on read.

Callers get stores from ``app.adapters.providers`` (``attachment_store()``); unit tests use an
in-memory fake or a LocalFileStore on a temporary directory.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, Union


class FileStore(Protocol):
    def put(self, key: str, data: bytes) -> None:
        """Store ``data`` under ``key`` (replacing what was there)."""
        ...

    def get(self, key: str) -> bytes:
        """The stored bytes; FileNotFoundError when the key is missing, OSError on read errors."""
        ...

    def delete(self, key: str) -> None:
        """Remove the key; a missing key is not an error."""
        ...

    def exists(self, key: str) -> bool:
        ...

    def url(self, key: str) -> str:
        """Where a client can fetch the file (a ``file://`` URI locally, a presigned URL on S3)."""
        ...

    def local_path(self, key: str) -> Optional[Path]:
        """The file on this machine's disk when there is one (lets routes stream it), else None."""
        ...


class UnsafeStorageKey(ValueError):
    """The key would leave the store's root directory."""


class LocalFileStore:
    """Files in one directory; the directory is created on first write."""

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if not key or "/" in key or "\\" in key or key in (".", "..") or "\x00" in key:
            raise UnsafeStorageKey(f"invalid storage key: {key!r}")
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        self.root.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as out_file:
            out_file.write(data)

    def get(self, key: str) -> bytes:
        try:
            path = self._path(key)
        except UnsafeStorageKey as exc:
            raise FileNotFoundError(key) from exc
        with open(path, "rb") as in_file:
            return in_file.read()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except UnsafeStorageKey:
            return

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).exists()
        except UnsafeStorageKey:
            return False

    def url(self, key: str) -> str:
        return self._path(key).resolve().as_uri()

    def local_path(self, key: str) -> Optional[Path]:
        try:
            return self._path(key)
        except UnsafeStorageKey:
            return None
