"""HttpUpstream against a local fake OpenAI-compatible serving endpoint."""

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offchain.http_upstream import HttpUpstream, UpstreamTimeout


class Fake(BaseHTTPRequestHandler):
    """Behavior selected by path. Records the last request body and headers."""

    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        Fake.seen.append((self.path, dict(self.headers), json.loads(body)))
        if self.path == "/json":
            out = json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif self.path == "/slow-first-byte":
            time.sleep(0.6)  # longer than the connect timeout the test uses
            out = b'{"choices":[]}'
            self.send_response(200)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif self.path == "/sse":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for i in range(3):
                frame = b'data: {"i":%d}\n\n' % i
                self.wfile.write(b"%x\r\n%s\r\n" % (len(frame), frame))
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/stall":
            self.send_response(200)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"5\r\nhello\r\n")
            self.wfile.flush()
            time.sleep(2)  # never sends more within the idle timeout
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/never-headers":
            time.sleep(2)
        else:
            self.send_response(500)
            self.send_header("content-length", "4")
            self.end_headers()
            self.wfile.write(b"boom")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_json_response_and_deployment_headers(server):
    up = HttpUpstream(headers={"authorization": "Bearer upstream-secret"})
    r = up.request(server + "/json", b'{"model":"m"}')
    assert r.status == 200 and r.headers["content-type"] == "application/json"
    assert json.loads(b"".join(r.chunks))["usage"]["completion_tokens"] == 2
    path, headers, body = Fake.seen[-1]
    assert headers["authorization"] == "Bearer upstream-secret" and body == {"model": "m"}


def test_header_wait_is_bounded_by_idle_not_connect_timeout(server):
    up = HttpUpstream(connect_timeout_s=0.2, idle_timeout_s=5)
    r = up.request(server + "/slow-first-byte", b"{}")
    assert r.status == 200 and b"".join(r.chunks) == b'{"choices":[]}'


def test_sse_chunks_arrive_incrementally(server):
    up = HttpUpstream()
    r = up.request(server + "/sse", b"{}")
    seen = list(r.chunks)
    assert len(seen) >= 3 and b"".join(seen).count(b"\n\n") == 3


def test_idle_timeout_between_chunks_and_no_headers(server):
    up = HttpUpstream(idle_timeout_s=0.3)
    r = up.request(server + "/stall", b"{}")
    it = r.chunks
    assert next(it) == b"hello"
    with pytest.raises(UpstreamTimeout):
        next(it)
    with pytest.raises(UpstreamTimeout):
        up.request(server + "/never-headers", b"{}")


def test_max_connection_bound(server):
    t = {"now": 0.0}
    up = HttpUpstream(max_connection_s=1, clock=lambda: t["now"])
    r = up.request(server + "/sse", b"{}")
    next(r.chunks)
    t["now"] = 5.0
    with pytest.raises(UpstreamTimeout):
        next(r.chunks)


def test_closing_the_iterator_closes_the_connection(server):
    up = HttpUpstream()
    r = up.request(server + "/sse", b"{}")
    next(r.chunks)
    r.close()
    with pytest.raises(StopIteration):
        next(r.chunks)


def test_non_200_and_connect_failure(server):
    up = HttpUpstream(connect_timeout_s=0.5)
    r = up.request(server + "/error", b"{}")
    assert r.status == 500 and b"".join(r.chunks) == b"boom"
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    with pytest.raises(ConnectionError):
        up.request(f"http://127.0.0.1:{port}/json", b"{}")
    with pytest.raises(ValueError):
        up.request("ftp://x/y", b"{}")


class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def shutdown(self, how):
        pass


