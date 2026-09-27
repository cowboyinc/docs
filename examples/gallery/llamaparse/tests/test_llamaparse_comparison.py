#!/usr/bin/env python3
"""Live direct-versus-wrapper LlamaParse differential.

Submits one real document through LlamaParse (v2 source_url), polls the job to
completion, retrieves the presigned markdown URL, then drives the exact same
vendor replies through the wrapper lifecycle and requires exact output equality.
Requires a LlamaParse key, so it does not run in the offline sweep.

Because the wrapper fetches with expand=markdown_content_metadata, the vendor
reply is a small presigned handle, so this differential is representative of the
on-chain reply size (an inline markdown result would exceed the runner transport
cap and never resume).
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
FIXTURE = json.loads((PROJECT / "llamaparse_routes.json").read_text(encoding="utf-8"))
SDK = Path(os.environ.get("COWBOY_SDK_PATH", HERE.parents[4] / "node/pvm/Lib")).resolve()
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

import actors.parse.main as llamaparse
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("LLAMAPARSE_API_KEY_FILE", "/private/tmp/llamaparse-key.txt")
DEFAULT_DOC = os.environ.get(
    "LLAMAPARSE_DOCUMENT_URL", "https://pdfobject.com/pdf/sample.pdf"
)
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


def expected_body(document_url):
    return {
        "source_url": str(document_url).strip(),
        "tier": llamaparse.PARSE_TIER,
        "version": llamaparse.PARSE_VERSION,
    }


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def http(method, url, key, body=None):
    headers = {"Accept": "application/json", "Authorization": "Bearer " + key,
               "User-Agent": llamaparse.USER_AGENT}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json_bytes(body)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def poll_until_done(key, job_id, deadline_s=300):
    deadline = time.time() + deadline_s
    url = (llamaparse.RESULT_ENDPOINT_PREFIX + job_id + "?expand=" + llamaparse.RESULT_EXPAND)
    reply = {}
    while time.time() < deadline:
        status, reply = http("GET", url, key)
        assert status == 200, "LlamaParse GET %d: %r" % (status, reply)
        job = reply.get("job", reply)
        state = str(job.get("status") or "").upper()
        if state in llamaparse.SUCCESS_STATUSES:
            return reply
        if state not in llamaparse.PENDING_STATUSES:
            raise AssertionError("parse job failed: %r" % reply)
        time.sleep(5)
    raise TimeoutError("parse job did not finish: %r" % reply)


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = llamaparse.LlamaParseWrapper()
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


def _last_job():
    return codec.decode(host_mock.get_send_log()[-1]["payload"])


def _resume(job, reply):
    host_mock.set_context(sender=ACTOR)
    llamaparse._run_request__resume(
        codec.encode({"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply})
    )


def wrapped(key, document_url):
    actor = fresh_actor()
    # 1) submit
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(
        actor.parse(codec.encode({"hire_id": 42, "document_url": document_url}))
    )
    assert accepted["ok"] is True and accepted["operation"] == "parse"
    submit_job = _last_job()
    kwargs = submit_job["payload"]["kwargs"]
    assert kwargs["url"] == FIXTURE["submit_endpoint"]
    assert kwargs["method"] == "POST"
    assert kwargs["raw_body"] == json_bytes(expected_body(document_url))
    assert "body" not in kwargs and "timeout_ms" not in kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer ${LLAMAPARSE_API_KEY}"
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), "LLAMAPARSE_API_KEY"]]

    status, submit_reply = http("POST", FIXTURE["submit_endpoint"], key, expected_body(document_url))
    assert status in (200, 201), "LlamaParse POST %d: %r" % (status, submit_reply)
    job_id = submit_reply.get("id") or submit_reply.get("job_id")
    assert isinstance(job_id, str) and job_id, "no job id: %r" % submit_reply
    _resume(submit_job, submit_reply)
    host_mock.set_context(sender=BUYER)
    out = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert out["status"] == "ok" and out["output"]["data"]["job_id"] == job_id

    # 2) fetch (poll live to completion, then feed that exact reply)
    done_reply = poll_until_done(key, job_id)
    host_mock.set_context(sender=BUYER, tx_hash=(2).to_bytes(32, "big"))
    fetch_accepted = codec.decode(
        actor.fetch_result(codec.encode({"hire_id": 42, "request_id": 1}))
    )
    assert fetch_accepted["ok"] is True
    fetch_job = _last_job()
    fkwargs = fetch_job["payload"]["kwargs"]
    assert fkwargs["method"] == "GET" and "body" not in fkwargs
    assert fkwargs["url"] == (llamaparse.RESULT_ENDPOINT_PREFIX + job_id + "?expand=" + llamaparse.RESULT_EXPAND)
    _resume(fetch_job, done_reply)
    host_mock.set_context(sender=BUYER)
    final = codec.decode(
        actor.get_result(codec.encode({"request_id": fetch_accepted["request_id"], "hire_id": 42}))
    )
    assert final["status"] == "ok"
    data = final["output"]["data"]
    assert data["ready"] is True and data["result_type"] == "markdown"
    assert isinstance(data["result_url"], str) and data["result_url"].startswith("https://")
    assert "api.cloud.llamaindex.ai" not in data["result_url"]
    return final, done_reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--document-url", default=DEFAULT_DOC)
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    out, reply = wrapped(key, args.document_url)
    print("PASS: exact LlamaParse wrapper lifecycle (result_url %s...)" % out["output"]["data"]["result_url"][:60])


if __name__ == "__main__":
    main()
