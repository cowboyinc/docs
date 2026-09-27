#!/usr/bin/env python3
"""Drift guard for the pinned Kernel browse surface: checks the actor's
literals/entrypoints against `kernel_routes.json` (both the `visit` and `step`
actions and the full step op set); `--live` also probes Kernel's published API
reference.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ACTOR = os.path.join(ROOT, "actors", "browse", "main.py")
FIXTURE = os.path.join(ROOT, "kernel_routes.json")
HANDLERS = ("visit", "step", "get_result", "register_hire", "capability",
            "_run_request__resume", "_run_close__resume")


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
        fixture["source"], headers={"User-Agent": "cowboy-kernel-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8", "ignore")
    return {
        "endpoint_present": "/invocations" in document,
        "bearer_scheme": "Bearer" in document and "Authorization" in document,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="also probe Kernel's published reference"
    )
    args = parser.parse_args()
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    values, functions = actor_literals()
    problems = []

    expected = {
        "INVOKE_URL": fixture["endpoint"],
        "SECRET_KEY": fixture["auth"]["secret"],
        "AUTH_HEADER": fixture["auth"]["name"],
        "AUTH_SCHEME": fixture["auth"]["scheme"],
        "KERNEL_APP": fixture["app"],
        "VISIT_ACTION": fixture["actions"][0],
        "STEP_ACTION": fixture["actions"][1],
        "MAX_URL_CHARS": fixture["bounds"]["max_url_chars"],
        "MAX_SELECTOR_CHARS": fixture["bounds"]["max_selector_chars"],
        "MAX_TEXT_CHARS": fixture["bounds"]["max_text_chars"],
        "MAX_REQUEST_BYTES": fixture["bounds"]["max_request_bytes"],
        "RUNNER_RESULT_BYTE_BUDGET": fixture["bounds"]["runner_result_bytes"],
        "RETURN_BYTE_BUDGET": fixture["bounds"]["result_bytes"],
        "TEXT_BYTE_BUDGET": fixture["bounds"]["text_bytes"],
        "MAX_LINKS": fixture["bounds"]["max_links"],
        "MAX_ACTIVE_SESSIONS": fixture["bounds"]["max_active_sessions"],
    }
    for name, wanted in expected.items():
        if values.get(name) != wanted:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    if tuple(values.get("STEP_OPS") or ()) != tuple(fixture["step_ops"]):
        problems.append("step op set drifted: %r" % (values.get("STEP_OPS"),))
    if tuple(values.get("MUTATING_OPS") or ()) != tuple(fixture["mutating_ops"]):
        problems.append("mutating op set drifted: %r" % (values.get("MUTATING_OPS"),))
    if set(values.get("VISIT_INPUT_FIELDS") or ()) != set(fixture["visit_inputs"]):
        problems.append("visit input fields drifted")
    if set(values.get("STEP_INPUT_FIELDS") or ()) != set(fixture["step_inputs"]):
        problems.append("step input fields drifted")
    if "session_id" in (values.get("STEP_INPUT_FIELDS") or ()):
        problems.append("session_id must NOT be a caller input field")
    if fixture["idempotency"] is None:
        problems.append("fixture must declare the Idempotency-Key on mutating ops")
    missing = [name for name in HANDLERS if name not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))
    if "browse" in functions:
        problems.append("legacy 'browse' handler must be renamed to 'visit'")
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    for needle in (
        '"raw_body"',
        '"%s ${%s}" % (AUTH_SCHEME, SECRET_KEY)',
        "allow_nan=False",
        '"app_name": KERNEL_APP',
        '"action_name": action_name',
        '"Idempotency-Key"',
        'headers["Idempotency-Key"] = "kernel-browse-%d" % request_id',
    ):
        if needle not in source:
            problems.append("missing load-bearing construct: %s" % needle)

    live = "skipped"
    if args.live:
        try:
            current = live_contract(fixture)
            if not current["endpoint_present"]:
                problems.append("live reference no longer exposes /invocations")
            if not current["bearer_scheme"]:
                problems.append("live reference no longer shows the Bearer auth scheme")
            live = "ok"
        except Exception as exc:
            problems.append("live reference probe failed: %s" % exc)

    print("Kernel create-invocation (visit + step) contract | transport bounds: pinned | reference:", live)
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned POST /invocations visit+step contract")


if __name__ == "__main__":
    main()
