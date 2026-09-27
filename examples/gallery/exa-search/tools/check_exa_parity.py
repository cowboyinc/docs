#!/usr/bin/env python3
"""Compare the literal route table with a pinned or explicitly fetched schema."""
import argparse
import ast
import json
from pathlib import Path
import urllib.request

HERE = Path(__file__).parent.parent


def actor_routes():
    for node in ast.parse((HERE / 'actors/exa/main.py').read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'API_OPERATIONS' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('API_OPERATIONS not found')


def test_pinned_routes():
    routes = json.loads((HERE / 'exa_routes.json').read_text())['routes']
    assert actor_routes() == {name: tuple(spec[1:]) for name, spec in routes.items()}


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--schema', type=Path)
    source.add_argument('--url')
    args = parser.parse_args()
    test_pinned_routes()
    if args.schema or args.url:
        if args.schema:
            schema = json.loads(args.schema.read_text())
        else:
            with urllib.request.urlopen(args.url, timeout=30) as response:
                schema = json.load(response)
        live = {(method.upper(), path) for path, item in schema['paths'].items()
                for method in ('get', 'post', 'patch', 'delete') if method in item}
        mapped = set(actor_routes().values())
        assert live == mapped, 'missing=%r stale=%r' % (sorted(live - mapped), sorted(mapped - live))
    print('PASS: %d Exa routes' % len(actor_routes()))


if __name__ == '__main__':
    main()
