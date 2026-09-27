#!/usr/bin/env python3
"""Drift guard for the pinned Tavily Search surface."""

import argparse
import ast
import json
import os
import re
import urllib.request
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
ACTOR = PROJECT / "actors" / "tavily" / "main.py"
FIXTURE = PROJECT / "tavily_routes.json"
HANDLERS = ("search", "get_result", "register_hire", "capability", "_run_request__resume")


def actor_literals():
    with ACTOR.open(encoding="utf-8") as handle:
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


def indented_names(block, spaces):
    return re.findall(r"^ {%d}([a-z_][a-z0-9_]*):\s*$" % spaces, block, re.MULTILINE)


def required_names(block):
    return re.findall(r"^ {18}- ([a-z_][a-z0-9_]*)\s*$", block, re.MULTILINE)


def live_contract(fixture):
    request = urllib.request.Request(
        fixture["source"], headers={"User-Agent": "cowboy-tavily-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8")
    if "````yaml POST /search" not in document:
        raise ValueError("official documentation no longer exposes POST /search OpenAPI")
    request_start = document.index("              properties:\n")
    request_end = document.index("              required:\n                - query", request_start)
    request_block = document[request_start:request_end]
    response_start = document.index("                properties:\n", request_end)
    response_end = document.index("        '400':", response_start)
    response_block = document[response_start:response_end]
    required_start = response_block.rindex("                required:\n")
    response_required = required_names(response_block[required_start:])
    depth_block = request_block[
        request_block.index("                search_depth:\n") :
        request_block.index("                chunks_per_source:\n")
    ]
    depth_values = re.findall(r"^ {20}- ([a-z-]+)\s*$", depth_block, re.MULTILINE)
    topic_block = request_block[
        request_block.index("                topic:\n") :
        request_block.index("                time_range:\n")
    ]
    topic_values = re.findall(r"^ {20}- ([a-z-]+)\s*$", topic_block, re.MULTILINE)
    return {
        "method": "POST",
        "endpoint": "https://api.tavily.com/search",
        "auth_scheme": "bearer" if "scheme: bearer" in document else "",
        "required_request": ["query"],
        "request_fields": indented_names(request_block, 16),
        "required_response": response_required,
        "search_depths": depth_values,
        "topics": topic_values,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    with FIXTURE.open(encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "SEARCH_ENDPOINT": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "MAX_QUERY_CHARS": fixture["bounds"]["max_query_chars"],
        "MAX_DOMAIN_COUNT": fixture["bounds"]["max_domains"],
        "MAX_MAX_RESULTS": fixture["bounds"]["max_results"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RUNNER_DATA_BYTE_BUDGET": fixture["bounds"]["runner_data_target_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if list(values.get("SEARCH_DEPTHS") or ()) != fixture["search_depths"]:
        problems.append("search depths drifted")
    if list(values.get("TOPICS") or ()) != fixture["topics"]:
        problems.append("topics drifted")
    if set(values.get("SUPPORTED_INPUT_FIELDS") or ()) != set(fixture["wrapper_inputs"]):
        problems.append("wrapper input fields drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    with ACTOR.open(encoding="utf-8") as handle:
        source = handle.read()
    if '"raw_body"' not in source or '"Bearer ${%s}" % SECRET_KEY' not in source:
        problems.append("POST raw body or CBSS Bearer reference is missing")

    live = "skipped"
    if not args.offline:
        try:
            current = live_contract(fixture)
            request_fields = fixture["required_request"] + fixture["optional_request"]
            checks = {
                "method": fixture["method"],
                "endpoint": fixture["endpoint"],
                "required_request": fixture["required_request"],
                "required_response": fixture["required_response"],
                "search_depths": fixture["search_depths"],
                "topics": fixture["topics"],
                "auth_scheme": fixture["auth"]["scheme"].lower(),
            }
            for field, wanted in checks.items():
                if current[field] != wanted:
                    problems.append("live OpenAPI %s drifted: %r" % (field, current[field]))
            if set(current["request_fields"]) != set(request_fields):
                problems.append("live OpenAPI request fields drifted")
            live = "ok"
        except Exception as exc:
            problems.append("live OpenAPI probe failed: %s" % exc)

    print("Tavily Search contract | transport bounds: pinned | OpenAPI:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /search contract")


if __name__ == "__main__":
    main()
