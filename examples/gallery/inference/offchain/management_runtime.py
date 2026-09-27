"""Compose management with the concrete proof, CBFS and grant adapters.

Only the trusted executor constructs these objects, using finalized actor/Hire
state and purpose-scoped CBSS credentials. HTTP JSON cannot configure any of
them. Bundle loading and native context admission belong to the executor.
"""

from dataclasses import dataclass
import time

from . import account_proof
from .cbfs_volume import CbfsVolume
from .grants import GrantAdapter
from .manage import Account, Management, Rejected
from .nonces import NonceStore


@dataclass(frozen=True)
class AccountProof:
    sig: bytes
    expiry: int
    nonce: bytes


@dataclass(frozen=True)
class VolumeBinding:
    name: str
    state_dir: str
    owner: bytes
    binary: str = "cbfs"
    rpc_url: str | None = None
    timeout_s: float = 30
    extra_args: tuple[str, ...] = ()  # Trusted executor configuration, never HTTP data.

    def volume(self):
        return CbfsVolume(self.name, self.state_dir, binary=self.binary,
                          rpc_url=self.rpc_url, timeout_s=self.timeout_s, extra_args=list(self.extra_args))

    def grants(self):
        return GrantAdapter(self.name, "0x" + self.owner.hex(), self.state_dir,
                            binary=self.binary, rpc_url=self.rpc_url, timeout_s=self.timeout_s, extra_args=list(self.extra_args))


class HireGrants:
    def __init__(self, bindings):
        self.bindings = dict(bindings)

    def put(self, slot, account, signing_key, encryption_key):
        grant = self.bindings[slot.hire_id].grants().put_read_grant(
            "0x" + account.hex(), signing_key.hex(), encryption_key.hex())
        return {"owner": grant.owner.removeprefix("0x").lower(), "volume": grant.volume_name}

    def remove(self, slot, account):
        self.bindings[slot.hire_id].grants().remove_read_grant("0x" + account.hex())


def build_management(*, actor, chain_id, network, slots, hire_lookup,
                     volumes: dict[int, VolumeBinding], nonce_volume: VolumeBinding | None = None,
                     issue_key=None, grant_bindings: dict[int, VolumeBinding] | None = None,
                     executor_claim_verifier=None,
                     handler="offchain.manage.Management.handle", clock=lambda: int(time.time())):
    """Nonce volume is shared by all management jobs for this actor, owner-only.

    `volumes` and grant bindings are trusted per-hire configuration. Their
    state directories may differ so only grant jobs receive owner-signing and
    DEK-release authority. Every data route needs nonce-volume write authority
    until the trusted executor claim verifier is configured. That construction
    argument switches replay ownership: executor mode requires a valid claim
    on every data request and never falls back to consuming a local nonce.

    executor_claim_verifier(req, caller20, claim_key32, digest32) must authenticate
    the executor's admitted context, match these expected values, and ensure this
    is the original authorized execution. It must never trust an HTTP-supplied
    field or rerun a claimed invocation. This is a native adapter, not a boolean
    supplied in HTTP JSON. No native claim format is invented by this module.
    """
    configured = dict(slots)
    bindings = dict(volumes)
    required = {slot.hire_id for slot in configured.values()}
    if not required <= bindings.keys():
        raise ValueError("missing hire volume binding")
    if executor_claim_verifier is None and nonce_volume is None:
        raise ValueError("management replay protection is required")
    if executor_claim_verifier is not None and not callable(executor_claim_verifier):
        raise ValueError("executor claim verifier must be callable")
    for binding in [*([nonce_volume] if nonce_volume is not None else []), *bindings.values(), *(grant_bindings or {}).values()]:
        if (not isinstance(binding.owner, bytes) or len(binding.owner) != 20
                or not binding.name or not binding.state_dir or binding.timeout_s <= 0
                or not isinstance(binding.extra_args, tuple)
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in binding.extra_args)):
            raise ValueError("invalid volume binding")
    # Nonce records must not be exposed by the whole-volume hirer read grants.
    if nonce_volume is not None and any((b.owner, b.name) == (nonce_volume.owner, nonce_volume.name) for b in bindings.values()):
        raise ValueError("nonce volume must be separate from hire volumes")
    if grant_bindings is not None:
        if set(grant_bindings) != required:
            raise ValueError("grant bindings must cover every configured hire")
        if any((b.owner, b.name) != (bindings[hire].owner, bindings[hire].name)
               for hire, b in grant_bindings.items()):
            raise ValueError("grant binding does not match hire volume")

    def verify(req):
        if ((req.actor, req.chain_id, req.network, req.handler) != (actor, chain_id, network, handler)
                or not isinstance(req.proof, AccountProof)):
            raise Rejected(403, "access_denied")
        proof = req.proof
        recovered = account_proof.verify(sig=proof.sig, chain_id=chain_id, network=network,
            actor=actor, handler=handler, verb=req.method, raw_target=req.raw_target,
            body=req.body, expiry=proof.expiry, nonce=proof.nonce, now=clock())
        return Account(recovered.address, recovered.expiry, recovered.nonce, recovered.digest)

    nonce_store = NonceStore(nonce_volume.volume(), clock=clock) if executor_claim_verifier is None else None

    def verify_replay(req, account):
        if executor_claim_verifier is not None:
            if req.executor_claim is None:
                return False
            key = account_proof.claim_key(chain_id=chain_id, network=network, actor=actor,
                                         caller=account.address, nonce=account.nonce)
            return executor_claim_verifier(req, account.address, key, account.digest) is True
        if req.executor_claim is not None:
            return False
        return nonce_store((chain_id, network, actor, account.address, account.nonce), account.expiry, account.digest)

    return Management(actor=actor, chain_id=chain_id, network=network, handler=handler,
        slots=configured, hire_lookup=hire_lookup,
        volume_for=lambda slot: bindings[slot.hire_id].volume(), verify_account=verify,
        verify_replay=verify_replay, issue_key=issue_key,
        grants=HireGrants(grant_bindings) if grant_bindings is not None else None, clock=clock)
