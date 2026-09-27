"""Lower route-intent.json to what the Gateway routes by.

Two outputs, one per serving shape:

- `--gateway-toml`: CIP-15 §8.15 workload routes for the Gateway operator's
  config. Every intended route becomes a `[[workload_routes]]` entry whose
  target names the actor's CIP-10 workload; the Gateway resolves the serving
  endpoint from the registry record on chain and relays the caller's request.

      python3 install_routes.py --gateway-toml --actor 0x<actor> --workload-id <64 hex> --out workload-routes.toml

- `--runner`: the CIP-15 §6.5 Routes table with Runner targets, the raw CBOR
  blob the actor's owner installs with `set_routes`.

      python3 install_routes.py --runner 0x<runner address> --out routes.cbor
      cowboy actor execute --actor <actor> --handler set_routes --payload @routes.cbor --private-key <owner>
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROUTES_VERSION = 1
MAX_ROUTES_BYTES = 64 * 1024
VERBS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def gateway_path(path: str) -> str:
    """route-intent writes `:id`; the Gateway router captures `{id}`."""
    if not path.startswith("/") or "//" in path:
        raise ValueError(f"bad route path {path!r}")
    out = []
    for segment in path.split("/"):
        if segment.startswith(":"):
            name = segment[1:]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"bad path parameter {segment!r}")
            segment = "{" + name + "}"
        elif "{" in segment or "}" in segment or ":" in segment:
            raise ValueError(f"bad path segment {segment!r}")
        out.append(segment)
    return "/".join(out)


def _intent_routes(intent: dict):
    if intent.get("format") != "inference-route-intent-v1" or intent.get("target_kind") != "runner_route":
        raise ValueError("unsupported route intent")
    seen = set()
    for row in intent["routes"]:
        verb = row["verb"].upper()
        if verb not in VERBS:
            raise ValueError(f"unsupported verb {row['verb']!r}")
        path = gateway_path(row["path"])
        if (verb, path) in seen:
            raise ValueError(f"duplicate route {verb} {path}")
        seen.add((verb, path))
        if row["auth"] not in ("public", "account"):
            raise ValueError(f"unsupported auth {row['auth']!r}")
        handler = row["implementation"]
        if not isinstance(handler, str) or not handler or len(handler) > 128:
            raise ValueError("handler name must be nonempty text of at most 128 bytes")
        yield verb, path, row["auth"], handler


ENTRY = """
[[workload_routes]]
actor = "{actor}"
verb = "{verb}"
path = "{path}"
pays = "actor"
priority = 0
enabled = true
[workload_routes.target]
kind = "workload"
workload = "{workload}"
strip_prefix = false
auth = "{auth}"
idle_timeout_ms = {idle_timeout_ms}"""


def workload_toml(intent: dict, *, actor: str, workload_id: str, idle_timeout_ms: int) -> str:
    """`[[workload_routes]]` entries for the Gateway config (CIP-15 §8.15)."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", actor):
        raise ValueError("actor must be a 0x-prefixed 20-byte address")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", workload_id):
        raise ValueError("workload id must be 32 bytes of hex")
    if not 1 <= idle_timeout_ms <= 3_600_000:
        raise ValueError("idle_timeout_ms must be within 1..3600000")
    out = [f"# {actor.lower()} routes served by workload {workload_id.lower()} (generated from route-intent.json)"]
    for verb, path, auth, _ in _intent_routes(intent):
        out.append(ENTRY.format(actor=actor[2:].lower(), verb=verb, path=path, workload=workload_id.lower(),
                                auth=auth, idle_timeout_ms=idle_timeout_ms))
    return "\n".join(out) + "\n"


def lower(intent: dict, *, runner: str, timeout_ms: int) -> dict:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", runner):
        raise ValueError("runner must be a 0x-prefixed 20-byte address")
    if not 1 <= timeout_ms <= 300_000:
        raise ValueError("timeout_ms must be within 1..300000")
    routes = []
    for verb, path, auth, handler in _intent_routes(intent):
        routes.append({
            "verb": verb,
            "path": path,
            "target": {"kind": "runner", "handler": handler, "auth": auth,
                       "affinity": "none", "runner": runner.lower(), "timeout_ms": timeout_ms},
            "pays": "actor",
            "priority": 0,
            "enabled": True,
        })
    return {"version": ROUTES_VERSION, "routes": routes}


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * width):
            return bytes([major << 5 | info]) + n.to_bytes(width, "big")
    raise ValueError("length too large")


def cbor(value) -> bytes:
    """Canonical CBOR for the table (RFC 8949 §4.2.1: shortest ints, maps
    keyed shortest-first then bytewise). offchain.cbor is the wire subset for
    claim keys and tokens and deliberately has no maps or bools."""
    if value is True or value is False:
        return b"\xf5" if value else b"\xf4"
    if isinstance(value, int):
        if value < 0:
            raise ValueError("negative integers are not used in the table")
        return _head(0, value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, list):
        return _head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        items = sorted(((cbor(k), cbor(v)) for k, v in value.items()), key=lambda kv: (len(kv[0]), kv[0]))
        return _head(5, len(items)) + b"".join(k + v for k, v in items)
    raise TypeError(f"unsupported value {type(value).__name__}")


def encode(table: dict) -> bytes:
    raw = cbor(table)
    if len(raw) > MAX_ROUTES_BYTES:
        raise ValueError("routes table exceeds the Gateway's 64 KiB limit")
    return raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--intent", type=Path, default=Path(__file__).with_name("route-intent.json"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--gateway-toml", action="store_true", help="emit [[workload_routes]] for the Gateway config")
    mode.add_argument("--runner", help="serving runner address (route pin) for the on-chain Routes table")
    parser.add_argument("--actor", help="actor address (workload routes)")
    parser.add_argument("--workload-id", help="CIP-10 workload id, 32 bytes hex (workload routes)")
    parser.add_argument("--timeout-ms", type=int, default=120_000, help="runner: Gateway wait before 504; workload: relay idle timeout")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    intent = json.loads(args.intent.read_text())
    if args.gateway_toml:
        if not args.actor or not args.workload_id:
            parser.error("--gateway-toml needs --actor and --workload-id")
        text = workload_toml(intent, actor=args.actor, workload_id=args.workload_id, idle_timeout_ms=args.timeout_ms)
        args.out.write_text(text)
        print(json.dumps({"routes": text.count("[[workload_routes]]"), "bytes": args.out.stat().st_size,
                          "paths": [f"{v} {p}" for v, p, _, _ in _intent_routes(intent)]}, indent=1))
        return 0
    table = lower(intent, runner=args.runner, timeout_ms=args.timeout_ms)
    args.out.write_bytes(encode(table))
    print(json.dumps({"routes": len(table["routes"]), "bytes": args.out.stat().st_size,
                      "paths": [f"{r['verb']} {r['path']}" for r in table["routes"]]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
