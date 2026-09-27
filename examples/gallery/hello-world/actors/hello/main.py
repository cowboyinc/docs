"""
Hello World — the smallest store-publishable Cowboy actor.

Exists to exercise the General Store pipeline end to end (Labs publish →
review → approve → Live → hire → instance deploy) with no moving parts:
no secrets, no volumes, no runner. Everything a hired instance needs is
installed by `init`, which tolerates an empty payload — the store's
instance-deploy leg calls `init` with `{}` and the hirer becomes owner.

Routes (lowered to `__cowboy/routes` by the deploy CLI from the
decorators below, CIP-15 §6):

  GET  /        HTML greeting page (read-only)
  POST /hello   increments a counter, returns JSON

Owner knob: `set_greeting` — lets a hirer personalize their instance and
gives the store's post-hire owner path something to test.
"""

import json

from cowboy_sdk import OWNER, actor, callable_by, codec, public, pure, runtime
from cowboy_sdk.routes import get, post

KEY_OWNER = "owner"
KEY_GREETING = "greeting"
KEY_HELLO_COUNT = "hello_count"

DEFAULT_GREETING = "Hello, world!"
MAX_GREETING_CHARS = 200


def _http_resp(status, headers, body):
    """Return CBOR-encoded HttpResponseEnvelope bytes (CIP-14)."""
    if body is None:
        body = b""
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return codec.encode({
        "status": int(status),
        "headers": list(headers or []),
        "body": body,
    })


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@actor
class HelloWorld:
    def init(self, payload):
        owner = runtime.assume_ownership_if_unowned()
        self.storage[KEY_OWNER] = owner.hex()
        self.storage[KEY_GREETING] = DEFAULT_GREETING
        self.storage[KEY_HELLO_COUNT] = 0
        runtime.emit_event("hello_world.init", {KEY_OWNER: owner.hex()})
        return b"ok"

    # ── Gateway-facing handlers (CIP-14 / CIP-15) ─────────────────────────

    @public
    @get("/")
    @pure
    def home(self, payload):
        """GET / — greeting page. Read-only; no chain mutation."""
        greeting = _escape(self.storage.get(KEY_GREETING, DEFAULT_GREETING))
        count = int(self.storage.get(KEY_HELLO_COUNT, 0))
        html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Hello World</title></head>"
            f"<body><h1>{greeting}</h1>"
            f"<p>Said hello {count} time{'' if count == 1 else 's'}.</p>"
            "<p>POST /hello to say it again.</p></body></html>"
        )
        return _http_resp(200, [("content-type", "text/html; charset=utf-8")], html)

    @public
    @post("/hello")
    def hello(self, payload):
        """POST /hello — the one mutating route: bump the counter."""
        count = int(self.storage.get(KEY_HELLO_COUNT, 0)) + 1
        self.storage[KEY_HELLO_COUNT] = count
        greeting = self.storage.get(KEY_GREETING, DEFAULT_GREETING)
        runtime.emit_event("hello_world.hello", {"count": count})
        body = json.dumps({"greeting": greeting, "count": count})
        return _http_resp(200, [("content-type", "application/json")], body)

    # ── Chain-facing handlers ─────────────────────────────────────────────

    @callable_by(OWNER, decode="cbor")
    def set_greeting(self, payload):
        """Owner-only: replace the greeting."""
        greeting = str((payload or {}).get(KEY_GREETING, "")).strip()
        if not greeting or len(greeting) > MAX_GREETING_CHARS:
            raise ValueError(
                f"greeting must be 1..{MAX_GREETING_CHARS} characters"
            )
        self.storage[KEY_GREETING] = greeting
        runtime.emit_event("hello_world.greeting_set", {KEY_GREETING: greeting})
        return b"ok"

    @pure
    @public(decode="cbor")
    def get_stats(self, payload):
        return {
            KEY_OWNER: self.storage.get(KEY_OWNER, ""),
            KEY_GREETING: self.storage.get(KEY_GREETING, DEFAULT_GREETING),
            KEY_HELLO_COUNT: int(self.storage.get(KEY_HELLO_COUNT, 0)),
        }


def _get_actor():
    return HelloWorld()


def init(payload):
    return _get_actor().init(payload)


def home(payload):
    return _get_actor().home(payload)


def hello(payload):
    return _get_actor().hello(payload)


def set_greeting(payload):
    return _get_actor().set_greeting(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
