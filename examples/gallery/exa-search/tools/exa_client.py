"""Signed CLI calls and receipt delivery for a single accepted Exa request."""
import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from receipt_request_id import decode_cbor, extract_request_id, extract_result_chunk, extract_transaction_hash


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('request deadline reached; retain the request ID before retrying')
    return seconds


def execute(args, handler, payload, deadline):
    output = subprocess.check_output(
        [args.cowboy, 'actor', 'execute', '--actor', args.actor,
         '--handler', handler, '--payload-json', json.dumps(payload),
         '--private-key', args.wallet_key, '--no-wait'], text=True,
        env=dict(os.environ, COWBOY_RPC_URL=args.rpc), timeout=remaining(deadline),
    )
    tx = extract_transaction_hash(output)
    while True:
        try:
            with urllib.request.urlopen(args.rpc.rstrip('/') + '/transaction/' + tx + '/receipt',
                                        timeout=min(15, remaining(deadline))) as response:
                receipt = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            if receipt.get('status') != 'Success':
                raise RuntimeError('transaction failed: %r' % receipt.get('status'))
            return receipt
        time.sleep(min(2, remaining(deadline)))


def read_result(args, request_id, operation=None, deadline=None):
    deadline = time.monotonic() + 180 if deadline is None else deadline
    while True:
        data = bytearray()
        total = None
        while total is None or len(data) < total:
            receipt = execute(args, 'get_result', {
                'hire_id': args.hire_id, 'request_id': request_id,
                'receipt_offset': len(data), 'receipt_length': 4015,
            }, deadline)
            chunk = extract_result_chunk(receipt, args.actor, request_id, args.hire_id)
            if chunk['offset'] != len(data) or (total is not None and chunk['total_bytes'] != total):
                raise ValueError('result changed during delivery')
            total = chunk['total_bytes']
            data.extend(chunk['data'])
        output = decode_cbor(bytes(data))
        if not isinstance(output, dict) or type(output.get('request_id')) is not int or output['request_id'] != request_id:
            raise ValueError('result does not match the accepted request ID')
        status = output.get('status')
        if status not in ('ok', 'error', 'pending') or output.get('ok') is not (status != 'error'):
            raise ValueError('invalid result status')
        if status != 'pending':
            if status == 'ok' and operation is not None and output.get('operation') != operation:
                raise ValueError('result operation does not match the request')
            return output
        time.sleep(min(4, remaining(deadline)))


def request(args, operation, payload):
    deadline = time.monotonic() + 180
    receipt = execute(args, 'api', dict(payload, hire_id=args.hire_id, operation=operation), deadline)
    request_id = extract_request_id(receipt, args.actor)
    print('Accepted request %d; retain this ID for later get_result calls.' % request_id, flush=True)
    return read_result(args, request_id, operation, deadline)


def add_connection_arguments(parser):
    parser.add_argument('--actor', required=True)
    parser.add_argument('--hire-id', required=True, type=int)
    parser.add_argument('--rpc', required=True)
    parser.add_argument('--wallet-key', required=True)
    parser.add_argument('--cowboy', default='cowboy')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read an existing request through signed result receipts.')
    add_connection_arguments(parser)
    parser.add_argument('--request-id', required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(read_result(args, args.request_id), ensure_ascii=False))
