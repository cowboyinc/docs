"""`Volume` over the cbfs CLI: one process per operation, compare-and-swap
commits through `cbfs tx --expect-root`.

The handler job runs with a state dir holding the volume owner's cbfs
delegation (released to the job, never in the volume). `root()` reads the
chain's current manifest root, `list()`/`read()` open the volume read-only at
that root, and `commit(prev_root, writes)` refuses with ConflictError when
the root moved (exit code 3) so `Volume.mutate` re-reads and reapplies.
Deletions are expressed as a `None` value in `writes`.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass, field

from .cbfs_proc import CbfsError, is_rate_limited, run_cbfs
from .volume import BusyError, ConflictError, Volume

CONFLICT_EXIT_CODE = 3
NOT_FOUND_EXIT_CODE = 4


@dataclass
class CbfsVolume(Volume):
    name: str
    state_dir: str
    binary: str = "cbfs"
    rpc_url: str | None = None
    timeout_s: float = 120
    max_output_bytes: int = 16 * 1024 * 1024  # stdout cap; the process is killed past it
    extra_args: list[str] = field(default_factory=list)

    def _run(self, *args: str, stdin: bytes | None = None, ok_codes=(0,)) -> subprocess.CompletedProcess:
        return run_cbfs(self.binary, self.state_dir, list(args), rpc_url=self.rpc_url, extra_args=self.extra_args, stdin=stdin, timeout_s=self.timeout_s, max_output_bytes=self.max_output_bytes, ok_codes=ok_codes)

    def root(self) -> str:
        out = json.loads(self._run("root", self.name).stdout)
        return out["manifest_root"]

    def list(self, prefix: str) -> list[str]:
        args = ["ls", self.name] + ([prefix] if prefix else [])
        entries = json.loads(self._run(*args).stdout)
        return sorted(e["path"] for e in entries if e["path"].startswith(prefix))

    def read(self, path: str) -> bytes | None:
        """None only on the CLI's explicit not-found exit code; every other
        failure (network, auth, relay) raises so it cannot read as absence."""
        proc = self._run("cat", self.name, path, ok_codes=(0, NOT_FOUND_EXIT_CODE))
        return None if proc.returncode == NOT_FOUND_EXIT_CODE else proc.stdout

    def commit(self, prev_root: str, writes: dict[str, bytes | None]) -> str:
        batch = {
            "puts": [{"remote": p, "data_b64": base64.b64encode(d).decode()} for p, d in writes.items() if d is not None],
            "removes": [p for p, d in writes.items() if d is None],
        }
        if not batch["puts"] and not batch["removes"]:
            return prev_root
        try:
            proc = self._run("tx", self.name, "--expect-root", prev_root, "--from-json", "-", stdin=json.dumps(batch).encode(), ok_codes=(0, CONFLICT_EXIT_CODE))
        except CbfsError as exc:
            if _is_busy(str(exc)):
                raise BusyError(str(exc)[:200]) from exc
            raise
        if proc.returncode == CONFLICT_EXIT_CODE:
            raise ConflictError(proc.stderr.decode(errors="replace").strip()[:200])
        return json.loads(proc.stdout)["manifest_root"]




_COMMIT_CONFLICT_MARKERS = ("ErrConflict", "tx_reverted", "E1213")


def _is_busy(message: str) -> bool:
    """After run_cbfs has exhausted its rate-limit retries, a still-limited
    tx, or one the chain reverted because prev_root moved (commit-v2 CAS:
    tx_reverted / E1213 / ErrConflict) at any tx stage, including the SDK's
    pending-commit replay during reconciliation, is a conflict: back off,
    re-read and reapply. The backoff also covers the lag between a winning
    commit's inclusion and its visibility to the root read."""
    return is_rate_limited(message) or ("cbfs tx failed" in message and any(m in message for m in _COMMIT_CONFLICT_MARKERS))
