#!/usr/bin/env python3
"""Drift guard for the pinned Anchor Browser usage surface."""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "anchor", "main.py")
FIXTURE = os.path.join(PROJECT, "anchor_routes.json")
HANDLERS = ("usage", "get_result", "register_hire", "capability", "_run_request__resume")


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


def fetch_doc(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "cowboy-anchor-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "BILLING_ENDPOINT": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "MAX_REQUEST_BYTES": fixture["bounds"]["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RUNNER_DATA_BYTE_BUDGET": fixture["bounds"]["runner_data_target_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if list(values.get("GRANULARITIES") or ()) != fixture["granularities"]:
        problems.append("granularities drifted")
    if set(values.get("SUPPORTED_INPUT_FIELDS") or ()) != set(fixture["wrapper_inputs"]):
        problems.append("wrapper input fields drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")

    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    if '"${%s}" % SECRET_KEY' not in source:
        problems.append("CBSS header reference is missing")
    if '"method": "GET"' not in source or '"raw_body"' in source:
        problems.append("usage must be a GET with no request body")

    live = "skipped"
    if not args.offline:
        try:
            billing = fetch_doc(fixture["source"])
            if "/v1/billing" not in billing:
                problems.append("live doc no longer documents /v1/billing")
            if fixture["auth"]["name"] not in billing:
                problems.append("live doc no longer documents the %s header" % fixture["auth"]["name"])
            if "GET" not in billing:
                problems.append("live doc no longer documents GET /v1/billing")
            unlocker = fetch_doc(fixture["web_unlocker_source"])
            if "/v1/tools/fetch/webpage" not in unlocker:
                problems.append("excluded web-unlocker path drifted")
            live = "ok"
        except Exception as exc:
            problems.append("live doc probe failed: %s" % exc)

    print("Anchor usage contract | transport bounds: pinned | docs:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned GET /v1/billing contract")


if __name__ == "__main__":
    main()
