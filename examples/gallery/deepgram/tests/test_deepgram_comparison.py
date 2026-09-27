#!/usr/bin/env python3
"""Live direct-versus-wrapper Deepgram transcription differential: transcribes
one real audio URL through Deepgram, then requires the wrapper's extracted
transcript to match it.

Uses importlib (not sys.path.insert) to avoid the RustPython threading shim
that shadows the stdlib and crashes the interpreter.
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
FIXTURE = json.loads((PROJECT / "deepgram_routes.json").read_text(encoding="utf-8"))
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

import actors.deepgram.main as deepgram
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("DEEPGRAM_API_KEY_FILE", "/private/tmp/deepgram-key.txt")
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20
DEFAULT_URL = "https://dpgr.am/spacewalk.wav"
DEFAULT_MODEL = "nova-3"


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def http(listen_url, audio_url, key):
    request = urllib.request.Request(
        listen_url,
        data=json_bytes({"url": audio_url}),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": "Token " + key,
            "Content-Type": "application/json",
            "User-Agent": deepgram.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def vendor_transcript(reply):
    return reply["results"]["channels"][0]["alternatives"][0]["transcript"]


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = deepgram.DeepgramTranscribeWrapper()
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


def wrapped(key, audio_url, model):
    actor = fresh_actor()
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(
        actor.transcribe(codec.encode({"hire_id": 42, "url": audio_url, "model": model}))
    )
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": "transcribe",
        "status": "pending",
        "poll_handler": "get_result",
    }
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    assert kwargs["url"] == deepgram.LISTEN_ENDPOINT + "?model=" + model
    assert kwargs["method"] == FIXTURE["method"]
    assert json.loads(kwargs["raw_body"]) == {"url": audio_url}
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"]["Authorization"] == "Token ${DEEPGRAM_API_KEY}"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["User-Agent"] == deepgram.USER_AGENT
    assert "Idempotency-Key" not in kwargs["headers"]
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "DEEPGRAM_API_KEY"]]
    status, reply = http(kwargs["url"], audio_url, key)
    assert status == 200, "Deepgram returned HTTP %d: %r" % (status, reply)
    expected_transcript = vendor_transcript(reply)
    host_mock.set_context(sender=ACTOR)
    deepgram._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}
        )
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == "transcribe" and result["status"] == "ok"
    bounded = deepgram._truncate_text_bytes(expected_transcript, deepgram.TRANSCRIPT_BYTE_BUDGET)
    assert result["output"]["data"]["transcript"] == bounded
    return result, reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    result, reply = wrapped(key, args.url, args.model)
    request_id = reply.get("metadata", {}).get("request_id", "?")
    print("PASS: exact Deepgram transcription wrapper lifecycle (request_id %s)" % request_id)


if __name__ == "__main__":
    main()
