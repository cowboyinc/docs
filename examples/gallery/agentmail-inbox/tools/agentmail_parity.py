#!/usr/bin/env python3
"""Drift guard for the AgentMail wrapper (stdlib only).

Every endpoint the actor calls must still exist in AgentMail's published
OpenAPI with the same method and path template, the Bearer auth scheme must
still be declared, and the actor's constants must match. Fails on API drift.

    python3 test_agentmail_parity.py                    # fetches the live OpenAPI
    python3 test_agentmail_parity.py --schema openapi.json
"""
import argparse
import ast
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ACTOR = os.path.join(os.path.dirname(HERE), "actors/inbox/main.py")
DEFAULT_SCHEMA = "https://docs.agentmail.to/openapi.json"
# (method, path template) the actor depends on
ENDPOINTS = [
    ("POST", "/v0/inboxes"),
    ("POST", "/v0/inboxes/{inbox_id}/messages/send"),
    ("POST", "/v0/inboxes/{inbox_id}/messages/{message_id}/reply"),
    ("GET", "/v0/inboxes/{inbox_id}/messages"),
    ("GET", "/v0/inboxes/{inbox_id}/messages/{message_id}"),
    ("GET", "/v0/inboxes/{inbox_id}/messages/search"),
]
MANUAL_HANDLERS = ("create_inbox", "send", "reply", "list_messages",
                   "get_message", "search", "register_hire", "capability")


def actor_literals():
    with open(ACTOR, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), ACTOR)
    values, functions = {}, set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return values, functions


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cowboy-am-parity/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", help="local OpenAPI json; default fetches AgentMail live")
    parser.add_argument("--url", default=DEFAULT_SCHEMA)
    args = parser.parse_args()
    values, functions = actor_literals()
    schema = json.load(open(args.schema, encoding="utf-8")) if args.schema else fetch(args.url)

    paths = schema.get("paths", {})
    live = {(m.upper(), p) for p, item in paths.items()
            for m in item if m.lower() in ("get", "post", "put", "patch", "delete")}
    schemes = schema.get("components", {}).get("securitySchemes", {}) or {}
    has_bearer = any((v or {}).get("scheme") == "bearer" for v in schemes.values())
    servers = [x.get("url") for x in schema.get("servers", [])]

    problems = []
    for method, path in ENDPOINTS:
        if (method, path) not in live:
            problems.append("%s %s no longer in the OpenAPI" % (method, path))
    if not has_bearer:
        problems.append("Bearer auth scheme not declared in the OpenAPI")
    if "https://api.agentmail.to" not in servers:
        problems.append("api.agentmail.to not a declared server: %r" % servers)
    if values.get("API_ROOT") != "https://api.agentmail.to/v0":
        problems.append("API_ROOT drifted: %r" % values.get("API_ROOT"))
    if values.get("AUTH_HEADER") != "Authorization" or values.get("AUTH_SCHEME") != "Bearer ":
        problems.append("auth constants drifted: %r %r"
                        % (values.get("AUTH_HEADER"), values.get("AUTH_SCHEME")))
    missing = [h for h in MANUAL_HANDLERS if h not in functions]
    if missing:
        problems.append("missing handler entrypoints: %s" % ", ".join(missing))

    print("AgentMail OpenAPI live operations:", len(live), "| bearer:", has_bearer)
    print("checked endpoints:", len(ENDPOINTS), "| API_ROOT:", values.get("API_ROOT"))
    if problems:
        print("DRIFT:", *problems, sep="\n  ")
        raise SystemExit(1)
    print("PASS: every endpoint the actor calls is in the OpenAPI with the same "
          "method/path and Bearer auth, and the actor's constants match")


if __name__ == "__main__":
    main()
