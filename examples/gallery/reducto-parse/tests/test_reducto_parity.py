#!/usr/bin/env python3
"""Drift guard for the pinned Reducto document-parse surface.

Offline by default: checks actor literals/entrypoints against
`reducto_routes.json`. `--live` also probes Reducto's published OpenAPI.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "reducto", "main.py")
FIXTURE = os.path.join(PROJECT, "reducto_routes.json")
HANDLERS = ("parse", "get_result", "register_hire", "capability", "_run_request__resume")


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
        fixture["openapi"], headers={"User-Agent": "cowboy-reducto-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8")
    spec = json.loads(document)
    paths = spec.get("paths", {})
    parse_path = paths.get("/parse", {})
    schemes = (spec.get("components", {}) or {}).get("securitySchemes", {}) or {}
    bearer = any(
        (isinstance(v, dict) and v.get("type") == "http" and str(v.get("scheme", "")).lower() == "bearer")
        for v in schemes.values()
    )
    return {
        "parse_post": "post" in {k.lower() for k in parse_path},
        "auth_bearer": bearer,
        "force_url_result": "force_url_result" in document,
        "url_result": '"url"' in document and "result_id" in document,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Reducto's published OpenAPI"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    bounds = fixture["bounds"]
    expected = {
        "PARSE_ENDPOINT": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "MAX_URL_CHARS": bounds["max_url_chars"],
        "MAX_RESULT_URL_CHARS": bounds["max_result_url_chars"],
        "MAX_ID_CHARS": bounds["max_id_chars"],
        "MAX_ERROR_CHARS": bounds["max_error_chars"],
        "MAX_REQUEST_BYTES": bounds["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": bounds["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": bounds["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if set(values.get("SUPPORTED_INPUT_FIELDS") or ()) != set(fixture["wrapper_inputs"]):
        problems.append("wrapper input fields drifted")
    if set(values.get("ALLOWED_SCHEMES") or ()) != set(fixture["allowed_input_schemes"]):
        problems.append("allowed input schemes drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    for needle in ('"raw_body"', '"Bearer ${%s}" % SECRET_KEY', '"force_url_result"', '"settings"'):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            if not current["parse_post"]:
                problems.append("live OpenAPI no longer exposes POST /parse")
            if not current["auth_bearer"]:
                problems.append("live OpenAPI dropped the HTTP Bearer scheme")
            if not current["force_url_result"]:
                problems.append("live OpenAPI no longer documents force_url_result")
            if not current["url_result"]:
                problems.append("live OpenAPI no longer documents a url result type")
            live = "ok"
        except Exception as exc:
            problems.append("live OpenAPI probe failed: %s" % exc)

    print("Reducto document-parse contract | transport bounds: pinned | OpenAPI:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /parse (force_url_result) contract")


if __name__ == "__main__":
    main()
