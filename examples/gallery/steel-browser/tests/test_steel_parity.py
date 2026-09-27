#!/usr/bin/env python3
"""Drift guard for the pinned Steel browser surface.

Offline (default, and collected by pytest): the actor's emitted (method, path)
routes, base URL, auth header, and secret name must equal the pinned
``steel_routes.json`` fixture, and the deliberately excluded routes (notably
``/v1/scrape``) must not be emitted. ``--live`` additionally probes Steel's
published SDK reference so a vendor method/path change is caught.
"""

import argparse
import ast
import json
import os
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ACTOR = os.path.join(PROJECT, "actors", "steel", "main.py")
FIXTURE = os.path.join(PROJECT, "steel_routes.json")
HANDLERS = (
    "screenshot",
    "pdf",
    "get_result",
    "register_hire",
    "capability",
    "_run_request__resume",
)


def _tree():
    with open(ACTOR, encoding="utf-8") as handle:
        source = handle.read()
    return ast.parse(source, ACTOR), source


def _literals(tree):
    values, functions = {}, set()
    for node in ast.walk(tree):
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


def _flatten_add(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_add(node.left) + _flatten_add(node.right)
    return [node]


def _url_template(node):
    parts = []
    for operand in _flatten_add(node):
        if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
            parts.append(operand.value)
        elif isinstance(operand, ast.Name) and operand.id == "API_ROOT":
            continue
        else:
            parts.append("{id}")
    return "".join(parts)


def _emitted_routes(tree):
    """Every (method, path-template) the actor hands to ``_accept``.

    Signature: ``self._accept(hire_id, hire, operation, url, body)`` — both
    exposed routes are POST (screenshot and pdf), so there is no GET-body case."""
    routes = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_accept"
            and len(node.args) >= 4
            and isinstance(node.args[2], ast.Constant)
        ):
            operation = node.args[2].value
            routes[operation] = ("POST", _url_template(node.args[3]))
    return routes


def _live_reference(fixture, source_url):
    request = urllib.request.Request(
        source_url, headers={"User-Agent": "cowboy-steel-parity/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = response.read().decode("utf-8")
    problems = []
    for operation, route in fixture["routes"].items():
        title = 'title="%s %s"' % (route["method"].lower(), route["path"])
        if title not in document:
            problems.append("%s: %s no longer present in SDK reference" % (operation, title))
    # the excluded scrape route must still be a POST that returns inline content
    if 'title="post /v1/scrape"' not in document:
        problems.append("/v1/scrape shape changed; re-review the exclusion decision")
    return problems


def _check(offline=True):
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    tree, source = _tree()
    values, functions = _literals(tree)
    problems = []

    if values.get("API_ROOT") != fixture["server"]:
        problems.append("API_ROOT drifted: %r" % values.get("API_ROOT"))
    if values.get("SECRET_KEY") != fixture["auth"]["secret"]:
        problems.append("SECRET_KEY drifted: %r" % values.get("SECRET_KEY"))
    if values.get("AUTH_HEADER") != fixture["auth"]["name"]:
        problems.append("AUTH_HEADER drifted: %r" % values.get("AUTH_HEADER"))

    emitted = _emitted_routes(tree)
    pinned = {op: (r["method"], r["path"]) for op, r in fixture["routes"].items()}
    # exact match means the actor emits EXACTLY the pinned routes — so any
    # excluded route (scrape/list/files/credentials) is enforced-absent here.
    if emitted != pinned:
        problems.append(
            "route mismatch: actor=%r pinned=%r" % (sorted(emitted.items()), sorted(pinned.items()))
        )
    emitted_paths = {path for _, path in emitted.values()}
    if "/v1/scrape" in emitted_paths:
        problems.append("actor emits /v1/scrape; it is deliberately excluded (parks)")

    for handler in HANDLERS:
        if handler not in functions:
            problems.append("missing handler entrypoint: %s" % handler)
    if "on_runner_result" in functions:
        problems.append("public runner callback must not be exported")
    if '"raw_body"' not in source or '"${%s}" % SECRET_KEY' not in source:
        problems.append("POST raw_body or CBSS secret reference is missing")

    live = "skipped"
    if not offline:
        try:
            live_problems = _live_reference(fixture, fixture["source"])
            problems.extend(live_problems)
            live = "ok" if not live_problems else "drift"
        except Exception as exc:
            problems.append("live SDK-reference probe failed: %s" % exc)
            live = "error"
    return fixture, live, problems


def test_actor_matches_pinned_routes():
    _, _, problems = _check(offline=True)
    assert problems == [], "\n".join(problems)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also probe Steel's SDK reference")
    parser.add_argument("--offline", action="store_true", help="offline drift only (default)")
    args = parser.parse_args()
    fixture, live, problems = _check(offline=not args.live)
    print(
        "Steel browser contract | pinned routes: %d | excluded: %d | SDK reference: %s"
        % (len(fixture["routes"]), len(fixture["excluded_routes"]), live)
    )
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: actor matches the pinned Steel route + Steel-Api-Key contract")


if __name__ == "__main__":
    main()