class FakeConn:
    """Records close() so the never-pulled path can be observed without a socket."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.sock = FakeSock()
        self.closed = 0
        self.timeout = timeout
        FakeConn.instances.append(self)

    def connect(self):
        pass

    def request(self, *a, **k):
        pass

    def getresponse(self):
        conn = self

        class R:
            status = 500

            def getheaders(self):
                return []

            def isclosed(self):
                return False

            def read1(self, n):
                return b""

            def close(self):
                pass
        return R()

    def close(self):
        self.closed += 1


def test_close_releases_connection_before_first_pull():
    up = HttpUpstream()
    up.connection_classes = {"http": FakeConn}
    r = up.request("http://fake/x", b"{}")
    assert r.status == 500
    conn = FakeConn.instances[-1]
    assert conn.closed == 0
    r.close()
    assert conn.closed >= 1
    r.close()  # idempotent


def test_every_stage_is_capped_by_the_connection_budget():
    t = {"now": 100.0}
    up = HttpUpstream(connect_timeout_s=10, idle_timeout_s=50, max_connection_s=7, clock=lambda: t["now"])
    up.connection_classes = {"http": FakeConn}
    r = up.request("http://fake/x", b"{}")
    conn = FakeConn.instances[-1]
    assert conn.timeout == 7 and all(0 < x <= 7 for x in conn.sock.timeouts)
    t["now"] = 108.0
    with pytest.raises(UpstreamTimeout):
        next(r.chunks)
    assert conn.closed >= 1


def test_header_wait_respects_max_connection(server):
    up = HttpUpstream(idle_timeout_s=5, max_connection_s=0.3)
    started = time.monotonic()
    with pytest.raises(UpstreamTimeout):
        up.request(server + "/never-headers", b"{}")
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("kw", [dict(connect_timeout_s=0), dict(idle_timeout_s=-1), dict(max_connection_s=float("inf")), dict(read_size=0)])
def test_bounds_are_validated(kw):
    with pytest.raises(ValueError):
        HttpUpstream(**kw)


def drip_server(lines, interval, body_drip=None):
    """Raw TCP server that sends one header line per `interval`, then optionally
    drips a chunked body one piece per `interval`. Returns (port, stop)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stopped = threading.Event()

    def run():
        srv.settimeout(5)
        try:
            c, _ = srv.accept()
        except OSError:
            return
        try:
            c.settimeout(5)
            c.recv(65536)
            for ln in lines:
                if stopped.is_set():
                    return
                c.sendall(ln)
                time.sleep(interval)
            for b in body_drip or []:
                if stopped.is_set():
                    return
                c.sendall(b)
                time.sleep(0.08)
        except OSError:
            pass
        finally:
            c.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], lambda: (stopped.set(), srv.close())


def test_dripped_headers_hit_the_wall_clock_deadline():
    lines = [b"HTTP/1.1 200 OK\r\n"] + [b"x-h%d: v\r\n" % i for i in range(40)] + [b"\r\n"]
    port, stop = drip_server(lines, 0.08)
    try:
        up = HttpUpstream(idle_timeout_s=2, max_connection_s=0.15)
        started = time.monotonic()
        with pytest.raises(UpstreamTimeout):
            up.request(f"http://127.0.0.1:{port}/x", b"{}")
        assert time.monotonic() - started < 1.5
    finally:
        stop()


def test_dripped_chunk_header_hits_the_wall_clock_deadline():
    head = [b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"]
    # A chunk-size line dripped one byte at a time keeps the chunk parser reading.
    body = [bytes([b]) for b in b"5\r\nhello\r\n" + b"1" * 40]
    port, stop = drip_server(head, 0.0, body_drip=body)
    try:
        up = HttpUpstream(idle_timeout_s=2, max_connection_s=0.3)
        r = up.request(f"http://127.0.0.1:{port}/x", b"{}")
        started = time.monotonic()
        with pytest.raises(UpstreamTimeout):
            for _ in r.chunks:
                pass
        assert time.monotonic() - started < 2
    finally:
        stop()


def test_late_connect_is_closed_not_leaked():
    t = {"now": 0.0}
    up = HttpUpstream(max_connection_s=1, clock=lambda: t["now"])

    class SlowConn(FakeConn):
        def connect(self):
            t["now"] = 5.0  # e.g. name resolution took longer than the whole budget

    up.connection_classes = {"http": SlowConn}
    with pytest.raises(UpstreamTimeout):
        up.request("http://fake/x", b"{}")
    assert FakeConn.instances[-1].closed >= 1
