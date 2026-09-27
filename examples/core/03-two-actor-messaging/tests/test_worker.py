import _sdk_path  # noqa: F401  (examples/_sdk_path.py, on pythonpath via pyproject.toml)
from cowboy_sdk import codec
from cowboy_sdk.testing import SimulatedChain

from actors.worker.main import WorkerActor


def test_init_starts_with_no_tasks() -> None:
    actor = WorkerActor()
    chain = SimulatedChain(actor)
    assert codec.decode(chain.call(actor.init, b"")) == {"status": "initialized"}
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("worker.init", {"status": "ready"})
    assert codec.decode(chain.call(actor.get_tasks, codec.encode({}))) == {"handled_count": 0, "tasks": {}}


def test_handle_message_records_task() -> None:
    actor = WorkerActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    task = {"task_id": 1, "title": "Prepare launch checklist", "details": "Confirm wiring.", "sender": "11" * 20}
    result = codec.decode(chain.call(actor.handle_message, codec.encode(task)))
    assert result == {"status": "handled", "task_id": 1}
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("worker.task_handled", {"task_id": 1, "title": "Prepare launch checklist"})
    tasks = codec.decode(chain.call(actor.get_tasks, codec.encode({})))
    assert tasks["handled_count"] == 1
    assert tasks["tasks"][1] == dict(task, status="handled")


def test_handle_message_defaults_task_id_to_handled_count() -> None:
    actor = WorkerActor()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    chain.call(actor.handle_message, codec.encode({"title": "a"}))
    chain.call(actor.handle_message, codec.encode({"title": "b"}))
    tasks = codec.decode(chain.call(actor.get_tasks, codec.encode({})))
    assert tasks["handled_count"] == 2
    assert sorted(tasks["tasks"]) == [1, 2]
