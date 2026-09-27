from cowboy_sdk import actor, deferred, pure, runtime, public, codec


@actor
class WorkerActor:
    def init(self, payload):
        self.storage["__name__"] = "Task Worker"
        self.storage["__description__"] = "Two-actor messaging worker"
        self.storage["__version__"] = "1.0.0"
        self.storage["tasks"] = {}
        self.storage["handled_count"] = 0
        runtime.emit_event("worker.init", {"status": "ready"})
        return codec.encode({"status": "initialized"})

    @deferred
    @public(decode="cbor")
    def handle_message(self, payload):
        data = payload or {}
        handled_count = int(self.storage.get("handled_count", 0)) + 1
        self.storage["handled_count"] = handled_count
        task_id = int(data.get("task_id", handled_count))
        task = {
            "task_id": task_id,
            "title": str(data.get("title", "")),
            "details": str(data.get("details", "")),
            "sender": str(data.get("sender", "")),
            "status": "handled",
        }
        tasks = dict(self.storage.get("tasks") or {})
        tasks[task_id] = task
        self.storage["tasks"] = tasks
        runtime.emit_event("worker.task_handled", {"task_id": task_id, "title": task["title"]})
        return {"status": "handled", "task_id": task_id}

    @pure
    @public(decode="cbor")
    def get_tasks(self, payload):
        return {
            "handled_count": int(self.storage.get("handled_count", 0)),
            "tasks": dict(self.storage.get("tasks") or {}),
        }


def _get_actor():
    return WorkerActor()


def init(payload):
    return _get_actor().init(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def get_tasks(payload):
    return _get_actor().get_tasks(payload)
