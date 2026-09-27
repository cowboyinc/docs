import _sdk_path  # noqa: F401  (examples/_sdk_path.py, on pythonpath via pyproject.toml)
import pytest
from cowboy_sdk import codec
from cowboy_sdk.errors import PermissionDeniedError
from cowboy_sdk.testing import SimulatedChain

from actors.roles.main import AuthRoles

OWNER = "0x" + "11" * 20
WRITER = "0x" + "44" * 20
STRANGER = "0x" + "33" * 20


def _deployed():
    actor = AuthRoles()
    chain = SimulatedChain(actor, sender=OWNER)
    chain.call(actor.init, b"{}")
    return actor, chain


def test_init_claims_deployer_as_owner_and_is_a_no_op_afterwards() -> None:
    actor = AuthRoles()
    chain = SimulatedChain(actor, sender=OWNER)
    first = codec.decode(chain.call(actor.init, b"{}"))
    assert first == {"status": "initialized", "owner": OWNER}
    chain.call(actor.grant_role, codec.encode({"role": "writer", "address": WRITER}))
    chain.set_sender(STRANGER)
    again = codec.decode(chain.call(actor.init, b'{"owner":"' + STRANGER.encode() + b'"}'))
    assert again == {"status": "already_initialized", "owner": OWNER}
    assert codec.decode(chain.state("roles")) == {"writer": [WRITER]}


def test_only_owner_can_grant_roles() -> None:
    actor, chain = _deployed()
    granted = codec.decode(chain.call(actor.grant_role, codec.encode({"role": "writer", "address": WRITER})))
    assert granted == {"status": "granted", "role": "writer", "address": WRITER}
    chain.set_sender(WRITER)
    with pytest.raises(PermissionDeniedError):
        chain.call(actor.grant_role, codec.encode({"role": "writer", "address": STRANGER}))


def test_delegated_writer_can_write_and_stranger_cannot() -> None:
    actor, chain = _deployed()
    chain.call(actor.grant_role, codec.encode({"role": "writer", "address": WRITER}))
    chain.set_sender(WRITER)
    written = codec.decode(chain.call(actor.write_entry, codec.encode({"content": "hi"})))
    assert written == {"status": "written", "entry": {"entry_id": 1, "author": WRITER, "content": "hi"}}
    chain.set_sender(STRANGER)
    denied = codec.decode(chain.call(actor.write_entry, codec.encode({"content": "nope"})))
    assert denied == {"error": "UNAUTHORIZED"}
    entries = codec.decode(chain.call(actor.get_entries, codec.encode({})))
    assert entries == {"entries": [{"entry_id": 1, "author": WRITER, "content": "hi"}]}


def test_get_roles_reads_owner_from_runtime() -> None:
    actor, chain = _deployed()
    chain.call(actor.grant_role, codec.encode({"role": "writer", "address": WRITER}))
    chain.call(actor.revoke_role, codec.encode({"role": "writer", "address": WRITER}))
    roles = codec.decode(chain.call(actor.get_roles, codec.encode({})))
    assert roles == {"owner": OWNER, "roles": {"writer": []}}
