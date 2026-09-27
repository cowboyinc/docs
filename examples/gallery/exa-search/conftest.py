"""Real SDK execution with a fresh host and restored imports per test."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
OWNER = b'\x22' * 20
BUYER = b'\x33' * 20
OTHER = b'\x44' * 20
ACTOR = b'\x99' * 20


def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sdk():
    lib = Path(os.environ.get('COWBOY_SDK_PATH', HERE.parents[4] / 'node/pvm/Lib'))
    names = lambda: [n for n in sys.modules if n.startswith('cowboy_sdk') or n in ('pvm_host', 'exa_search', 'exa_buyer')]
    saved = {n: sys.modules.pop(n) for n in names()}
    try:
        host = load('pvm_host', lib / 'cowboy_sdk/tests/mock_pvm_host.py')
        package = load('cowboy_sdk', lib / 'cowboy_sdk/__init__.py', package=True)
        module = load('exa_search', HERE / 'actors/exa/main.py')
        host.reset()
        host.set_context(actor_addr=ACTOR, sender=OWNER)
        yield SimpleNamespace(E=module, host=host, codec=package.codec, package=package)
    finally:
        for name in names():
            sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.fixture
def actor(sdk):
    E, host, codec = sdk.E, sdk.host, sdk.codec
    a = E.ExaSearchWrapper()
    a.init(codec.encode({'store_registrar': '0x' + OWNER.hex()}))
    for hire, buyer in ((42, BUYER), (43, OTHER)):
        a.register_hire(codec.encode({'hire_id': hire, 'hirer': '0x' + buyer.hex(),
            'declaration_id': '0x' + '55' * 32, 'version': 3, 'artifact_id': 7}))
    host.set_context(sender=BUYER)
    return a


@pytest.fixture
def start(sdk, actor):
    def run(handler='search', *, buyer=BUYER, hire_id=42, **fields):
        sdk.host.set_context(sender=buyer, tx_hash=(len(sdk.host.get_send_log()) + 1).to_bytes(32, 'big'))
        payload = dict(fields, hire_id=hire_id)
        if handler == 'search':
            payload.setdefault('query', 'Cowboy')
        result = sdk.codec.decode(getattr(actor, handler)(sdk.codec.encode(payload)))
        job = sdk.codec.decode(sdk.host.get_send_log()[-1]['payload']) if result.get('ok') else None
        return result, job
    return run


@pytest.fixture
def resume(sdk):
    def run(job, result, sender=ACTOR):
        sdk.host.set_context(sender=sender)
        return sdk.codec.decode(sdk.E._run_request__resume(sdk.codec.encode({
            'cid': job['cid'], 'reply_handler': job['reply_handler'], 'result': result,
        })))
    return run


@pytest.fixture
def read(sdk, actor):
    def run(request_id, *, buyer=BUYER, hire_id=42, **fields):
        sdk.host.set_context(sender=buyer)
        return sdk.codec.decode(actor.get_result(sdk.codec.encode(dict(fields, request_id=request_id, hire_id=hire_id))))
    return run
