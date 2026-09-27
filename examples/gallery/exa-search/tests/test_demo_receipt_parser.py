"""Receipt identity, bounded delivery, and explicit live-success criteria."""
from types import SimpleNamespace

import pytest

import exa_client as client
import receipt_request_id as parser
from check_exa_canyon import assert_success

ACTOR = '0x' + '99' * 20
TX_HASH = '0x' + 'ab' * 32


def receipt(sdk, topic, value, emitter=ACTOR):
    return {'status': 'Success', 'events': [[emitter, topic, '0x' + sdk.codec.encode(value).hex()]]}


@pytest.mark.parametrize('label', ['Transaction', 'Tx'])
def test_transaction_hash(label):
    output = '  %s hash: %s\n' % (label, TX_HASH)
    assert parser.extract_transaction_hash(output) == TX_HASH
    for malformed in ('', TX_HASH, output + output):
        with pytest.raises(ValueError):
            parser.extract_transaction_hash(malformed)


@pytest.mark.parametrize('mutation', ['emitter', 'duplicate', 'failed', 'boolean', 'zero'])
def test_invalid_acceptance(sdk, mutation):
    r = receipt(sdk, parser.EVENT_TOPIC, {'request_id': 7})
    if mutation == 'emitter': r['events'][0][0] = '0x' + '88' * 20
    if mutation == 'duplicate': r['events'] *= 2
    if mutation == 'failed': r['status'] = 'Failure'
    if mutation in ('boolean', 'zero'):
        r = receipt(sdk, parser.EVENT_TOPIC, {'request_id': True if mutation == 'boolean' else 0})
    with pytest.raises(ValueError):
        parser.extract_request_id(r, ACTOR)


def test_canonical_float_and_trailing_bytes(sdk):
    value = {'data': {'costDollars': {'total': 0.005}, 'score': 0.75}}
    encoded = sdk.codec.encode(value)
    assert parser.decode_cbor(encoded) == value
    for bad in (encoded + b'x', encoded[:-1]):
        with pytest.raises(ValueError):
            parser.decode_cbor(bad)


@pytest.mark.parametrize('mutation', ['request_id', 'hire_id', 'offset', 'length', 'total_bytes', 'data', 'failed'])
def test_invalid_chunks(sdk, mutation):
    value = {'request_id': 7, 'hire_id': 42, 'offset': 0, 'length': 1, 'total_bytes': 1, 'data': b'x'}
    if mutation in ('request_id', 'hire_id'): value[mutation] += 1
    if mutation == 'offset': value['offset'] = -1
    if mutation == 'length': value['length'] = 4016
    if mutation == 'total_bytes': value['total_bytes'] = 40001
    if mutation == 'data': value['data'] = b''
    r = receipt(sdk, parser.RESULT_TOPIC, value)
    if mutation == 'failed': r['status'] = 'Failure'
    with pytest.raises(ValueError):
        parser.extract_result_chunk(r, ACTOR, 7, 42)


@pytest.mark.parametrize('mutation', [None, 'id', 'status', 'operation', 'offset', 'total'])
def test_client_reads_only_accepted_result(sdk, monkeypatch, mutation):
    response = {'ok': True, 'status': 'ok', 'request_id': 7, 'operation': 'contents',
                'data': {'text': 'x' * 9000, 'score': 0.75}}
    if mutation == 'id': response['request_id'] = 6
    if mutation == 'status': response['status'] = 'unknown'
    if mutation == 'operation': response['operation'] = 'search'
    calls = []
    def execute(args, handler, payload, deadline):
        calls.append((handler, payload))
        if handler == 'api':
            return receipt(sdk, parser.EVENT_TOPIC, {'request_id': 7})
        assert payload['request_id'] == 7 and payload['hire_id'] == 42
        current = response if len(calls) > 2 else {'ok': True, 'status': 'pending', 'request_id': 7}
        encoded = sdk.codec.encode(current)
        offset = payload['receipt_offset']
        data = encoded[offset:offset + payload['receipt_length']]
        value = {'request_id': 7, 'hire_id': 42, 'offset': offset, 'length': len(data),
                 'total_bytes': len(encoded), 'data': data}
        if offset and mutation == 'offset': value['offset'] -= 1
        if offset and mutation == 'total': value['total_bytes'] += 1
        return receipt(sdk, parser.RESULT_TOPIC, value)
    monkeypatch.setattr(client, 'execute', execute)
    monkeypatch.setattr(client.time, 'sleep', lambda _: None)
    args = SimpleNamespace(actor=ACTOR, hire_id=42)
    if mutation:
        with pytest.raises(ValueError): client.request(args, 'contents', {})
    else:
        assert client.request(args, 'contents', {}) == response
        assert [c[0] for c in calls] == ['api'] + ['get_result'] * 4
    assert sum(c[0] == 'api' for c in calls) == 1


def test_pending_client_has_one_deadline(sdk, monkeypatch):
    now = [0]
    monkeypatch.setattr(client.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(client.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    calls = []
    def execute(args, handler, payload, deadline):
        client.remaining(deadline)
        calls.append(handler)
        if handler == 'api': return receipt(sdk, parser.EVENT_TOPIC, {'request_id': 7})
        data = sdk.codec.encode({'ok': True, 'status': 'pending', 'request_id': 7})
        return receipt(sdk, parser.RESULT_TOPIC, {'request_id': 7, 'hire_id': 42, 'offset': 0,
            'length': len(data), 'total_bytes': len(data), 'data': data})
    monkeypatch.setattr(client, 'execute', execute)
    with pytest.raises(TimeoutError): client.request(SimpleNamespace(actor=ACTOR, hire_id=42), 'search', {})
    assert now[0] == 180 and calls.count('api') == 1


@pytest.mark.parametrize('mutation', [None, 'owner', 'failed', 'duplicate', 'short', 'zero'])
def test_secret_version_uses_exact_receipt(mutation):
    raw = bytes.fromhex(ACTOR[2:]) + b'\xab' * 32 + (9).to_bytes(8, 'big')
    if mutation == 'short': raw = raw[:-1]
    if mutation == 'zero': raw = raw[:-8] + b'\x00' * 8
    r = {'status': 'Success', 'events': [[ACTOR, 'cbss.secret.created', '0x' + raw.hex()]]}
    if mutation == 'failed': r['status'] = 'Failure'
    if mutation == 'duplicate': r['events'] *= 2
    owner = '0x' + '88' * 20 if mutation == 'owner' else ACTOR
    if mutation:
        with pytest.raises(ValueError): parser.extract_secret_version(r, owner)
    else:
        assert parser.extract_secret_version(r, owner) == (TX_HASH, 9)


@pytest.mark.parametrize('direct,wrapped', [
    ({'error': 'invalid key'}, {'ok': False, 'status': 'error', 'error': 'invalid key'}),
    ({'error': 'invalid key'}, {'ok': True, 'status': 'ok', 'operation': 'search', 'data': {'error': 'invalid key'}}),
    ({'results': []}, {'ok': True, 'status': 'ok', 'operation': 'search', 'data': {'results': []}}),
])
def test_live_success_rejects_matching_errors(direct, wrapped):
    with pytest.raises(AssertionError): assert_success('search', direct, wrapped)
