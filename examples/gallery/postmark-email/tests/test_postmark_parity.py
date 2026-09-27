#!/usr/bin/env python3
"""Drift guard for the pinned Postmark email-send surface.

Default run is offline and deterministic: it checks the actor's literals and
entrypoints against the pinned ``postmark_routes.json`` fixture. Pass ``--live``
to also probe Postmark's published API reference (HTML) and fail on drift there.

This script deliberately parses the actor with ``ast`` and never imports
``cowboy_sdk``. That sidesteps the RustPython ``threading`` shim shipped under
``pvm/Lib`` which crashes a fresh CPython process (``_thread has no attribute
_set_sentinel``); a standalone script that DOES need the SDK must load it via an
explicit ``importlib`` spec rather than ``sys.path.insert`` of that Lib.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "postmark", "main.py")
FIXTURE = os.path.join(PROJECT, "postmark_routes.json")
HANDLERS = ("send", "get_result", "register_hire", "capability", "_run_request__resume")


def actor_literals():
    with open(ACTOR, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), ACTOR)
    values = {}
    functions = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return values, functions


def live_contract(fixture):
    """Best-effort probe of Postmark's published HTML API reference.

    Postmark documents the send surface as HTML (no machine-readable OpenAPI),
    so this checks for stable contract markers rather than parsing a spec.
    """
    request = urllib.request.Request(
        fixture["source"], headers={"User-Agent": "cowboy-postmark-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8", "replace")
    return {
        "auth_header": fixture["auth"]["name"] in document,
        "endpoint_path": "/email" in document and "api.postmarkapp.com" in document,
        "message_id_field": "MessageID" in document,
        "error_code_field": "ErrorCode" in document,
        "message_field": "Message" in document,
        "required_from_to": all(field in document for field in fixture["required_request"]),
        "body_fields": all(field in document for field in ("HtmlBody", "TextBody")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Postmark's published HTML API reference"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "SEND_ENDPOINT": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "MAX_ADDRESS_CHARS": fixture["bounds"]["max_address_chars"],
        "MAX_RECIPIENTS": fixture["bounds"]["max_recipients"],
        "MAX_TOTAL_RECIPIENTS": fixture["bounds"]["max_total_recipients"],
        "MAX_SUBJECT_CHARS": fixture["bounds"]["max_subject_chars"],
        "MAX_BODY_CHARS": fixture["bounds"]["max_body_chars"],
        "MAX_ID_CHARS": fixture["bounds"]["max_id_chars"],
        "MAX_ERROR_CHARS": fixture["bounds"]["max_error_chars"],
        "MAX_REQUEST_BYTES": fixture["bounds"]["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if str(values.get("SEND_ENDPOINT", "")).split("://")[-1].split("/")[0] != "api.postmarkapp.com":
        problems.append("send endpoint host is not api.postmarkapp.com")
    if set(values.get("SUPPORTED_INPUT_FIELDS") or ()) != set(fixture["wrapper_inputs"]):
        problems.append("wrapper input fields drifted")
    if set(values.get("RECIPIENT_FIELDS") or ()) != set(fixture["recipient_fields"]):
        problems.append("recipient fields drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")

    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    # Load-bearing constructs. Postmark uses a bare token header, NOT Bearer.
    for needle in (
        '"raw_body"',
        '"${%s}" % SECRET_KEY',
        '"Idempotency-Key"',
        "allow_nan=False",
    ):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)
    if '"Bearer ${' in source or "Bearer ${%s}" in source:
        problems.append("Postmark must authenticate with a bare token header, not Bearer")
    # The failure signal is a nonzero ErrorCode, not the HTTP status.
    if fixture["error_signal"]["nonzero_is_error"] and "code != 0" not in source:
        problems.append("actor no longer classifies a nonzero ErrorCode as a failure")
    if fixture["auth"]["scheme"] != "token":
        problems.append("fixture auth scheme is not 'token': %r" % fixture["auth"]["scheme"])
    if fixture["success_error_code"] != 0:
        problems.append("fixture success ErrorCode is not 0")

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            for field, present in current.items():
                if not present:
                    problems.append("live HTML reference missing marker: %s" % field)
            live = "ok"
        except Exception as exc:
            problems.append("live HTML probe failed: %s" % exc)

    print("Postmark email-send contract | transport bounds: pinned | reference:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /email token-auth contract")


if __name__ == "__main__":
    main()
