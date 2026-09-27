#!/usr/bin/env python3
"""Live direct-versus-wrapper Modal invoke differential.

Invokes a real deployed Modal endpoint, then feeds that exact reply through
the wrapper lifecycle and requires exact output equality.
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
FIXTURE = json.loads((PROJECT / "modal_routes.json").read_text(encoding="utf-8"))
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

import actors.modal.main as modal_compute
from cowboy_sdk import codec


OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
LABEL = "compute"
DEFAULT_PAYLOAD = {"n": 21}


def expected_body(payload):
    return dict(payload or {})


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def http(url, body, token_id, token_secret):
    request = urllib.request.Request(
        url,
        data=json_bytes(body),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            FIXTURE["auth"]["key_header"]: token_id,
            FIXTURE["auth"]["secret_header"]: token_secret,
            "User-Agent": modal_compute.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def fresh_actor(url):
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = modal_compute.ModalComputeWrapper()
    actor.init(
        codec.encode(
            {
                "store_registrar": "0x" + OWNER.hex(),
                "endpoints": [{"label": LABEL, "url": url, "method": "POST"}],
            }
        )
    )
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


def wrapped(url, token_id, token_secret, payload):
    actor = fresh_actor(url)
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(
        actor.invoke(codec.encode({"hire_id": 42, "endpoint": LABEL, "payload": payload}))
    )
    assert accepted["ok"] is True and accepted["endpoint"] == LABEL
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    assert kwargs["url"] == url
    assert kwargs["method"] == "POST"
    assert kwargs["raw_body"] == json_bytes(expected_body(payload))
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"][FIXTURE["auth"]["key_header"]] == "${MODAL_TOKEN_ID}"
    assert kwargs["headers"][FIXTURE["auth"]["secret_header"]] == "${MODAL_TOKEN_SECRET}"
    assert kwargs["secrets"] == [
        ["0x" + OWNER.hex(), "MODAL_TOKEN_ID"],
        ["0x" + OWNER.hex(), "MODAL_TOKEN_SECRET"],
    ]
    status, reply = http(url, expected_body(payload), token_id, token_secret)
    assert status == 200, "Modal returned HTTP %d: %r" % (status, reply)
    assert isinstance(reply, dict) and reply
    host_mock.set_context(sender=ACTOR)
    modal_compute._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}
        )
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == "invoke" and result["status"] == "ok"
    assert result["output"]["data"] == reply
    return result, reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="deployed https://<ws>--<fn>.modal.run URL")
    parser.add_argument("--token-id-file", default=os.environ.get("MODAL_TOKEN_ID_FILE"))
    parser.add_argument("--token-secret-file", default=os.environ.get("MODAL_TOKEN_SECRET_FILE"))
    args = parser.parse_args()
    token_id = open(args.token_id_file, encoding="utf-8").read().strip()
    token_secret = open(args.token_secret_file, encoding="utf-8").read().strip()
    result, reply = wrapped(args.url, token_id, token_secret, DEFAULT_PAYLOAD)
    print("PASS: exact Modal invoke wrapper lifecycle (reply keys %s)" % sorted(reply))


if __name__ == "__main__":
    main()
