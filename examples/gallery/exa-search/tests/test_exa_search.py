"""Request, permission, result, and capacity contracts through the real SDK."""
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from conftest import BUYER, OTHER, OWNER

ROUTES = json.loads((Path(__file__).parent.parent / 'exa_routes.json').read_text())['routes']


@pytest.mark.parametrize('operation,spec', ROUTES.items())
def test_routes_emit_valid_jobs(start, sdk, operation, spec):
    _, method, path = spec
    fields = {'id': 'id value', 'runId': 'run value', 'webset': 'web value', 'monitor': 'monitor value'}
    accepted, job = start('api', operation=operation, **fields)
    assert accepted['status'] == 'pending'
    args = job['payload']['kwargs']
    expected = path.format(**{k: quote(v, safe='') for k, v in fields.items()})
    assert args['url'] == 'https://api.exa.ai' + expected
    assert args['method'] == method
    assert args['headers']['x-api-key'] == '${EXA_API_KEY}'
    assert args['secrets'] == [['0x' + OWNER.hex(), 'EXA_API_KEY']]
    assert args['hire'] == {'hire_id': 42, 'declaration_id': '0x' + '55' * 32, 'version': 3, 'artifact_id': 7}
    if method in ('GET', 'DELETE'):
        assert 'body' not in args  # Node rejects a present, empty JSON body.
    else:
        body = json.loads(args['body'])
        assert body == {**{k: v for k, v in fields.items() if '{' + k + '}' not in path},
                        **({'stream': False} if operation in ('search', 'answer') else {})}
    assert sdk.E.API_OPERATIONS.keys() == ROUTES.keys()


def test_body_passthrough_and_query_encoding(start):
    _, job = start('api', operation='answer', query='2+2', stream=True, nested={'a': [1, True]})
    assert json.loads(job['payload']['kwargs']['body']) == {'query': '2+2', 'stream': False, 'nested': {'a': [1, True]}}
    _, job = start('api', operation='list_monitors', query_params={'name': 'C/C++? # café', 'active': True, 'tag': ['a/b', '']})
    assert job['payload']['kwargs']['url'] == 'https://api.exa.ai/monitors?active=true&name=C%2FC%2B%2B%3F%20%23%20caf%C3%A9&tag=a%2Fb&tag='


@pytest.mark.parametrize('handler,fields', [
    ('search', {'query': '${EXA_API_KEY}'}),
    ('search', {'start_published_date': '${EXA_API_KEY}'}),
    ('api', {'operation': 'create_webhook', 'metadata': {'nested': ['${EXA_API_KEY}']}}),
    ('api', {'operation': 'create_webhook', '${EXA_API_KEY}': 'key'}),
    ('api', {'operation': 'list_monitors', 'query_params': {'name': '${EXA_API_KEY}'}}),
])
def test_caller_templates_never_dispatch(actor, sdk, handler, fields):
    with pytest.raises(ValueError, match='secret templates'):
        getattr(actor, handler)(sdk.codec.encode(dict(fields, hire_id=42)))
    assert not sdk.host.get_send_log()
    assert actor.storage['call_count'] == 0


@pytest.mark.parametrize('fields', [
    {'operation': 'get_monitor', 'id': '..'}, {'operation': 'get_monitor', 'id': '../x'},
    {'operation': 'missing'}, {'operation': 'answer', 'query': 'x' * 28000},
    {'operation': 'list_monitors', 'query_params': {'x': {}}},
])
def test_invalid_api_does_not_dispatch(actor, sdk, fields):
    with pytest.raises(ValueError):
        actor.api(sdk.codec.encode(dict(fields, hire_id=42)))
    assert not sdk.host.get_send_log()
    assert actor.storage['call_count'] == 0


@pytest.mark.parametrize('handler,fields', [('search', {'query': 'q'}), ('api', {'operation': 'search', 'query': 'q'})])
def test_hire_permissions(actor, sdk, handler, fields):
    handler_call = getattr(actor, handler)
    assert sdk.codec.decode(handler_call(sdk.codec.encode(dict(fields, hire_id=99))))['ok'] is False
    sdk.host.set_context(sender=OTHER)
    with pytest.raises(ValueError, match='registered hirer'):
        handler_call(sdk.codec.encode(dict(fields, hire_id=42)))
    assert not sdk.host.get_send_log()


def test_registration_and_owner_permissions(actor, sdk):
    hire = {'hire_id': 42, 'hirer': '0x' + BUYER.hex(), 'declaration_id': '0x' + '55' * 32, 'version': 3, 'artifact_id': 7}
    with pytest.raises(ValueError, match='registrar'):
        actor.register_hire(sdk.codec.encode(hire))
    with pytest.raises(sdk.package.errors.PermissionDeniedError):
        actor.set_limits(b'{}')
    sdk.host.set_context(sender=OWNER)
    assert sdk.codec.decode(actor.register_hire(sdk.codec.encode(hire)))['hire_id'] == 42
    assert actor.storage['hire_count'] == 2
    with pytest.raises(ValueError, match='different data'):
        actor.register_hire(sdk.codec.encode(dict(hire, artifact_id=8)))
    with pytest.raises(ValueError, match='already initialized'):
        actor.init(b'{}')
    assert sdk.codec.decode(actor.set_limits(sdk.codec.encode({'num_results': 3})))['num_results'] == 3


