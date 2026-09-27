"""Emit the CIP-15 routes table for the ranchhand actor.

Produces a CBOR Routes blob compatible with `gateway-types::Routes`:
  Routes { version: 1, routes: Vec<Route> }
  Route  { verb, path, target, pays, priority, enabled, [mode], [timeout_ms], [fallback_response] }

Run with system Python (cbor2 needs system libs unavailable in PVM):
    python3 tools/emit_routes.py   # writes routes.cbor next to it

Then install on the actor:
    cowboy actor execute \
        --address $RANCHHAND_ACTOR \
        --handler set_routes \
        --payload @tools/routes.cbor \
        --private-key $RANCHHAND_PRIVATE_KEY \
        --rpc-url http://127.0.0.1:4002
"""

import pathlib

import cbor2


HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "routes.cbor"


def route(verb, path, handler):
    """Build one Route dict with all serde-required fields populated."""
    return {
        "verb": verb,                                       # UPPERCASE
        "path": path,
        "target": {"kind": "method", "name": handler},      # lowercase tag
        "pays": "actor",                                    # lowercase
        "priority": 0,
        "enabled": True,
    }


def canonical_sort(routes):
    """CIP-15 §6.5 order: priority desc, path desc, verb asc."""
    verb_order = {"GET": 0, "HEAD": 1, "POST": 2, "PUT": 3, "PATCH": 4, "DELETE": 5, "ANY": 6}
    return sorted(
        routes,
        key=lambda r: (-r["priority"], [-ord(c) for c in r["path"]], verb_order[r["verb"]]),
    )


def main():
    routes = [
        route("GET", "/health", "health"),
        route("POST", "/slack/events", "slack_http"),
    ]
    table = {"version": 1, "routes": canonical_sort(routes)}
    blob = cbor2.dumps(table, canonical=True)
    OUT.write_bytes(blob)
    print(f"wrote {OUT} ({len(blob)} bytes, {len(routes)} routes)")


if __name__ == "__main__":
    main()
