import _sdk_path  # noqa: F401  (examples/_sdk_path.py, on pythonpath via pyproject.toml)
from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.sender.main import SenderActor

WORKER = "0x" + "aa" * 20


def test_init_starts_unconfigured() -> None:
    actor = SenderActor()
    chain = SimulatedChain(actor)
    assert codec.decode(chain.call(actor.init, b"{}")) == {"status": "initialized", "worker_address": ""}
    state = codec.decode(chain.call(actor.get_state, codec.encode({})))
    assert state == {"worker_address": "", "sent_count": 0}


def test_send_task_requires_configuration() -> None:
    actor = SenderActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    result = codec.decode(chain.call(actor.send_task, codec.encode({"title": "x"})))
    assert result == {"error": "NOT_CONFIGURED"}
    assert mock_host.get_send_log() == []


def test_send_task_sends_deferred_message_to_worker() -> None:
    actor = SenderActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"{}")
    configured = codec.decode(chain.call(actor.configure, codec.encode({"worker_address": WORKER})))
    assert configured == {"status": "configured", "worker_address": WORKER}
    result = codec.decode(chain.call(actor.send_task, codec.encode({"title": "Prepare launch checklist", "details": "Confirm wiring."})))
    assert result == {"status": "sent", "task_id": 1}
    (message,) = mock_host.get_send_log()
    assert message["target"] == bytes.fromhex(WORKER[2:])
    assert codec.decode(message["payload"]) == {
        "task_id": 1,
        "title": "Prepare launch checklist",
        "details": "Confirm wiring.",
        "sender": "11" * 20,
    }
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("sender.task_sent", {"task_id": 1, "title": "Prepare launch checklist"})
    state = codec.decode(chain.call(actor.get_state, codec.encode({})))
    assert state == {"worker_address": WORKER, "sent_count": 1}
