#!/usr/bin/env python3
"""Drift guard for the pinned LlamaParse document-parse surface.

Default run is offline and deterministic: it checks the actor's literals and
entrypoints against the pinned `llamaparse_routes.json` fixture. Pass `--live`
to also probe LlamaParse's published OpenAPI and fail on any drift there.

The v2 parse endpoints are not fully published in the OpenAPI document (v1 is),
so `--live` is best-effort: it flags when the pinned markers are absent so the
morning verification against the live API is prompted, not silently skipped.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors/parse/main.py")
FIXTURE = os.path.join(PROJECT, "llamaparse_routes.json")
HANDLERS = (
    "parse",
    "fetch_result",
    "get_result",
    "register_hire",
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
        fixture["openapi"], headers={"User-Agent": "cowboy-llamaparse-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8")
    spec = json.loads(document)
    schemes = (spec.get("components", {}) or {}).get("securitySchemes", {}) or {}
    bearer = any(
        (isinstance(v, dict) and str(v.get("scheme", "")).lower() == "bearer")
        or (isinstance(v, dict) and v.get("type") == "http")
        for v in schemes.values()
    ) or "HTTPBearer" in document or "bearer" in document.lower()
    return {
        "v2_parse": "/api/v2/parse" in document,
        "auth_bearer": bearer,
        "source_url": "source_url" in document,
        "content_metadata": "content_metadata" in document,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe LlamaParse's published OpenAPI"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    bounds = fixture["bounds"]
    expected = {
        "PARSE_ENDPOINT": fixture["submit_endpoint"],
        "RESULT_EXPAND": fixture["result_expand"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "KEY_AUTH_HOST": fixture["key_auth_host"],
        "API_ROOT": fixture["base_url"],
        "MAX_URL_CHARS": bounds["max_url_chars"],
        "MAX_RESULT_URL_CHARS": bounds["max_result_url_chars"],
        "MAX_JOB_ID_CHARS": bounds["max_job_id_chars"],
        "MAX_ID_CHARS": bounds["max_id_chars"],
        "MAX_ERROR_CHARS": bounds["max_error_chars"],
        "MAX_REQUEST_BYTES": bounds["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": bounds["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": bounds["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))

    template = fixture["result_endpoint_template"]
    prefix = values.get("RESULT_ENDPOINT_PREFIX")
    if not (isinstance(prefix, str) and template.startswith(prefix)):
        problems.append("RESULT_ENDPOINT_PREFIX drifted: %r" % (prefix,))

    if set(values.get("SUPPORTED_PARSE_FIELDS") or ()) != set(fixture["wrapper_parse_inputs"]):
        problems.append("wrapper parse input fields drifted")
    if set(values.get("SUPPORTED_FETCH_FIELDS") or ()) != set(fixture["wrapper_fetch_inputs"]):
        problems.append("wrapper fetch input fields drifted")
    if set(values.get("ALLOWED_SCHEMES") or ()) != set(fixture["allowed_input_schemes"]):
        problems.append("allowed input schemes drifted")
    if set(values.get("SUCCESS_STATUSES") or ()) != set(fixture["job_status"]["success"]):
        problems.append("success status vocabulary drifted")
    if set(values.get("PENDING_STATUSES") or ()) != set(fixture["job_status"]["pending"]):
        problems.append("pending status vocabulary drifted")

    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")

    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    for needle in ('"raw_body"', '"Bearer ${%s}" % SECRET_KEY', '"source_url"', '"?expand="'):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            if not current["auth_bearer"]:
                problems.append("live OpenAPI dropped the HTTP Bearer scheme")
            if not current["v2_parse"]:
                problems.append(
                    "live OpenAPI does not expose /api/v2/parse (verify v2 contract directly)"
                )
            if not current["source_url"]:
                problems.append("live OpenAPI no longer documents source_url input")
            if not current["content_metadata"]:
                problems.append("live OpenAPI no longer documents *_content_metadata (presigned URL) results")
            live = "ok"
        except Exception as exc:
            problems.append("live OpenAPI probe failed: %s" % exc)

    print("LlamaParse document-parse contract | transport bounds: pinned | OpenAPI:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /api/v2/parse + presigned-result contract")


if __name__ == "__main__":
    main()
