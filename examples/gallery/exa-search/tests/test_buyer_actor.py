"""SDK execution-harness proof for the real Exa buyer Actor."""

import importlib.util
import os



HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER_ADDRESS = b"\x99" * 20
OWNER = b"\x11" * 20


def _load_buyer():
    spec = importlib.util.spec_from_file_location("exa_buyer", os.path.join(HERE, "actors/buyer/main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _setup(sdk):
    from cowboy_sdk import Address, codec, mock_host
    from cowboy_sdk.testing import SimulatedChain
    buyer_module = _load_buyer()
    buyer = buyer_module.ExaSearchBuyer()
    chain = SimulatedChain(buyer, sender=OWNER)
    buyer_address = bytes(buyer.address.as_ref())

    class HarnessWrapper(sdk.E.ExaSearchWrapper):
        def __init__(self):
            self._address = Address(WRAPPER_ADDRESS)
            self._harness_storage = {}

        @property
        def storage(self):
            return self._harness_storage

    wrapper = HarnessWrapper()
    mock_host.set_context(actor_addr=WRAPPER_ADDRESS, sender=OWNER)
    wrapper.init(codec.encode({"store_registrar": "0x" + OWNER.hex()}))
    wrapper.register_hire(
        codec.encode(
            {
                "hire_id": 42,
                "hirer": "0x" + buyer_address.hex(),
                "declaration_id": "0x" + "55" * 32,
                "version": 3,
                "artifact_id": 7,
            }
        )
    )

    mock_host.set_context(actor_addr=buyer_address, sender=OWNER)
    chain.call(
        "init",
        codec.encode({"wrapper": "0x" + WRAPPER_ADDRESS.hex(), "hire_id": 42}),
    )

    calls = []

    def route(target, method, payload, cycles_limit, access_limit):
        calls.append((bytes(target), method, codec.decode(payload), cycles_limit))
        mock_host.set_context(tx_hash=len(calls).to_bytes(32, "big"))
        before = mock_host.context()
        mock_host.set_context(actor_addr=WRAPPER_ADDRESS, sender=buyer_address)
        try:
            return getattr(wrapper, method)(payload)
        finally:
            mock_host.set_context(**before)

    mock_host.set_call_handler(route)
    return chain, buyer, buyer_address, wrapper, calls


def _resume_latest(wrapper, result):
    from cowboy_sdk import codec, mock_host
    job = codec.decode(mock_host.get_send_log()[-1]["payload"])
    before = mock_host.context()
    mock_host.set_context(actor_addr=WRAPPER_ADDRESS, sender=WRAPPER_ADDRESS)
    try:
        wrapper._run_request__resume(
            codec.encode(
                {
                    "cid": job["cid"],
                    "result": result,
                    "reply_handler": "_run_request__resume",
                }
            )
        )
    finally:
        mock_host.set_context(**before)


def test_buyer_actor_receives_success_and_error_by_request_id(sdk):
    codec = sdk.codec
    chain: "SimulatedChain"
    chain, buyer, buyer_address, wrapper, calls = _setup(sdk)

    started = codec.decode(chain.call("start", codec.encode({"query": "Cowboy"})))
    assert started == {
        "ok": True,
        "request_id": 1,
        "status": "pending",
        "poll_handler": "get_result",
    }
    assert calls[-1][0] == WRAPPER_ADDRESS and calls[-1][1] == "search"
    assert wrapper._request(1)["buyer"] == "0x" + buyer_address.hex()

    _resume_latest(
        wrapper,
        {"results": [{"title": "Cowboy", "url": "https://cowboy.example"}]},
    )
    result = codec.decode(chain.call("poll", b""))
    assert result["status"] == "ok"
    assert result["request_id"] == 1
    assert result["results"][0]["title"] == "Cowboy"
    assert buyer.storage["last_result"] == result
    assert calls[-1][1] == "get_result" and calls[-1][2] == {
        "hire_id": 42,
        "request_id": 1,
    }

    started = codec.decode(chain.call("start", codec.encode({"query": "error"})))
    assert started["request_id"] == 2
    _resume_latest(wrapper, {"error": "vendor unavailable"})
    error = codec.decode(chain.call("poll", b""))
    assert error["status"] == "error" and "vendor unavailable" in error["error"]
