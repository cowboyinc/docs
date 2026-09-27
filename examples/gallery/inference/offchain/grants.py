"""CIP-9 §7.7.1 direct-client read grants on a hire volume, driven through
the cbfs CLI under the volume owner's delegation state dir.

`put_read_grant` records (or replaces) a READ_ONLY whole-volume grant for the
hirer's account bound to its cbfs Ed25519 signing key, with the volume DEK
sealed to its X25519 encryption key by the CLI (the DEK never enters Python).
`remove_read_grant` drops it. Both are idempotent at the chain level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cbfs_proc import CbfsError, run_cbfs

_HEX32 = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class GrantError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadGrant:
    owner: str  # 0x-prefixed volume owner
    volume_name: str
    grantee: str
    signing_key: str  # hex, no 0x
    encryption_key: str


@dataclass
class GrantAdapter:
    volume_name: str
    owner: str
    state_dir: str
    binary: str = "cbfs"
    rpc_url: str | None = None
    timeout_s: float = 180
    # Trusted CLI arguments the deployment configures, e.g. --trusted-checkpoint
    # <path> for grants on a private volume. Never from the request.
    extra_args: list[str] = field(default_factory=list)

    def _run(self, *args: str) -> str:
        try:
            proc = run_cbfs(self.binary, self.state_dir, list(args), rpc_url=self.rpc_url, extra_args=self.extra_args, timeout_s=self.timeout_s, max_output_bytes=1 << 20)
        except CbfsError as exc:
            raise GrantError(str(exc)) from exc
        return proc.stdout.decode(errors="replace")

    def put_read_grant(self, grantee: str, signing_key: str, encryption_key: str) -> ReadGrant:
        if not _ADDR.match(grantee):
            raise GrantError("grantee must be a 0x-prefixed 20-byte address")
        sk = _key(signing_key, "signing_key")
        ek = _key(encryption_key, "encryption_key")
        self._run("volume", "allow", self.volume_name, "--account", grantee, "--mode", "ro", "--signing-key", sk, "--encryption-key", ek)
        return ReadGrant(self.owner, self.volume_name, grantee, sk, ek)

    def remove_read_grant(self, grantee: str) -> None:
        if not _ADDR.match(grantee):
            raise GrantError("grantee must be a 0x-prefixed 20-byte address")
        self._run("volume", "allow", self.volume_name, "--account", grantee, "--remove")


def _key(value: str, label: str) -> str:
    if not isinstance(value, str) or not _HEX32.match(value):
        raise GrantError(f"{label} must be 32 bytes of hex")
    return value.lower().removeprefix("0x")
