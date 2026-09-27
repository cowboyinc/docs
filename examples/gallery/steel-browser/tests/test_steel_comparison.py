#!/usr/bin/env python3
"""Live direct-vs-wrapper differential for the exposed Steel routes.

UNRUN in this build: it needs a real STEEL_API_KEY and spends Steel credits (it
renders one screenshot and one PDF of a caller-supplied URL). The wrapper side
uses the real SDK lifecycle: submit the handler, take the exact job it composed,
replay that request directly with the key, resume the continuation, and read the
request-specific result as the registered hirer, asserting exact JSON equality.

Standalone scripts must load ``cowboy_sdk`` by an explicit importlib spec — NOT
``sys.path.insert(0, <pvm/Lib>)`` — because that Lib carries a RustPython
``threading`` shim that crashes a fresh CPython process.
"""

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SDK = Path(os.environ.get("COWBOY_SDK_PATH", PROJECT.parents[3] / "node/pvm/Lib")).resolve()
if "pvm_host" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "pvm_host", SDK / "cowboy_sdk/tests/mock_pvm_host.py"
    )
    host_mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host_mock)
    sys.modules["pvm_host"] = host_mock
else:
    host_mock = sys.modules["pvm_host"]
if "cowboy_sdk" not in sys.modules:
    package = SDK / "cowboy_sdk"
    spec = importlib.util.spec_from_file_location(
        "cowboy_sdk", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    sdk = importlib.util.module_from_spec(spec)
    sys.modules["cowboy_sdk"] = sdk
    spec.loader.exec_module(sdk)
sys.path.insert(0, str(PROJECT))
import actors.steel.main as steel
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("STEEL_API_KEY_FILE", "/private/tmp/steel-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


def direct(kwargs, key):
    headers = dict(kwargs["headers"])
    headers[steel.AUTH_HEADER] = key  # swap the ${STEEL_API_KEY} reference for the real key
    data = kwargs.get("raw_body") if kwargs["method"] == "POST" else None
    request = urllib.request.Request(
        kwargs["url"], data=data, method=kwargs["method"], headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = steel.SteelBrowserWrapper()
    actor.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    actor.register_hire(
        codec.encode(
            {
                "hire_id": 42,
                "hirer": "0x" + BUYER.hex(),
                "declaration_id": "0x" + "55" * 32,
                "version": 3,
                "artifact_id": 7,
            }
        )
    )
    return actor


def wrapped(actor, key, handler, payload, tx):
    host_mock.set_context(sender=BUYER, tx_hash=(tx).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
    assert accepted["ok"] and accepted["status"] == "pending", accepted
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    if kwargs["method"] == "GET":
        assert "body" not in kwargs and "raw_body" not in kwargs, "GET must omit body"
    assert kwargs["headers"][steel.AUTH_HEADER] == "${STEEL_API_KEY}"
    status, vendor = direct(kwargs, key)
    host_mock.set_context(sender=ACTOR)
    steel._run_request__resume(
        codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"], "result": vendor})
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(
        actor.get_result(codec.encode({"request_id": accepted["request_id"], "hire_id": 42}))
    )
    return status, vendor, result["output"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--url", default="https://example.com")
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    actor = fresh_actor()

    for tx, handler in ((1, "screenshot"), (2, "pdf")):
        status, vendor, out = wrapped(actor, key, handler, {"url": args.url}, tx=tx)
        assert status == 200 and out["ok"] and out["data"] == vendor, out
        assert isinstance(out["data"]["url"], str)
        print("PASS %-10s url=%s (%d bytes)" % (handler, out["data"]["url"], len(steel._json_bytes(out))))

    print("PASS: exposed Steel routes (screenshot, pdf) match direct calls exactly")


if __name__ == "__main__":
    main()
