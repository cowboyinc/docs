"""Reusable helpers for building a CIP-15 routes table (incl. ``Target::Runner``).

Today every example hand-rolls the route dicts in its own ``tools/emit_routes.py``,
and only knows about ``Target::Method``. This module is the shared, correct way to
declare routes — including the off-chain ``Target::Runner`` target — so actors can
expose runner-served endpoints without re-deriving the wire shape.

The dicts match ``gateway-types`` serde exactly:
  * ``verb``   is UPPERCASE (GET/HEAD/POST/PUT/PATCH/DELETE/ANY)
  * ``target`` is internally tagged on ``kind`` = method | volume | runner (lowercase)
  * ``pays`` / ``auth`` / ``affinity`` / mount ``mode`` / ``owner`` are lowercase

Emit + install::

    from cip15_routes import runner_route, method_route, dump_routes
    routes = [
        method_route("POST", "/greet", "greet"),
        runner_route("/chat", handler="chat", auth="account",
                     mounts=[mount("memory", mode="rw", owner="caller")]),
    ]
    dump_routes(routes, "routes.cbor")        # canonical CBOR Routes blob

    # cowboy actor execute --handler set_routes --payload @routes.cbor ...

The client-side checks here mirror a subset of gateway ``Route::validate()``
for fast local failure (auth/affinity/mount coupling, pays/price coupling,
timeout bound, reserved paths, volume-target payment); the gateway remains
authoritative and re-validates every table it decodes.
"""

from __future__ import annotations

import functools
import pathlib
from typing import Optional

import cbor2

VERB_ORDER = {"GET": 0, "HEAD": 1, "POST": 2, "PUT": 3, "PATCH": 4, "DELETE": 5, "ANY": 6}
ROUTES_VERSION = 1
MAX_TIMEOUT_MS = 120_000  # gateway Route::validate() upper bound


def _route(verb, path, target, *, pays="actor", price=None, priority=0, enabled=True):
    verb = verb.upper()
    if verb not in VERB_ORDER:
        raise ValueError(f"bad verb {verb!r}")
    if path == "/_cowboy" or path.startswith("/_cowboy/"):
        raise ValueError("'/_cowboy/*' paths are reserved by the gateway")
    if pays not in ("actor", "caller"):
        raise ValueError(f"pays must be 'actor' or 'caller', got {pays!r}")
    if pays == "caller" and not price:
        raise ValueError("pays='caller' requires a price (CIP-18)")
    if pays == "actor" and price is not None:
        raise ValueError("pays='actor' must not set a price (gateway ActorPaysHasPrice)")
    r = {
        "verb": verb,
        "path": path,
        "target": target,
        "pays": pays,
        "priority": priority,
        "enabled": enabled,
    }
    if price is not None:
        # gateway `price` is Option<String>; a CBOR int fails decode
        r["price"] = str(price)
    return r


def method_route(verb, path, handler, **kw):
    """A route handled on-chain by the actor's PVM (``Target::Method``)."""
    return _route(verb, path, {"kind": "method", "name": handler}, **kw)


def volume_route(verb, path, volume_name, *, strip_prefix=True, volume_path_prefix="", **kw):
    """A route served as static CBFS content (``Target::Volume``)."""
    if kw.get("pays", "actor") != "actor":
        raise ValueError("Target::Volume routes require pays='actor'")
    target = {
        "kind": "volume",
        "volume_name": volume_name,
        "strip_prefix": strip_prefix,
        "volume_path_prefix": volume_path_prefix,
    }
    return _route(verb, path, target, **kw)


def mount(volume, *, mode="ro", owner="actor", path_prefix=""):
    """A CBFS mount granted to the runner on a ``Target::Runner`` route."""
    if mode not in ("ro", "rw"):
        raise ValueError("mount mode must be 'ro' or 'rw'")
    if owner not in ("actor", "caller"):
        raise ValueError("mount owner must be 'actor' or 'caller'")
    return {"volume": volume, "mode": mode, "owner": owner, "path_prefix": path_prefix}


