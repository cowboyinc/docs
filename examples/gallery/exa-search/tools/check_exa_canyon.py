#!/usr/bin/env python3
"""Explicit, paid live checks. Pytest does not execute this matrix."""
import argparse
import json
from pathlib import Path
import urllib.request

from exa_client import add_connection_arguments, request as wrapped_request

ROUTES = json.loads((Path(__file__).parent.parent / 'exa_routes.json').read_text())['routes']
CASES = [
    ('search', {'query': 'Cowboy actor network', 'numResults': 2, 'contents': {'text': {'maxCharacters': 800}}}),
    ('contents', {'urls': ['https://example.com'], 'text': {'maxCharacters': 800}}),
    ('answer', {'query': 'What is 2+2?', 'text': False}),
    ('list_monitors', {}),
    ('list_agent_runs', {}),
]


def direct_request(key, operation, payload):
    _, method, path = ROUTES[operation]
    body = dict(payload)
    if operation in ('search', 'answer'):
        body['stream'] = False
    data = json.dumps(body).encode() if method in ('POST', 'PATCH') else None
    req = urllib.request.Request('https://api.exa.ai' + path, data=data, method=method,
        headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'x-api-key': key})
    # HTTP errors must fail the live success matrix, including matching invalid keys.
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def assert_success(operation, direct, wrapped):
    assert wrapped.get('ok') is True and wrapped.get('status') == 'ok', 'wrapper request failed'
    assert wrapped.get('operation') == operation
    result = wrapped.get('data')
    for value in (direct, result):
        assert isinstance(value, dict), 'expected a JSON object'
        assert not any(k in value for k in ('error', 'detail', 'raw', 'raw_text')), 'vendor request failed'
        assert not value.get('truncated'), 'live fixture exceeds delivery budget'
    assert set(direct) == set(result), 'response fields differ'
    if operation in ('search', 'contents'):
        for value in (direct, result):
            assert value.get('results'), 'expected nonempty results'
            assert all(isinstance(item.get('url'), str) and item['url'].startswith('https://') for item in value['results'])
        assert set(direct['results'][0]) == set(result['results'][0])
        if operation == 'contents':
            assert [v['url'] for v in direct['results']] == [v['url'] for v in result['results']]
    elif operation == 'answer':
        assert '4' in direct.get('answer', '') and '4' in result.get('answer', '')
    else:
        # The list routes use a data array, even when the tenant has no resources.
        assert isinstance(direct.get('data'), list) and isinstance(result.get('data'), list)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument('--key-file', required=True)
    args = parser.parse_args()
    key = Path(args.key_file).read_text().strip()
    for operation, payload in CASES:
        direct = direct_request(key, operation, payload)
        output = wrapped_request(args, operation, payload)
        assert_success(operation, direct, output)
        print('PASS %s: direct and signed Actor success' % operation)


if __name__ == '__main__':
    main()
