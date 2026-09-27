import json
from cowboy_sdk import actor, deferred, pure, runtime, send, public, codec


@actor
class SenderActor:
    def init(self, payload):
        self.storage["__name__"] = "Task Sender"
        self.storage["__description__"] = "Two-actor messaging sender"
        self.storage["__version__"] = "1.0.0"
        data = json.loads(payload) if payload else {}
        self.storage["owner"] = runtime.get_sender()
        self.storage["worker_address"] = str(data.get("worker_address", ""))
        self.storage["sent_count"] = 0
        runtime.emit_event("sender.init", {"worker_address": self.storage["worker_address"]})
        return codec.encode({"status": "initialized", "worker_address": self.storage["worker_address"]})

    @pure
    @public(decode="cbor")
    def configure(self, payload):
        data = payload or {}
        worker_address = str(data.get("worker_address", ""))
        if not worker_address:
            return {"error": "MISSING_WORKER_ADDRESS"}
        self.storage["worker_address"] = worker_address
        runtime.emit_event("sender.configured", {"worker_address": worker_address})
        return {"status": "configured", "worker_address": worker_address}

    @deferred
    @public(decode="cbor")
    def send_task(self, payload):
        data = payload or {}
        worker_address = self.storage.get("worker_address", "")
        if not worker_address:
            return {"error": "NOT_CONFIGURED"}

        sent_count = int(self.storage.get("sent_count", 0)) + 1
        self.storage["sent_count"] = sent_count
        task = {
            "task_id": sent_count,
            "title": str(data.get("title", "Review task")),
            "details": str(data.get("details", "")),
            "sender": runtime.get_sender().hex(),
        }
        send(worker_address, codec.encode(task), raw=True)
        runtime.emit_event("sender.task_sent", {"task_id": sent_count, "title": task["title"]})
        return {"status": "sent", "task_id": sent_count}

    @pure
    @public(decode="cbor")
    def get_state(self, payload):
        return {
            "worker_address": self.storage.get("worker_address", ""),
            "sent_count": int(self.storage.get("sent_count", 0)),
        }


def _get_actor():
    return SenderActor()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def send_task(payload):
    return _get_actor().send_task(payload)


def get_state(payload):
    return _get_actor().get_state(payload)
