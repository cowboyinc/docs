from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.cache.main import OracleCache


class _HttpResult:
    def __init__(self, raw):
        self.raw = raw
        self.text = ""


def test_init_starts_idle_with_source_url() -> None:
    actor = OracleCache()
    chain = SimulatedChain(actor)
    reply = codec.decode(chain.call(actor.init, b'{"source_url":"https://example.com/eth.json"}'))
    assert reply == {"status": "initialized", "source_url": "https://example.com/eth.json"}
    assert codec.decode(chain.state("status")) == "idle"
    status = codec.decode(chain.call(actor.get_status, codec.encode({})))
    assert status == {"status": "idle", "refresh_count": 0}


def test_request_refresh_marks_pending_and_dispatches_http_job() -> None:
    actor = OracleCache()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"source_url":"https://example.com/eth.json"}')
    chain.call(actor.request_refresh, codec.encode({}))
    assert codec.decode(chain.state("status")) == "pending"
    assert codec.decode(chain.state("refresh_count")) == 1
    job = codec.decode(mock_host.get_send_log()[-1]["payload"])
    assert job["job_type"] == "http"
    assert job["payload"]["args"] == ["https://example.com/eth.json"]
    assert job["reply_handler"] == "_refresh_cache__resume"


def test_finalize_refresh_normalizes_and_caches_record() -> None:
    actor = OracleCache()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    result = _HttpResult({"symbol": "eth", "value": 3500, "timestamp": "2026-01-01"})
    reply = codec.decode(actor._finalize_refresh({"refresh_count": 1}, result))
    record = {"symbol": "ETH", "price": 3500, "as_of": "2026-01-01"}
    assert reply == {"status": "ready", "refresh_count": 1, "record": record}
    assert codec.decode(chain.state("cache")) == record
    assert chain.events()[-1][0] == "oracle_cache.refreshed"
    cached = codec.decode(chain.call(actor.get_cached_record, codec.encode({})))
    assert cached == {"status": "cache_hit", "record": record}


def test_get_cached_record_reports_miss_before_refresh() -> None:
    actor = OracleCache()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    assert codec.decode(chain.call(actor.get_cached_record, codec.encode({}))) == {"status": "cache_miss"}
