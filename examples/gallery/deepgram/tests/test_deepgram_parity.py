#!/usr/bin/env python3
"""Drift guard for the pinned Deepgram transcription surface: checks the
actor's literals/entrypoints against `deepgram_routes.json`; `--live` also
probes Deepgram's published API reference.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "deepgram", "main.py")
FIXTURE = os.path.join(PROJECT, "deepgram_routes.json")
HANDLERS = ("transcribe", "get_result", "register_hire", "capability", "_run_request__resume")


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
        fixture["source"], headers={"User-Agent": "cowboy-deepgram-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8", "ignore")
    return {
        "endpoint_present": "/v1/listen" in document,
        "token_scheme": "Token" in document and "Authorization" in document,
        "url_body": '"url"' in document or "\\\"url\\\"" in document or "url" in document,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Deepgram's published reference"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "LISTEN_ENDPOINT": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "AUTH_SCHEME": fixture["auth"]["scheme"],
        "DEFAULT_MODEL": fixture["default_model"],
        "MAX_URL_CHARS": fixture["bounds"]["max_url_chars"],
        "MAX_REQUEST_BYTES": fixture["bounds"]["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
        "TRANSCRIPT_BYTE_BUDGET": fixture["bounds"]["transcript_bytes"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if set(values.get("SUPPORTED_INPUT_FIELDS") or ()) != set(fixture["wrapper_inputs"]):
        problems.append("wrapper input fields drifted")
    if set(values.get("MODELS") or ()) != set(fixture["models"]):
        problems.append("model allow-list drifted")
    if fixture["idempotency"] is not None:
        problems.append("fixture unexpectedly declares an idempotency key")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    for needle in ('"raw_body"', '"%s ${%s}" % (AUTH_SCHEME, SECRET_KEY)', "allow_nan=False"):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)
    if '"Idempotency-Key"' in source:
        problems.append("must not send an Idempotency-Key header (Deepgram has none)")

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            if not current["endpoint_present"]:
                problems.append("live reference no longer exposes /v1/listen")
            if not current["token_scheme"]:
                problems.append("live reference no longer shows the Token auth scheme")
            live = "ok"
        except Exception as exc:
            problems.append("live reference probe failed: %s" % exc)

    print("Deepgram listen-pre-recorded contract | transport bounds: pinned | reference:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /v1/listen contract")


if __name__ == "__main__":
    main()
