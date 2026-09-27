"""Hire-volume adapter (CIP §3.4 layout) with a compare-and-swap commit.

One volume per hire, owned by the listing creator:

    keys/<key_id>.json               key record
    usage/<session>/<segment>.jsonl  append-only request records
    catalog.json                     enabled models, limits, current pricing version
    pricing/<version>.json           immutable rate tables

Every mutation is a CAS commit against the root the writer read; a conflict
re-reads and reapplies. The local-filesystem backend below is for tests only;
the CBFS backend implements the same interface over the cbfs client's
manifest CAS and is wired in by the deployment.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


class ConflictError(RuntimeError):
    """The volume root moved between read and commit."""


class BusyError(ConflictError):
    """The backend refused the commit transiently (rate limit, busy relayer);
    re-read and reapply after a backoff, exactly like a root conflict."""


class Volume:
    """Interface every backend provides."""

    def read(self, path: str) -> bytes | None:
        raise NotImplementedError

    def list(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def root(self) -> str:
        raise NotImplementedError

    def commit(self, prev_root: str, writes: dict[str, bytes]) -> str:
        """Apply `writes` atomically iff the current root equals `prev_root`.

        Returns the new root. Raises ConflictError otherwise.
        """
        raise NotImplementedError

    def mutate(self, apply: Callable[["Volume"], dict[str, bytes]], retries: int = 16) -> str:
        """Read-modify-write under CAS: `apply` sees the volume at one root and
        returns the writes; a conflict or a busy backend re-runs `apply`
        against the new root after a jittered exponential backoff."""
        for attempt in range(retries):
            prev = self.root()
            writes = apply(self)
            try:
                return self.commit(prev, writes)
            except ConflictError:
                if attempt + 1 < retries:
                    time.sleep(min(0.1 * (2 ** attempt), 3.0) * (0.5 + random.random()))
                continue
        raise ConflictError("volume root kept moving; giving up")


@dataclass
class LocalVolume(Volume):
    """TEST-ONLY directory-backed volume with a content root over (path, sha256)
    pairs. The lock is per instance and a multi-file commit is neither atomic
    nor durable across a crash; it exercises the interface, not CBFS's
    manifest CAS guarantees."""

    base: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.base = Path(self.base)
        self.base.mkdir(parents=True, exist_ok=True)

    def _p(self, path: str) -> Path:
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"bad volume path {path!r}")
        return self.base / path

    def read(self, path: str) -> bytes | None:
        p = self._p(path)
        return p.read_bytes() if p.is_file() else None

    def list(self, prefix: str) -> list[str]:
        out = []
        for p in self.base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.base).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def root(self) -> str:
        h = hashlib.sha256()
        for rel in self.list(""):
            h.update(rel.encode())
            h.update(b"\0")
            h.update(hashlib.sha256(self.read(rel) or b"").digest())
        return h.hexdigest()

    def commit(self, prev_root: str, writes: dict[str, bytes]) -> str:
        with self._lock:
            if self.root() != prev_root:
                raise ConflictError("root moved")
            for path, data in writes.items():
                p = self._p(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, p)
            return self.root()


def read_json(volume: Volume, path: str) -> dict | None:
    raw = volume.read(path)
    return None if raw is None else json.loads(raw)


def encode_json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
