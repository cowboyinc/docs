"""Owner configuration for the shared inference actor.

This is the PVM portion only. HTTP executes in offchain/, after native route
admission. No route installer exists until the shared RunnerRoute builder is
available; see route-intent.json and MANAGEMENT.md.
"""

from cowboy_sdk import OWNER, actor, callable_by, runtime

SLOTS_KEY = "inference/slots"
MAX_SLOTS = 128
# CIP-15 §6.5: the Gateway reads this reserved KV slot as a raw CBOR Routes
# blob. The deploy CLI writes it from @route/mount decorations, which cannot
# express a Runner target, so the owner installs the lowered table directly
# (install_routes.py builds it). 64 KiB is the Gateway's MAX_ROUTES_SIZE.
ROUTES_KEY = "__cowboy/routes"
MAX_ROUTES_BYTES = 64 * 1024


def _text(value, label, limit):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(label + " must be nonempty bounded text")
    return value


def _uint(value, label, minimum=0, maximum=(1 << 64) - 1):
    # isinstance, not `type(value) is int`: inside the PVM sandbox the `int`
    # the actor module sees is not the object the decoder's ints are made from,
    # so an identity check refuses every integer (proved on a live validator).
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(label + " is out of range")
    return value


def _slot_config(payload):
    fields = ["active_key_cap", "artifact_id", "declaration_id", "hire_id",
              "public_base_url", "slot", "version", "volume_name", "volume_owner"]
    if not isinstance(payload, dict) or sorted(payload) != fields:
        raise ValueError("configure_slot requires exactly the documented fields")
    slot = _text(payload["slot"], "slot", 128)
    owner = _text(payload["volume_owner"], "volume_owner", 40)
    if len(owner) != 40 or any(char not in "0123456789abcdef" for char in owner):
        raise ValueError("volume_owner must be lowercase hex for 20 bytes, without 0x")
    name = _text(payload["volume_name"], "volume_name", 128)
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in name) or name in (".", ".."):
        raise ValueError("invalid volume_name")
    base = _text(payload["public_base_url"], "public_base_url", 2048)
    if not base.startswith(("https://", "http://")) or any(char in base for char in "?#") or base.endswith("/"):
        raise ValueError("public_base_url must be an HTTP(S) base without query, fragment or trailing slash")
    if any(ord(char) <= 32 or ord(char) >= 127 for char in base):
        raise ValueError("public_base_url must be an ASCII URI")
    authority = base.split("://", 1)[1].split("/", 1)[0]
    if not authority or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]" for char in authority):
        raise ValueError("public_base_url requires a host and must not contain credentials")
    return slot, {
        "hire_id": _uint(payload["hire_id"], "hire_id"),
        "declaration_id": _text(payload["declaration_id"], "declaration_id", 128),
        "version": _uint(payload["version"], "version", minimum=1),
        "artifact_id": _uint(payload["artifact_id"], "artifact_id"),
        "volume_owner": owner, "volume_name": name, "public_base_url": base,
        "active_key_cap": _uint(payload["active_key_cap"], "active_key_cap", minimum=1, maximum=100),
    }


@actor
class InferenceActor:
    def init(self, payload):
        # `cowboy actor deploy` sends its default init payload `{}` (JSON bytes
        # or the empty CBOR map); none of these carry configuration.
        if payload not in (None, b"", b"{}", b"\xa0"):
            raise ValueError("init takes no configuration; use configure_slot")
        runtime.assume_ownership_if_unowned()
        self.storage[SLOTS_KEY] = {}
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def configure_slot(self, payload):
        """Bind public deployment metadata. Retained Hire remains the authority.

        This stores no hirer claim, credential, signing key, DEK or usage.
        The offchain adapter reads this from authenticated finalized actor state.
        """
        slot, config = _slot_config(payload)
        slots = dict(self.storage.get(SLOTS_KEY, {}))
        if slot not in slots and len(slots) >= MAX_SLOTS:
            raise ValueError("slot limit reached")
        slots[slot] = config
        self.storage[SLOTS_KEY] = slots
        runtime.emit_event("inference.slot_configured", {"slot": slot, "hire_id": config["hire_id"]})
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_routes(self, payload):
        """Install the CBOR Routes table verbatim (no SDK wrapping)."""
        if not isinstance(payload, (bytes, bytearray)) or not payload or len(payload) > MAX_ROUTES_BYTES:
            raise ValueError("set_routes takes a nonempty CBOR Routes blob of at most 64 KiB")
        self.storage.set_raw(ROUTES_KEY, bytes(payload))
        return b"ok"


def init(payload):
    return InferenceActor().init(payload)


def configure_slot(payload):
    return InferenceActor().configure_slot(payload)


def set_routes(payload):
    return InferenceActor().set_routes(payload)