def test_search_maps_options_and_preserves_hire_pins(start):
    _, job = start(search_type='FAST', category='news', include_domains=['Exa.AI', 'exa.ai'],
        user_location='us', start_published_date='vendor-validates-this', declaration_id='attacker', version=999)
    args = job['payload']['kwargs']
    body = json.loads(args['body'])
    assert body['type'] == 'fast' and body['category'] == 'news'
    assert body['includeDomains'] == ['exa.ai'] and body['userLocation'] == 'US'
    assert body['startPublishedDate'] == 'vendor-validates-this'
    assert args['hire']['version'] == 3 and args['hire']['declaration_id'] == '0x' + '55' * 32
    assert 1 <= body['numResults'] <= 10 and body['contents']['text']['maxCharacters'] > 0


@pytest.mark.parametrize('fields', [{'search_type': 'deep'}, {'category': 'blog'}, {'include_domains': 'exa.ai'},
    {'include_domains': ['https://exa.ai']}, {'category': 'people', 'include_domains': ['a.com']},
    {'category': 'company', 'exclude_domains': ['a.com']}, {'user_location': 'USA'}])
def test_invalid_search_options(actor, sdk, fields):
    with pytest.raises(ValueError):
        actor.search(sdk.codec.encode(dict(fields, hire_id=42, query='q')))
    assert not sdk.host.get_send_log()


@pytest.mark.parametrize('query', ['', ' ', 'x' * 513])
def test_invalid_query_returns_error(actor, sdk, query):
    assert sdk.codec.decode(actor.search(sdk.codec.encode({'hire_id': 42, 'query': query})))['ok'] is False
    assert not sdk.host.get_send_log()


def test_success_failure_and_overlapping_results(start, resume, read):
    first, job_a = start(query='alpha')
    second, job_b = start(buyer=OTHER, hire_id=43, query='beta')
    resume(job_b, {'error': 'quota exceeded'})
    resume(job_a, {'results': [{'title': 'T', 'url': 'https://a', 'text': 'x' * 1000}]})
    assert read(first['request_id'])['results'][0]['text'] == 'x' * 1000
    assert read(first['request_id'], hire_id=43)['ok'] is False
    assert read(second['request_id'], buyer=OTHER, hire_id=43)['error'] == 'quota exceeded'
    cross = read(first['request_id'], buyer=OTHER, hire_id=43)
    assert cross == read(999, buyer=OTHER, hire_id=43) == {'ok': False, 'error': 'request not found for caller and hire'}


def test_search_preserves_exact_url_when_response_fits(start, resume, read):
    url = 'https://example.com/' + ('a' * 1_100) + '?id=exact'
    accepted, job = start(query='exact URL')
    completed = resume(job, {'results': [{'title': 'T', 'url': url, 'text': 'short'}]})
    assert completed['status'] == 'ok'
    assert read(accepted['request_id'])['results'][0]['url'] == url


def test_search_normalizes_null_url_to_empty(start, resume, read):
    accepted, job = start(query='null URL')
    completed = resume(job, {'results': [{'title': 'T', 'url': None, 'text': 'short'}]})
    assert completed['status'] == 'ok'
    assert read(accepted['request_id'])['results'][0]['url'] == ''


def test_search_rejects_url_that_cannot_fit_result_budget(start, resume, read):
    accepted, job = start(query='oversized URL')
    completed = resume(job, {'results': [{'url': 'https://example.com/' + ('a' * 40_000)}]})
    assert completed['status'] == 'error'
    assert completed['error'] == 'vendor result exceeds the encoded result byte budget'
    assert read(accepted['request_id']) == completed


@pytest.mark.parametrize('result', [{'error': 'quota'}, {'detail': {'message': 'invalid key'}},
    {'raw': 'PGh0bWw+', 'content_type': 'text/html'}, 'not JSON', None])
def test_api_errors_are_retained(start, resume, read, result):
    accepted, job = start('api', operation='search', query='q')
    resume(job, result)
    output = read(accepted['request_id'])
    assert output['status'] == 'error' and output['ok'] is False and output['error']


@pytest.mark.parametrize('result', [{'data': [{'id': 'x'}], 'requestId': 'r'}, b'{"results":[]}', '{"results":[]}'])
def test_supported_runner_shapes(start, resume, read, result):
    accepted, job = start('api', operation='search', query='q')
    resume(job, result)
    assert read(accepted['request_id'])['data'] == (result if isinstance(result, dict) else {'results': []})


def test_callback_permissions_duplicates_and_expiry(start, resume, read, sdk, actor):
    accepted, job = start()
    assert resume(job, {'results': []}, sender=OTHER)['ok'] is False
    assert read(accepted['request_id'])['status'] == 'pending'
    assert resume(job, {'results': []})['status'] == 'ok'
    assert resume(job, {'results': []})['error'] == 'stale or duplicate callback'
    accepted, job = start()
    sdk.host.set_block_height(actor._request(accepted['request_id'])['deadline_height'])
    assert read(accepted['request_id'])['status'] == 'error'
    assert resume(job, {'results': []})['error'] == 'stale or duplicate callback'


