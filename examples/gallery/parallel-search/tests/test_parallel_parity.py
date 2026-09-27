#!/usr/bin/env python3
"""Drift guard for the pinned Parallel Search surface."""

import argparse
import ast
import json
import os
import urllib.request
from urllib.parse import urlsplit


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "search", "main.py")
FIXTURE = os.path.join(PROJECT, "parallel_routes.json")
HANDLERS = ("search", "get_result", "register_hire", "capability", "_run_request__resume")


def actor_literals():
    with open(ACTOR, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), ACTOR)
    values, functions = {}, set()
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


def live_auth(spec, operation):
    security = operation.get("security")
    if security is None:
        security = spec.get("security") or []
    schemes = spec.get("components", {}).get("securitySchemes", {})
    required = []
    for requirement in security:
        for name in requirement:
            scheme = schemes.get(name, {})
            required.append([scheme.get("in"), scheme.get("name")])
    return required


def live_openapi(fixture):
    with urllib.request.urlopen(fixture["source"], timeout=30) as response:
        spec = json.load(response)
    path = urlsplit(fixture["endpoint"]).path
    method = fixture["method"].lower()
    path_item = spec["paths"].get(path, {})
    if method not in path_item:
        raise ValueError("route %s no longer defines %s" % (path, fixture["method"]))
    operation = path_item[method]
    request_name = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
    response_name = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
    schemas = spec["components"]["schemas"]
    request_schema = schemas[request_name]
    response_schema = schemas[response_name]
    result_name = response_schema["properties"]["results"]["items"]["$ref"].split("/")[-1]
    mode_schema = request_schema["properties"]["mode"]["anyOf"][0]
    return {
        "method": fixture["method"],
        "auth": live_auth(spec, operation),
        "required_request": request_schema["required"],
        "optional_request": list(request_schema["properties"]),
        "required_response": response_schema["required"],
        "result_required": schemas[result_name]["required"],
        "modes": mode_schema["enum"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "SEARCH_ENDPOINT": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "MAX_QUERY_COUNT": fixture["bounds"]["max_queries"],
        "MAX_QUERY_CHARS": fixture["bounds"]["max_query_chars"],
        "MAX_OBJECTIVE_CHARS": fixture["bounds"]["max_objective_chars"],
        "MAX_SESSION_CHARS": fixture["bounds"]["max_session_chars"],
        "MAX_MAX_RESULTS": fixture["bounds"]["max_results"],
        "MAX_MAX_CHARS_TOTAL": fixture["bounds"]["max_chars_total"],
        "MAX_CHARS_PER_RESULT": fixture["bounds"]["max_chars_per_result"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RUNNER_DATA_BYTE_BUDGET": fixture["bounds"]["runner_data_target_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if list(values.get("MODES") or ()) != fixture["modes"]:
        problems.append("search modes drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    if '"raw_body"' not in source or '"${%s}" % SECRET_KEY' not in source:
        problems.append("POST raw body or secret header reference is missing")

    live = "skipped"
    if not args.offline:
        try:
            current = live_openapi(fixture)
            for field in ("method", "required_request", "required_response", "result_required", "modes"):
                if current[field] != fixture[field]:
                    problems.append("live OpenAPI %s drifted" % field)
            if set(current["optional_request"]) != set(fixture["required_request"] + fixture["optional_request"]):
                problems.append("live OpenAPI request fields drifted")
            expected_auth = [fixture["auth"]["in"], fixture["auth"]["name"]]
            if expected_auth not in current["auth"]:
                problems.append(
                    "live OpenAPI auth drifted or dropped: %r" % (current["auth"],)
                )
            live = "ok"
        except Exception as exc:
            problems.append("live OpenAPI probe failed: %s" % exc)

    print("Parallel Search GA contract | transport bounds: pinned | OpenAPI:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /v1/search contract")


if __name__ == "__main__":
    main()
