#!/usr/bin/env python3
"""Drift guard for the pinned Hyperbrowser scrape surface.

Default run is offline and deterministic: it checks the actor's literals and
entrypoints against the pinned `hyperbrowser_routes.json` fixture. Pass `--live`
to also probe Hyperbrowser's published docs and fail on any route/auth drift.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors/scrape/main.py")
FIXTURE = os.path.join(PROJECT, "hyperbrowser_routes.json")
HANDLERS = (
    "scrape",
    "fetch",
    "get_result",
    "register_hire",
    "capability",
    "_run_request__resume",
)
DOC_PAGES = (
    "https://docs.hyperbrowser.ai/api-reference/create-new-scrape-job",
    "https://docs.hyperbrowser.ai/api-reference/get-scrape-job-status-and-result",
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


def live_contract():
    text = ""
    for url in DOC_PAGES:
        request = urllib.request.Request(
            url, headers={"User-Agent": "cowboy-hyperbrowser-parity/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            text += response.read().decode("utf-8", "ignore")
    lowered = text.lower()
    return {
        "scrape_post": "/api/scrape" in text and "post" in lowered,
        "scrape_get": "/api/scrape/" in text or "{jobid}" in lowered or "{id}" in lowered,
        "api_key_header": "x-api-key" in lowered,
        "not_bearer": "authorization: bearer" not in lowered,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Hyperbrowser's published docs"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    bounds = fixture["bounds"]
    expected = {
        "API_ROOT": fixture["base_url"],
        "SCRAPE_ENDPOINT": fixture["endpoints"]["scrape_submit"]["url"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "JOB_DONE": fixture["job_states"]["terminal_done"],
        "MAX_URL_CHARS": bounds["max_url_chars"],
        "MAX_ID_CHARS": bounds["max_id_chars"],
        "MAX_ERROR_CHARS": bounds["max_error_chars"],
        "MAX_REQUEST_BYTES": bounds["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": bounds["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": bounds["result_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if list(values.get("JOB_FAILED") or ()) != list(fixture["job_states"]["terminal_failed"]):
        problems.append("JOB_FAILED drifted: %r" % (values.get("JOB_FAILED"),))
    if set(values.get("ALLOWED_FORMATS") or ()) != set(fixture["allowed_formats"]):
        problems.append("allowed formats drifted")
    if list(values.get("DEFAULT_FORMATS") or ()) != list(fixture["default_formats"]):
        problems.append("default formats drifted")
    if set(values.get("SUPPORTED_INPUT_FIELDS_SCRAPE") or ()) != set(fixture["wrapper_scrape_inputs"]):
        problems.append("scrape input fields drifted")
    if set(values.get("SUPPORTED_INPUT_FIELDS_FETCH") or ()) != set(fixture["wrapper_fetch_inputs"]):
        problems.append("fetch input fields drifted")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")

    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    for needle in ('"raw_body"', '"${%s}" % SECRET_KEY', '"onlyMainContent": True'):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)
    # The vendor auth must be the raw x-api-key header, not Bearer.
    if '"Authorization"' in source:
        problems.append("unexpected Authorization/Bearer header for a x-api-key vendor")

    live = "skipped"
    if args.live:
        try:
            current = live_contract()
            if not current["scrape_post"]:
                problems.append("live docs no longer document POST /api/scrape")
            if not current["scrape_get"]:
                problems.append("live docs no longer document GET /api/scrape/{jobId}")
            if not current["api_key_header"]:
                problems.append("live docs dropped the x-api-key header")
            if not current["not_bearer"]:
                problems.append("live docs now document Bearer auth for scrape")
            live = "ok"
        except Exception as exc:
            problems.append("live docs probe failed: %s" % exc)

    print("Hyperbrowser scrape contract | transport bounds: pinned | docs:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST+GET /api/scrape (x-api-key) contract")


if __name__ == "__main__":
    main()
