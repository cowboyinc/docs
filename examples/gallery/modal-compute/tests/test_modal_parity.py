#!/usr/bin/env python3
"""Drift guard for the pinned Modal compute-invoke surface.

Offline by default: checks the actor's literals/entrypoints against the
pinned ``modal_routes.json`` fixture. ``--live`` also probes Modal's proxy-auth
guide for header/token/host drift.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "modal", "main.py")
FIXTURE = os.path.join(PROJECT, "modal_routes.json")
HANDLERS = (
    "invoke",
    "get_result",
    "register_hire",
    "register_endpoint",
    "list_endpoints",
    "capability",
    "_run_request__resume",
)


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
        fixture["source"], headers={"User-Agent": "cowboy-modal-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8", "replace")
    auth = fixture["auth"]
    return {
        "key_header": auth["key_header"] in document,
        "secret_header": auth["secret_header"] in document,
        "token_id_prefix": auth["token_id_prefix"] in document,
        "token_secret_prefix": auth["token_secret_prefix"] in document,
        "host_suffix": fixture["endpoint_host_suffix"] in document,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Modal's proxy-auth guide"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "SECRET_TOKEN_ID": fixture["auth"]["key_secret"],
        "SECRET_TOKEN_SECRET": fixture["auth"]["secret_secret"],
        "KEY_HEADER": fixture["auth"]["key_header"],
        "SECRET_HEADER": fixture["auth"]["secret_header"],
        "ENDPOINT_HOST_SUFFIX": fixture["endpoint_host_suffix"],
        "MAX_ENDPOINTS": fixture["bounds"]["max_endpoints"],
        "MAX_REQUEST_BYTES": fixture["bounds"]["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if list(values.get("METHODS") or ()) != list(fixture["methods"]):
        problems.append("METHODS drifted: %r" % (values.get("METHODS"),))
    if set(values.get("SUPPORTED_INPUT_FIELDS") or ()) != set(fixture["wrapper_inputs"]):
        problems.append("wrapper input fields drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    for needle in (
        '"${%s}" % SECRET_TOKEN_ID',
        '"${%s}" % SECRET_TOKEN_SECRET',
        '"raw_body"',
        "_validate_endpoint_url",
        "_querystring",
    ):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            for field, present in current.items():
                if not present:
                    problems.append("live Modal doc no longer mentions %s" % field)
            live = "ok"
        except Exception as exc:
            problems.append("live Modal doc probe failed: %s" % exc)

    print("Modal compute-invoke contract | transport bounds: pinned | doc:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned Modal proxy-auth invoke contract")


if __name__ == "__main__":
    main()
