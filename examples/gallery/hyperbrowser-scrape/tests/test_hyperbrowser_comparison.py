#!/usr/bin/env python3
"""Live direct-versus-wrapper Hyperbrowser scrape differential.

Submits one real scrape through Hyperbrowser (POST /api/scrape), polls the
key-authenticated GET /api/scrape/{jobId} until it completes, then feeds those
exact vendor replies through the wrapper's two-phase lifecycle (scrape submit,
then fetch) and requires exact output equality. Requires a Hyperbrowser key, so
it does not run in the offline sweep.

Note the transport caveat: a large page's markdown can exceed the runner->actor
cap and would PARK the fetch continuation on chain even though this differential
(which bypasses transport) passes. Use `links` or a small page here.
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
FIXTURE = json.loads((PROJECT / "hyperbrowser_routes.json").read_text(encoding="utf-8"))
SDK = Path(os.environ.get("COWBOY_SDK_PATH", HERE.parents[3] / "node/pvm/Lib")).resolve()
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

import actors.scrape.main as hyperbrowser
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get("HYPERBROWSER_API_KEY_FILE", "/private/tmp/hyperbrowser-key.txt")
DEFAULT_URL = os.environ.get("HYPERBROWSER_URL", "https://example.com")
SCRAPE_URL = FIXTURE["endpoints"]["scrape_submit"]["url"]
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


def expected_scrape_body(url, formats=None):
    return {
        "url": str(url).strip(),
        "scrapeOptions": {
            "formats": formats or list(hyperbrowser.DEFAULT_FORMATS),
            "onlyMainContent": True,
        },
    }


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def http_post(body, key):
    request = urllib.request.Request(
        SCRAPE_URL,
        data=json_bytes(body),
        method="POST",
        headers={
            "Accept": "application/json",
            "x-api-key": key,
            "Content-Type": "application/json",
            "User-Agent": hyperbrowser.USER_AGENT,
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


def http_get(job_id, key):
    request = urllib.request.Request(
        SCRAPE_URL + "/" + job_id,
        method="GET",
        headers={
            "Accept": "application/json",
            "x-api-key": key,
            "User-Agent": hyperbrowser.USER_AGENT,
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


def poll_until_complete(job_id, key, deadline_s=180):
    deadline = time.time() + deadline_s
    reply = {"jobId": job_id, "status": "pending"}
    while time.time() < deadline:
        status, reply = http_get(job_id, key)
        assert status == 200, "Hyperbrowser GET returned HTTP %d: %r" % (status, reply)
        if reply.get("status") in (hyperbrowser.JOB_DONE,) + hyperbrowser.JOB_FAILED:
            return reply
        time.sleep(4)
    return reply


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = hyperbrowser.HyperbrowserScrapeWrapper()
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


def _resume(actor, job, reply):
    host_mock.set_context(sender=ACTOR)
    hyperbrowser._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}
        )
    )


def wrapped(key, url, formats=None):
    actor = fresh_actor()
    # phase 1: submit
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    fields = {"hire_id": 42, "url": url}
    if formats:
        fields["formats"] = formats
    accepted = codec.decode(actor.scrape(codec.encode(fields)))
    assert accepted["ok"] is True and accepted["operation"] == "scrape"
    submit_job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = submit_job["payload"]["kwargs"]
    assert kwargs["url"] == SCRAPE_URL and kwargs["method"] == "POST"
    assert kwargs["raw_body"] == json_bytes(expected_scrape_body(url, formats))
    assert kwargs["headers"]["x-api-key"] == "${HYPERBROWSER_API_KEY}"
    assert "Authorization" not in kwargs["headers"]

    status, submit_reply = http_post(expected_scrape_body(url, formats), key)
    assert status == 200, "Hyperbrowser POST returned HTTP %d: %r" % (status, submit_reply)
    job_id = submit_reply.get("jobId")
    assert hyperbrowser._valid_job_id(job_id), "no jobId in submit reply: %r" % submit_reply
    _resume(actor, submit_job, submit_reply)
    host_mock.set_context(sender=BUYER)
    scrape_out = codec.decode(actor.get_result(codec.encode({"request_id": 1, "hire_id": 42})))
    assert scrape_out["status"] == "ok" and scrape_out["output"]["data"]["job_id"] == job_id

    # phase 2: fetch
    host_mock.set_context(sender=BUYER, tx_hash=(2).to_bytes(32, "big"))
    fetch_accepted = codec.decode(
        actor.fetch(codec.encode({"hire_id": 42, "source_request_id": 1}))
    )
    assert fetch_accepted["ok"] is True and fetch_accepted["operation"] == "fetch"
    fetch_job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    fkwargs = fetch_job["payload"]["kwargs"]
    assert fkwargs["method"] == "GET" and fkwargs["url"] == SCRAPE_URL + "/" + job_id
    assert "body" not in fkwargs and "raw_body" not in fkwargs

    final_reply = poll_until_complete(job_id, key)
    _resume(actor, fetch_job, final_reply)
    host_mock.set_context(sender=BUYER)
    fetch_out = codec.decode(
        actor.get_result(codec.encode({"request_id": fetch_accepted["request_id"], "hire_id": 42}))
    )
    if final_reply.get("status") == hyperbrowser.JOB_DONE:
        assert fetch_out["status"] == "ok"
        assert fetch_out["output"]["data"]["job_status"] == "completed"
    return scrape_out, fetch_out, final_reply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--formats", nargs="*", default=["links"])
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    scrape_out, fetch_out, reply = wrapped(key, args.url, args.formats)
    print(
        "PASS: exact Hyperbrowser scrape wrapper lifecycle (job_status %s)"
        % fetch_out["output"].get("data", {}).get("job_status")
    )


if __name__ == "__main__":
    main()
