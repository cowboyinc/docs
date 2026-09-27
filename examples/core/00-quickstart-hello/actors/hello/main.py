import json
from cowboy_sdk import actor, pure, runtime, public


@actor
class HelloActor:
    def init(self, payload):
        data = json.loads(payload) if payload else {}
        self.storage["greeting"] = str(data.get("greeting", "hello"))
        self.storage["last_message"] = {"status": "initialized"}
        runtime.emit_event("hello.init", {"greeting": self.storage["greeting"]})
        return b"ok: initialized"

    @public(decode="cbor")
    def say_hello(self, payload):
        data = payload or {}
        name = str(data.get("name", "Cowboy"))
        message = {
            "greeting": self.storage.get("greeting", "hello"),
            "name": name,
            "message": f"{self.storage.get('greeting', 'hello')}, {name}",
        }
        self.storage["last_message"] = message
        runtime.emit_event("hello.message", message)
        return message

    @pure
    @public(decode="cbor")
    def get_greeting(self, payload):
        return self.storage.get("last_message", {"status": "idle"})


def _get_actor():
    return HelloActor()


def init(payload):
    return _get_actor().init(payload)


def say_hello(payload):
    return _get_actor().say_hello(payload)


def get_greeting(payload):
    return _get_actor().get_greeting(payload)
