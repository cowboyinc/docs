#!/usr/bin/env python3
"""Live direct-versus-wrapper Alpha Vantage differential."""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
FIXTURE = json.loads((PROJECT / "alphavantage_routes.json").read_text(encoding="utf-8"))
ROUTES = FIXTURE["functions"]
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

import actors.alphavantage.main as av
from cowboy_sdk import codec


DEFAULT_KEY_FILE = os.environ.get(
    "ALPHAVANTAGE_API_KEY_FILE", "/private/tmp/alphavantage-key.txt"
)
OWNER, BUYER, ACTOR = b"\x22" * 20, b"\x33" * 20, b"\xaa" * 20


_LAST_CALL = [0.0]
_MIN_SPACING = 1.6


def expected_url(handler, payload, key="${ALPHAVANTAGE_API_KEY}"):
    spec = ROUTES[handler]
    symbol = str(payload["symbol"]).strip().upper()
    params = [("function", spec["function"])]
    if handler == "daily":
        params.extend(
            [("symbol", symbol), ("outputsize", "compact")]
        )
    else:
        params.append(("symbol", symbol))
    return FIXTURE["base"] + "?" + urllib.parse.urlencode(params) + "&apikey=" + key


def selected_vendor_data(handler, vendor_reply):
    extract = ROUTES[handler]["extract"]
    if extract is None:
        return vendor_reply
    return {extract: vendor_reply[extract]} if extract in vendor_reply else {}


def http(url, key):
    wait = _MIN_SPACING - (time.time() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)
    live = url.replace("${%s}" % av.SECRET_KEY, key)
    _LAST_CALL[0] = time.time()
    request = urllib.request.Request(
        live,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": av.USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def fresh_actor():
    host_mock.reset()
    host_mock.set_context(sender=OWNER, actor_addr=ACTOR)
    actor = av.AlphaVantageWrapper()
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


def wrapped(key, handler, payload):
    actor = fresh_actor()
    host_mock.set_context(sender=BUYER, tx_hash=(1).to_bytes(32, "big"))
    accepted = codec.decode(getattr(actor, handler)(codec.encode({"hire_id": 42, **payload})))
    assert accepted == {
        "ok": True,
        "request_id": 1,
        "hire_id": 42,
        "operation": handler,
        "status": "pending",
        "poll_handler": "get_result",
    }
    job = codec.decode(host_mock.get_send_log()[-1]["payload"])
    kwargs = job["payload"]["kwargs"]
    assert kwargs["url"] == expected_url(handler, payload)
    assert kwargs["method"] == "GET" and kwargs["headers"] == {
        "Accept": "application/json",
        "User-Agent": av.USER_AGENT,
    }
    assert "body" not in kwargs and "raw_body" not in kwargs
    assert "Content-Type" not in kwargs["headers"] and "timeout_ms" not in kwargs
    assert kwargs["secrets"] == [["0x" + OWNER.hex(), av.SECRET_KEY]]
    assert kwargs["hire"] == {
        "hire_id": 42,
        "declaration_id": "0x" + "55" * 32,
        "version": 3,
        "artifact_id": 7,
    }
    extract = ROUTES[handler]["extract"]
    assert kwargs.get("extract_path") == extract
    _, vendor_reply = http(kwargs["url"], key)
    runner_data = (
        {"result": vendor_reply[extract]}
        if extract is not None and extract in vendor_reply
        else ({} if extract is not None else vendor_reply)
    )
    assert len(av._json_bytes(runner_data)) <= av.RUNNER_DATA_BYTE_BUDGET, (
        "%s reply exceeds the request-side Runner data target" % handler
    )
    host_mock.set_context(sender=ACTOR)
    av._run_request__resume(
        codec.encode(
            {"cid": job["cid"], "reply_handler": job["reply_handler"], "result": runner_data}
        )
    )
    host_mock.set_context(sender=BUYER)
    result = codec.decode(
        actor.get_result(codec.encode({"request_id": accepted["request_id"], "hire_id": 42}))
    )
    assert result["request_id"] == 1 and result["hire_id"] == 42
    assert result["operation"] == handler and result["status"] in ("ok", "error")
    return result, vendor_reply


def assert_fidelity(data, vendor_reply, required):
    if data == vendor_reply:
        return "exact"
    marker = data.get("_cowboy") if isinstance(data, dict) else None
    assert marker and marker.get("truncated") is True, "wrapper altered an unmarked reply"
    assert marker["original_bytes"] == len(codec.encode(vendor_reply))
    assert marker["limit_bytes"] == av.API_BYTE_BUDGET
    retained = data[required]
    original = vendor_reply[required]
    if isinstance(original, list):
        assert retained and retained == original[: len(retained)]
        return "bounded-prefix"
    if isinstance(original, dict):
        assert retained
        newest = sorted(original, reverse=True)
        assert sorted(retained, reverse=True) == newest[: len(retained)]
        assert all(retained[key] == original[key] for key in retained)
        return "bounded-newest-rows"
    raise AssertionError("wrapper truncated a scalar required field")


CASES = [
    ("quote", {"symbol": "IBM"}),
    ("daily", {"symbol": "IBM"}),
    ("overview", {"symbol": "IBM"}),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    parser.add_argument("--only", help="run a single handler (conserves the daily quota)")
    args = parser.parse_args()
    key = open(args.key_file, encoding="utf-8").read().strip()
    cases = [c for c in CASES if not args.only or c[0] == args.only]
    for handler, payload in cases:
        result, vendor_reply = wrapped(key, handler, payload)
        output = result["output"]
        assert output["ok"] is True, "%s failed: %r" % (handler, output)
        assert output["operation"] == handler
        required = ROUTES[handler]["required"]
        assert required in output["data"], "%s: wrapper output missing %r" % (handler, required)
        expected = selected_vendor_data(handler, vendor_reply)
        fidelity = assert_fidelity(output["data"], expected, required)
        if ROUTES[handler]["extract"] is not None and fidelity == "exact":
            fidelity = "selected-exact"
        print(
            "PASS %-10s live wrapper lifecycle: %s %r (%d JSON bytes)"
            % (handler, fidelity, required, len(av._json_bytes(output)))
        )
    print("PASS: Alpha Vantage wrapper delivers live vendor data faithfully")


if __name__ == "__main__":
    main()
