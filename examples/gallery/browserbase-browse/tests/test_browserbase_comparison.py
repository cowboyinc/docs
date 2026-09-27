# /// script
# requires-python = ">=3.13"
# dependencies = ["cbor2==6.1.4"]
# ///
"""Explicit live comparison: direct vendor calls versus real-SDK job replay.

Run with uv and COWBOY_SDK_PATH. Pytest does not run paid calls. The optional
agent check starts one run and requires its successful, request-specific result.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

# Standalone runs (uv run tests/test_browserbase_comparison.py) start with only
# tests/ on sys.path; add the example root (actors/) and tools/.
_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The Runner disables redirects; never forward its vendor header.
        return None


def http(url, method, headers, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    # Matching HTTP errors are not a successful comparison.
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
        return json.load(response)


def direct(key, path, body=None):
    headers = {"Accept": "application/json", "X-BB-API-Key": key}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, allow_nan=False).encode()
    return http("https://api.browserbase.com/v1" + path,
                "GET" if body is None else "POST", headers, data)


def _live_reply(job, key):
    kw = job["payload"]["kwargs"]
    if kw.get("secrets") != [["0x" + OWNER.hex(), "BROWSERBASE_API_KEY"]]:
        raise ValueError("job does not declare the configured vendor secret")
    if kw.get("hire") != {"hire_id": 42, "declaration_id": "0x" + "55" * 32,
                          "version": 3, "artifact_id": 7}:
        raise ValueError("job does not carry the registered hire binding")
    # This wrapper uses raw_body for POST JSON and omits it for GET. Do not
    # repair a broken job, including the invalid body="" Node representation.
    if "body" in kw or (kw["method"] == "GET" and "raw_body" in kw):
        raise ValueError("unexpected body in Browserbase job")
    if not kw["url"].startswith("https://api.browserbase.com/v1/"):
        raise ValueError("refusing to send the vendor key outside Browserbase")
    headers = {name: value.replace("${BROWSERBASE_API_KEY}", key)
               for name, value in kw["headers"].items()}
    return http(kw["url"], kw["method"], headers, kw.get("raw_body"))


def wrapped(key, handler, payload):
    import actors.browse.main as bb
    from cowboy_sdk import codec
    host = sys.modules["pvm_host"]
    host.set_context(sender=BUYER, tx_hash=(len(host.get_send_log()) + 1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(bb, handler)(codec.encode(dict(payload, hire_id=42))))
    assert accepted["ok"] and accepted["status"] == "pending", accepted
    job = codec.decode(host.get_send_log()[-1]["payload"])
    reply = _live_reply(job, key)
    host.set_context(sender=ACTOR)
    bb._run_request__resume(codec.encode(
        {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": reply}))
    host.set_context(sender=BUYER)
    result = codec.decode(bb.get_result(codec.encode({"request_id": accepted["request_id"], "hire_id": 42})))
    assert result["request_id"] == accepted["request_id"] and result["hire_id"] == 42
    return result


def success(result, handler):
    assert result.get("ok") is True and result.get("status") == "ok", result
    assert result.get("operation") == handler, result
    output = result["output"]
    assert output.get("ok") is True and output.get("operation") == handler, output
    data = output["data"]
    assert isinstance(data, dict) and "_cowboy" not in data, "comparison requires a complete response"
    return data


def completed_run(data, run_id):
    assert data.get("run_id") == run_id, "agent result belongs to another run"
    assert data.get("done") is True and data.get("status") == "COMPLETED", data
    assert data.get("result") == {"heading": "Example Domain"}, data


def run_comparison(key, call, with_agent=False):
    # Independent wire requests from Browserbase's API reference. These paths
    # and payloads must not come from the Actor's own endpoint constants.
    fetch_payload = {"url": "https://example.com", "format": "markdown"}
    fetched = direct(key, "/fetch", fetch_payload)
    out = success(call("fetch", fetch_payload), "fetch")
    assert fetched.get("statusCode") == out.get("statusCode") == 200
    assert isinstance(fetched.get("content"), str) and fetched["content"]
    assert out["content"] == fetched["content"], "fetch content differs"
    print("PASS fetch: successful content matches direct")

    searched = direct(key, "/search", {"query": "Cowboy actor network", "numResults": 3})
    out = success(call("search", {"query": "Cowboy actor network", "num_results": 3}), "search")
    urls = lambda reply: [item["url"] for item in reply["results"]]
    assert urls(searched) and urls(out) == urls(searched), "search URLs differ"
    print("PASS search: nonempty result URLs match direct")

    schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
    payload = {"url": "https://example.com", "format": "json", "schema": schema}
    extracted = direct(key, "/fetch", payload)
    out = success(call("fetch", payload), "fetch")
    assert isinstance(extracted.get("content"), dict) and extracted["content"].get("title")
    assert out["content"] == extracted["content"], "structured fetch differs"
    print("PASS fetch_json: structured content matches direct")

    if with_agent:
        schema = {"type": "object", "properties": {"heading": {"type": "string"}}, "required": ["heading"]}
        out = success(call("browse", {"task": "Report the main heading text of https://example.com",
                                       "result_schema": schema}), "browse")
        run_id = out["run_id"]
        print(f"Started agent run {run_id}; reuse this ID if polling stops.", flush=True)
        deadline = time.monotonic() + 180
        while not out["done"]:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"agent run {run_id} did not finish before the deadline")
            time.sleep(6)
            out = success(call("browse_result", {"run_id": run_id}), "browse_result")
            assert out["run_id"] == run_id, "agent poll changed run identity"
        completed_run(out, run_id)
        actual = direct(key, "/agents/runs/" + urllib.parse.quote(run_id, safe=""))
        assert actual.get("runId") == run_id and actual.get("status") == "COMPLETED", actual
        assert actual.get("result") == out["result"], "agent result differs from direct read"
        print("PASS browse: completed result matches direct read of the same run")


def setup_actor():
    sdk = Path(os.environ["COWBOY_SDK_PATH"]).resolve()
    for name, path, package in (
        ("pvm_host", sdk / "cowboy_sdk/tests/mock_pvm_host.py", None),
        ("cowboy_sdk", sdk / "cowboy_sdk/__init__.py", [str(sdk / "cowboy_sdk")]),
    ):
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=package)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    import actors.browse.main as bb
    from cowboy_sdk import codec
    host = sys.modules["pvm_host"]
    host.reset()
    host.set_context(sender=OWNER, actor_addr=ACTOR)
    bb.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    bb.register_hire(codec.encode({"hire_id": 42, "hirer": "0x" + BUYER.hex(),
        "declaration_id": "0x" + "55" * 32, "version": 3, "artifact_id": 7}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--with-agent", action="store_true", help="also start and complete one agent run")
    args = parser.parse_args()
    key = args.key_file.read_text().strip()
    if not key:
        raise ValueError("vendor key file is empty")
    setup_actor()
    run_comparison(key, lambda handler, payload: wrapped(key, handler, payload), args.with_agent)


if __name__ == "__main__":
    main()