def test_late_callback_at_deadline_is_an_error(start, resume, read, sdk, actor):
    accepted, job = start()
    sdk.host.set_block_height(actor._request(accepted['request_id'])['deadline_height'])
    assert resume(job, {'results': []})['status'] == 'error'
    assert read(accepted['request_id'])['status'] == 'error'


def test_same_buyer_concurrency_and_duplicate_transaction(start, resume, read, sdk, actor):
    a, job_a = start()
    duplicate = sdk.codec.decode(actor.api(sdk.codec.encode({'hire_id': 42, 'operation': 'search'})))
    assert duplicate['ok'] is False
    b, job_b = start('api', operation='search', query='b')
    assert a['request_id'] != b['request_id'] and job_a['cid'] != job_b['cid']
    resume(job_b, {'results': []})
    resume(job_a, {'results': []})
    assert read(a['request_id'])['status'] == read(b['request_id'])['status'] == 'ok'


@pytest.mark.parametrize('failed', [False, True])
def test_retained_results_survive_capacity_pressure(start, resume, read, sdk, actor, failed):
    first, job = start('api', operation='search', query='large')
    resume(job, {'error': 'quota'} if failed else {'text': 'x' * 8000})
    expected = read(first['request_id'])
    if not failed:
        read(first['request_id'], receipt_offset=0, receipt_length=4015)
    for _ in range(15):
        start(buyer=OTHER, hire_id=43)
    refused, _ = start(buyer=OTHER, hire_id=43)
    assert refused['ok'] is False and 'capacity' in refused['error']
    assert read(first['request_id']) == expected
    if not failed:
        read(first['request_id'], receipt_offset=4015, receipt_length=4015)
    sdk.host.set_block_height(expected['expires_height'])
    accepted, _ = start(buyer=OTHER, hire_id=43)
    assert accepted['ok'] is True
    assert not actor._request(first['request_id'])


def test_bounded_encoded_responses_and_total_storage(start, resume, read, sdk, actor):
    sdk.host.set_context(sender=OWNER)
    for hire_id in range(44, 554):
        actor.register_hire(sdk.codec.encode({'hire_id': hire_id, 'hirer': '0x' + BUYER.hex(),
            'declaration_id': '0x' + '55' * 32, 'version': 3, 'artifact_id': 7}))
    with pytest.raises(ValueError, match='capacity'):
        actor.register_hire(sdk.codec.encode({'hire_id': 555, 'hirer': '0x' + BUYER.hex(),
            'declaration_id': '0x' + '55' * 32, 'version': 3, 'artifact_id': 7}))
    for _ in range(16):
        accepted, job = start('api', operation='contents', urls=['https://example.com'])
        resume(job, {'data': [0.0] * 4400})
        output = read(accepted['request_id'])
        assert output['status'] == 'ok'
        assert 39000 < len(sdk.codec.encode(output)) < 40000
    values = list(sdk.host._storage.items())
    assert max(len(v) for _, v in values) <= 65536
    assert sum(len(k) + len(v) for k, v in values) < 1048576


@pytest.mark.parametrize('num', [1, 5, 10])
def test_search_unicode_request_and_response_budget(start, resume, read, sdk, num):
    accepted, job = start(num_results=num, chars_per_result=20000)
    body = json.loads(job['payload']['kwargs']['body'])
    response = {'results': [{'title': 'T', 'url': 'https://example.com',
        'text': '😀' * body['contents']['text']['maxCharacters']} for _ in range(body['numResults'])]}
    assert len(sdk.codec.encode(response)) < 40000
    resume(job, response)
    assert len(sdk.codec.encode(read(accepted['request_id']))) < 40000


@pytest.mark.parametrize('reply,status,truncated', [
    ({'error': '😀' * 37000}, 'error', False),
    ({'data': [0] * 27000}, 'ok', True),
    ({'text': 'é' * 30000}, 'ok', True),
    ({'data': [0.0] * 4500}, 'error', False),
])
def test_large_api_envelopes_remain_readable(start, resume, read, sdk, reply, status, truncated):
    accepted, job = start('api', operation='contents', urls=['https://example.com'])
    resume(job, reply)
    output = read(accepted['request_id'])
    assert output['status'] == status
    if truncated:
        assert output['data']['truncated'] is True
        assert len(output['data']['raw_json'].encode()) <= 28000
    assert len(sdk.codec.encode(output)) <= 40000


def test_receipt_event_boundary(sdk):
    E = sdk.E
    event = E._result_chunk_event(2**64 - 1, 2**64 - 1, 39999, 40000, b'x' * 4015)
    assert len(event) == 4096
    assert len(E._result_chunk_event(2**64 - 1, 2**64 - 1, 39999, 40000, b'x' * 4016)) == 4097
