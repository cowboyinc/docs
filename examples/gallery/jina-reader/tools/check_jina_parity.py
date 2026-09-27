#!/usr/bin/env python3
"""Route drift guard for the Jina Store wrapper (stdlib only).

Jina spans several hosts: only api.jina.ai publishes an OpenAPI, so the wrapper's
api-hosted operations are checked against it (covered or explicitly excluded),
while the Reader/Search/DeepSearch hosts are pinned as manual surfaces.

    python3 tools/check_jina_parity.py                 # offline: actor literals vs the pin
    python3 tools/check_jina_parity.py --url https://api.jina.ai/openapi.json
    python3 tools/check_jina_parity.py --schema openapi.json
"""
import argparse
import ast
import json
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).parent.parent
ACTOR = PROJECT / "actors" / "jina" / "main.py"
FIXTURE = PROJECT / "jina_routes.json"
METHODS = {"get", "post", "put", "patch", "delete"}


def actor_literals():
    values, functions = {}, set()
    for node in ast.parse(ACTOR.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return values, functions


def test_pinned_routes():
    """The actor's route surface still matches the committed pin."""
    values, functions = actor_literals()
    pin = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert values.get("OPENAPI_COVERAGE") == pin["coverage"]
    assert values.get("OPENAPI_EXCLUSIONS") == pin["exclusions"]
    assert {k: list(v) for k, v in values.get("CATALOG_OPERATIONS", {}).items()} == pin["catalog"]
    assert {h: values.get(c) for h, c in {
        "read": "READ_ENDPOINT", "read_post": "READ_ENDPOINT",
        "search": "SEARCH_ENDPOINT", "search_post": "SEARCH_ENDPOINT",
        "embeddings": "EMBEDDINGS_ENDPOINT", "rerank": "RERANK_ENDPOINT",
        "classify": "CLASSIFY_ENDPOINT", "train_classifier": "TRAIN_ENDPOINT",
        "segment": "SEGMENT_ENDPOINT", "deepsearch": "DEEPSEARCH_ENDPOINT",
    }.items()} == pin["manual_surfaces"]
    # every handler the coverage/manual maps name must exist in the actor
    named = {t.split(":", 1)[0] for t in pin["coverage"].values()} | set(pin["manual_surfaces"])
    assert not (named - functions), "handlers named by the pin are missing: %s" % sorted(named - functions)


def _live_operations(schema):
    return {m.upper() + " " + p for p, item in schema.get("paths", {}).items()
            for m in item if m.lower() in METHODS}


def main():
    parser = argparse.ArgumentParser()
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--schema", type=Path, help="local OpenAPI JSON")
    src.add_argument("--url", help="fetch the live OpenAPI and drift-check it")
    args = parser.parse_args()
    test_pinned_routes()
    values, _ = actor_literals()
    print("PASS: actor route surface matches the pin (%d covered, %d excluded, %d catalog)"
          % (len(values["OPENAPI_COVERAGE"]), len(values["OPENAPI_EXCLUSIONS"]),
             len(values["CATALOG_OPERATIONS"])))
    if not (args.schema or args.url):
        return
    if args.schema:
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
    else:
        req = urllib.request.Request(args.url, headers={"User-Agent": "cowboy-jina-parity/1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            schema = json.load(resp)
    live = _live_operations(schema)
    declared = set(values["OPENAPI_COVERAGE"]) | set(values["OPENAPI_EXCLUSIONS"])
    missing, stale = sorted(live - declared), sorted(declared - live)
    if missing or stale:
        raise SystemExit("DRIFT: missing=%r stale=%r" % (missing, stale))
    print("PASS: every published api.jina.ai operation is covered or excluded (%d live)" % len(live))


if __name__ == "__main__":
    main()
