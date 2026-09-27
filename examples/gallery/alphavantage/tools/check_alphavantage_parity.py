#!/usr/bin/env python3
"""Drift guard for the pinned Alpha Vantage surface."""

import argparse
import ast
import json
import os
import urllib.request
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
ACTOR = PROJECT / "actors" / "alphavantage" / "main.py"
FIXTURE = PROJECT / "alphavantage_routes.json"
HANDLERS = (
    "quote",
    "daily",
    "overview",
    "get_result",
    "register_hire",
    "capability",
    "_run_request__resume",
)


def actor_literals():
    with ACTOR.open(encoding="utf-8") as handle:
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


def demo_probe(fixture):
    probe = fixture["demo_probe"]
    url = fixture["base"] + "?function=%s&symbol=%s&apikey=demo" % (
        probe["function"],
        probe["symbol"],
    )
    request = urllib.request.Request(url, headers={"User-Agent": "cowboy-av-parity/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip the live demo-key probe")
    args = parser.parse_args()
    with FIXTURE.open(encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    if values.get("API_ROOT") != fixture["base"]:
        problems.append("API_ROOT drifted: %r" % values.get("API_ROOT"))
    if values.get("SECRET_KEY") != fixture["secret_key"]:
        problems.append("SECRET_KEY drifted: %r" % values.get("SECRET_KEY"))

    actor_functions = {name: list(spec) for name, spec in (values.get("FUNCTIONS") or {}).items()}
    pinned = {
        name: [spec["function"], spec["required"]]
        for name, spec in fixture["functions"].items()
    }
    if actor_functions != pinned:
        problems.append(
            "actor/fixture function map mismatch: actor=%r fixture=%r"
            % (actor_functions, pinned)
        )

    pinned_extracts = {
        name: spec["extract"]
        for name, spec in fixture["functions"].items()
        if spec["extract"] is not None
    }
    if values.get("EXTRACT_PATHS") != pinned_extracts:
        problems.append(
            "actor/fixture extraction mismatch: actor=%r fixture=%r"
            % (values.get("EXTRACT_PATHS"), pinned_extracts)
        )

    bounds = fixture["request_bounds"]
    if list(values.get("OUTPUTSIZES") or ()) != bounds["daily_outputsize"]:
        problems.append("daily outputsize bound drifted")
    if values.get("RUNNER_RESULT_BYTE_BUDGET") != bounds["runner_result_bytes"]:
        problems.append("Runner result byte budget drifted")
    if values.get("RUNNER_DATA_BYTE_BUDGET") != bounds["runner_data_target_bytes"]:
        problems.append("Runner data target drifted")

    with ACTOR.open(encoding="utf-8") as handle:
        source = handle.read()
    if "apikey=${" not in source:
        problems.append("apikey is no longer appended to the URL as a ${...} query reference")
    if fixture["auth"]["in"] != "query" or fixture["auth"]["name"] != "apikey":
        problems.append("fixture auth is not the apikey query parameter")

    missing = [handler for handler in HANDLERS if handler not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public on_runner_result callback must not be exported")

    live = "skipped"
    if not args.offline:
        probe = fixture["demo_probe"]
        try:
            body = demo_probe(fixture)
            if probe["required"] not in body:
                problems.append(
                    "live demo probe missing %r (got keys %s)"
                    % (probe["required"], sorted(body)[:6])
                )
            else:
                live = "ok"
        except Exception as exc:
            problems.append("live demo probe failed: %s" % exc)

    print(
        "Alpha Vantage pinned functions:",
        len(fixture["functions"]),
        "| transport bounds: pinned | auth: apikey (query) | live demo:",
        live,
    )
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor functions still match the pinned /query + apikey-query contract")


if __name__ == "__main__":
    main()
