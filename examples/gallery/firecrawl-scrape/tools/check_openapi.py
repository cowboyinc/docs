"""Check the pinned route fixture against a current or downloaded vendor schema."""

import argparse
import json
import urllib.request
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "openapi_routes.json"


def check(schema, pinned):
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    published = {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method in methods
    }
    expected = {tuple(route) for route in pinned["routes"].values()}
    if published != expected:
        raise ValueError(
            f"route drift: added={sorted(published - expected)}, removed={sorted(expected - published)}"
        )
    if pinned["server"] not in {server["url"] for server in schema["servers"]}:
        raise ValueError("API server changed")
    return len(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema", type=Path, help="downloaded OpenAPI JSON; otherwise fetch live"
    )
    args = parser.parse_args()
    pinned = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if args.schema:
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(pinned["source"], timeout=30) as response:
            schema = json.load(response)
    print(f"PASS: {check(schema, pinned)} published routes match the pinned contract")


if __name__ == "__main__":
    main()
