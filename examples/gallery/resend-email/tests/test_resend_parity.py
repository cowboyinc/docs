#!/usr/bin/env python3
"""Drift guard for the pinned Resend email-send surface.

Default run is offline and deterministic: it checks the actor's literals and
entrypoints against the pinned `resend_routes.json` fixture. Pass `--live` to
also probe Resend's published OpenAPI (github raw) and fail on any drift there.
"""

import argparse
import ast
import json
import os
import re
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "resend", "main.py")
FIXTURE = os.path.join(PROJECT, "resend_routes.json")
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
    request = urllib.request.Request(
        fixture["source"], headers={"User-Agent": "cowboy-resend-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8")
    if "operationId: emails/send" not in document:
        raise ValueError("official OpenAPI no longer exposes emails/send")
    request_start = document.index("    SendEmailRequest:")
    request_end = document.index("    Attachment:", request_start)
    request_block = document[request_start:request_end]
    required_block = request_block[
        request_block.index("required:") : request_block.index("properties:")
    ]
    required = set(re.findall(r"^\s*-\s*(from|to|subject)\s*$", required_block, re.MULTILINE))
    response_start = document.index("    SendEmailResponse:")
    response_end = document.index("    UpdateEmailOptions:", response_start)
    response_block = document[response_start:response_end]
    return {
        "operation_id": "emails/send",
        "required_request": sorted(required),
        "to_max_items": 50 if "maxItems: 50" in request_block else None,
        "recipient_fields": sorted(
            field for field in ("to", "cc", "bcc", "reply_to")
            if (field + ":\n") in request_block or ("\n        %s:" % field) in request_block
        ),
        "auth_scheme": "bearer" if "scheme: bearer" in document else "",
        "success_id": "id:" in response_block,
        "idempotency_header": "name: Idempotency-Key" in document,
        "idempotency_max_length": 256 if "maxLength: 256" in document else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Resend's published OpenAPI"
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
        "MAX_REQUEST_BYTES": fixture["bounds"]["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
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
    for needle in ('"raw_body"', '"Bearer ${%s}" % SECRET_KEY', '"Idempotency-Key"'):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            checks = {
                "operation_id": fixture["operation_id"],
                "required_request": sorted(fixture["required_request"]),
                "to_max_items": fixture["to_max_items"],
                "recipient_fields": sorted(fixture["recipient_fields"]),
                "auth_scheme": fixture["auth"]["scheme"].lower(),
                "idempotency_max_length": fixture["idempotency"]["max_length"],
            }
            for field, wanted in checks.items():
                if current[field] != wanted:
                    problems.append("live OpenAPI %s drifted: %r" % (field, current[field]))
            if not current["success_id"]:
                problems.append("live OpenAPI success response no longer returns id")
            if not current["idempotency_header"]:
                problems.append("live OpenAPI dropped the Idempotency-Key header")
            live = "ok"
        except Exception as exc:
            problems.append("live OpenAPI probe failed: %s" % exc)

    print("Resend email-send contract | transport bounds: pinned | OpenAPI:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /emails contract")


if __name__ == "__main__":
    main()
