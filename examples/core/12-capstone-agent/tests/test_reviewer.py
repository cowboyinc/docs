from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.reviewer.main import CapstoneAgent

OWNER = "0x" + "11" * 20
STRANGER = "0x" + "22" * 20


def _deployed():
    actor = CapstoneAgent()
    chain = SimulatedChain(actor, sender=OWNER)
    chain.call(actor.init, b"")
    return actor, chain


def test_init_records_sender_as_owner() -> None:
    actor, chain = _deployed()
    assert codec.decode(chain.state("owner")) == OWNER
    assert chain.events()[0] == ("capstone.init", codec.encode({"owner": OWNER}))
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {"proposal_count": 0, "approved_count": 0, "rejected_count": 0, "owner": OWNER}


def test_submit_proposal_stores_record() -> None:
    actor, chain = _deployed()
    chain.set_sender(STRANGER)
    reply = codec.decode(chain.call(actor.submit_proposal, codec.encode({"title": "Grants", "body": "Fund examples."})))
    assert reply == {"status": "submitted", "proposal_id": 1}
    proposal = codec.decode(chain.call(actor.get_proposal, codec.encode({"proposal_id": 1})))
    assert proposal["title"] == "Grants"
    assert proposal["author"] == STRANGER
    assert proposal["status"] == "submitted"
    assert proposal["review_status"] == "none"


def test_reject_is_owner_gated_and_counts() -> None:
    actor, chain = _deployed()
    chain.call(actor.submit_proposal, codec.encode({"title": "Grants"}))
    chain.set_sender(STRANGER)
    assert codec.decode(chain.call(actor.reject, codec.encode({"proposal_id": 1}))) == {"error": "UNAUTHORIZED"}
    chain.set_sender(OWNER)
    assert codec.decode(chain.call(actor.reject, codec.encode({"proposal_id": 1}))) == {"status": "rejected", "proposal_id": 1}
    proposal = codec.decode(chain.call(actor.get_proposal, codec.encode({"proposal_id": 1})))
    assert proposal["status"] == "rejected"
    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["rejected_count"] == 1
    missing = codec.decode(chain.call(actor.get_proposal, codec.encode({"proposal_id": 7})))
    assert missing["status"] == "missing"


def test_approve_is_owner_gated() -> None:
    actor, chain = _deployed()
    chain.call(actor.submit_proposal, codec.encode({"title": "Grants"}))
    chain.set_sender(STRANGER)
    assert codec.decode(chain.call(actor.approve, codec.encode({"proposal_id": 1}))) == {"error": "UNAUTHORIZED"}
    chain.set_sender(OWNER)
    assert codec.decode(chain.call(actor.approve, codec.encode({"proposal_id": 1}))) == {"status": "approved", "proposal_id": 1}
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats["approved_count"] == 1
    assert chain.events()[-1][0] == "capstone.approved"
