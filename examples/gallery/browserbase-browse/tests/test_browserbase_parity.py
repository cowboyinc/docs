#!/usr/bin/env python3
"""Drift guard for the Browserbase wrapper (stdlib only).

Browserbase publishes no single OpenAPI file, so this checks the actor against
its API reference pages: each endpoint the actor calls must still be documented
with the same path, method, and X-BB-API-Key auth, and the actor's endpoint
constants must match. Fails if Browserbase moves or renames an endpoint.

    python3 test_browserbase_parity.py                     # fetches the live docs
    python3 test_browserbase_parity.py --pages-dir ./pinned # offline pinned .md pages
"""
import argparse
import ast
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ACTOR = os.path.join(os.path.dirname(HERE), "actors", "browse", "main.py")
DOCS = "https://docs.browserbase.com/reference/api"
# (reference page, method, path the actor calls)
ENDPOINTS = [
    ("fetch-a-page", "POST", "/v1/fetch"),
    ("web-search", "POST", "/v1/search"),
    ("run-an-agent", "POST", "/v1/agents/runs"),
    ("get-a-run", "GET", "/v1/agents/runs"),
]
MANUAL_HANDLERS = ("fetch", "search", "browse", "browse_result", "get_result",
                   "register_hire", "capability", "get_last_output", "get_config")


def actor_literals():
    with open(ACTOR, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), ACTOR)
    values, functions = {}, set()

    def literal(node):
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal(node.left) + literal(node.right)
        return ast.literal_eval(node)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = literal(node.value)
            except (KeyError, ValueError, TypeError):
                pass
    return values, functions


def page_text(name, pages_dir):
    if pages_dir:
        with open(os.path.join(pages_dir, name + ".md"), encoding="utf-8") as handle:
            return handle.read()
    req = urllib.request.Request(DOCS + "/" + name + ".md",
                                 headers={"User-Agent": "cowboy-bb-parity/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-dir", help="dir of pinned reference .md pages; default fetches live")
    args = parser.parse_args()
    values, functions = actor_literals()
    problems = []

    for name, method, path in ENDPOINTS:
        try:
            text = page_text(name, args.pages_dir).lower()
        except Exception as exc:
            problems.append("%s: could not read reference page (%s)" % (name, exc))
            continue
        if path.lower() not in text:
            problems.append("%s: path %s no longer on the reference page" % (name, path))
        if method.lower() not in text:
            problems.append("%s: method %s no longer documented" % (name, method))
        if "x-bb-api-key" not in text:
            problems.append("%s: X-BB-API-Key auth no longer documented" % name)

    if values.get("API_ROOT") != "https://api.browserbase.com/v1":
        problems.append("API_ROOT drifted: %r" % values.get("API_ROOT"))
    if values.get("AUTH_HEADER") != "X-BB-API-Key":
        problems.append("AUTH_HEADER drifted: %r" % values.get("AUTH_HEADER"))
    for name, path in (("FETCH_ENDPOINT", "/fetch"), ("SEARCH_ENDPOINT", "/search"),
                       ("AGENTS_ENDPOINT", "/agents/runs")):
        if values.get(name) != "https://api.browserbase.com/v1" + path:
            problems.append("%s drifted: %r" % (name, values.get(name)))
    missing = [h for h in MANUAL_HANDLERS if h not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))

    print("Browserbase reference endpoints checked:", len(ENDPOINTS))
    print("actor: API_ROOT=%s AUTH_HEADER=%s" % (values.get("API_ROOT"), values.get("AUTH_HEADER")))
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: every endpoint the actor calls is documented with the same path, "
          "method and auth, and the actor's constants match")


if __name__ == "__main__":
    main()