def runner_route(
    path,
    *,
    handler,
    verb="POST",
    auth="public",
    affinity="none",
    runner: Optional[str] = None,
    requires: Optional[dict] = None,
    mounts: Optional[list] = None,
    timeout_ms=60_000,
    **kw,
):
    """A route proxied off-chain to a runner (``Target::Runner``).

    ``auth='account'`` authenticates the caller (any account, as itself);
    ``mounts`` with ``owner='caller'`` give that caller scoped access to their own
    CBFS volume and REQUIRE ``auth='account'``. ``affinity='caller'`` (sticky per
    caller) also requires ``auth='account'``.
    """
    if auth not in ("public", "account"):
        raise ValueError("auth must be 'public' or 'account'")
    if affinity not in ("none", "caller", "session"):
        raise ValueError("affinity must be 'none', 'caller' or 'session'")
    if not 0 < timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be in 1..={MAX_TIMEOUT_MS}")
    mounts = mounts or []
    if affinity == "caller" and auth != "account":
        raise ValueError("affinity='caller' requires auth='account'")
    if any(m["owner"] == "caller" for m in mounts) and auth != "account":
        raise ValueError("owner='caller' mounts require auth='account'")
    target = {
        "kind": "runner",
        "handler": handler,
        "affinity": affinity,
        "auth": auth,
        "mounts": mounts,
        "timeout_ms": timeout_ms,
    }
    if requires is not None:
        target["requires"] = requires
    if runner is not None:
        target["runner"] = runner
    return _route(verb, path, target, **kw)


def _route_cmp(a, b):
    # priority desc
    if a["priority"] != b["priority"]:
        return -1 if a["priority"] > b["priority"] else 1
    # path desc — compared as UTF-8 bytes to match gateway String::cmp
    # (byte-lexicographic). A negated-per-char key would misorder
    # prefix-overlapping paths: desc puts /chat/history BEFORE /chat.
    pa, pb = a["path"].encode("utf-8"), b["path"].encode("utf-8")
    if pa != pb:
        return -1 if pa > pb else 1
    # verb asc
    va, vb = VERB_ORDER[a["verb"]], VERB_ORDER[b["verb"]]
    if va != vb:
        return -1 if va < vb else 1
    return 0


def canonical_sort(routes):
    """CIP-15 §6.5 order: priority desc, then path desc, then verb asc.

    Path order is byte-lexicographic descending over the UTF-8 bytes,
    byte-identical to ``gateway-types`` ``canonicalize()``'s
    ``b.path.cmp(&a.path)`` — including for prefix-overlapping paths.
    """
    return sorted(routes, key=functools.cmp_to_key(_route_cmp))


def routes_table(routes):
    return {"version": ROUTES_VERSION, "routes": canonical_sort(routes)}


def dump_routes(routes, out_path):
    """Write the canonical CBOR ``Routes`` blob to ``out_path`` and return it."""
    blob = cbor2.dumps(routes_table(routes), canonical=True)
    pathlib.Path(out_path).write_bytes(blob)
    return blob


if __name__ == "__main__":
    # Demo: a static UI, an on-chain handler, and two runner endpoints.
    demo = [
        volume_route("GET", "/", "site"),
        method_route("POST", "/greet", "greet"),
        runner_route("/ask", handler="ask"),  # public off-chain inference
        runner_route(
            "/chat",
            handler="chat",
            auth="account",  # any caller, authenticated as itself
            affinity="caller",
            mounts=[mount("memory", mode="rw", owner="caller")],
        ),
        # prefix-overlaps /chat: canonical desc order puts it FIRST
        runner_route("/chat/history", handler="chat_history", auth="account"),
    ]
    table = routes_table(demo)
    paths = [r["path"] for r in table["routes"]]
    assert paths.index("/chat/history") < paths.index("/chat"), paths
    blob = dump_routes(demo, "routes.cbor")
    print(f"wrote routes.cbor ({len(blob)} bytes, {len(demo)} routes)")
    print("order:", " ".join(paths))
