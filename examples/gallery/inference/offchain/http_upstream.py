"""Real `Upstream` for `inference.Service`: one HTTP POST to an
OpenAI-compatible serving endpoint with the response streamed back chunk by
chunk. Standard library only, so it runs wherever the offchain bundle runs.

Bounds (CIP §4 relay requirements): `connect_timeout_s` for the TCP connect,
`idle_timeout_s` for header wait and for silence between body chunks (this is
NOT the connect timeout; a slow first byte is a legitimate completion),
`max_connection_s` for the whole exchange. Socket timeouts are idle timeouts,
so a peer dripping one header line or one chunk-size byte per interval would
keep http.client's parser alive for ever; a wall-clock watchdog shuts the
socket down at the connection deadline, which wakes any blocked read.
Name resolution inside connect() is not covered by any of these bounds; an
executor that needs a hard wall-clock bound on DNS must supply it (resolved
targets, or its own resolver timeout). Serving-endpoint credentials come from
the deployment (`headers`), never from the client request.
"""

from __future__ import annotations

import http.client
import math
import socket
import threading
import time
from typing import Iterator
from urllib.parse import urlsplit

from .inference import UpstreamResponse


class UpstreamTimeout(Exception):
    pass


class HttpUpstream:
    def __init__(self, *, headers: dict | None = None, connect_timeout_s: float = 10, idle_timeout_s: float = 120, max_connection_s: float = 600, read_size: int = 16384, clock=time.monotonic):
        for name, value in (("connect_timeout_s", connect_timeout_s), ("idle_timeout_s", idle_timeout_s), ("max_connection_s", max_connection_s)):
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive finite number")
        if not (isinstance(read_size, int) and read_size > 0):
            raise ValueError("read_size must be a positive integer")
        self.headers = dict(headers or {})
        self.connect_timeout_s = connect_timeout_s
        self.idle_timeout_s = idle_timeout_s
        self.max_connection_s = max_connection_s
        self.read_size = read_size
        self.clock = clock
        # Injectable for tests that need to observe close() without a socket.
        self.connection_classes = {"http": http.client.HTTPConnection, "https": http.client.HTTPSConnection}

    def request(self, target: str, body: bytes) -> UpstreamResponse:
        url = urlsplit(target)
        if url.scheme not in self.connection_classes or not url.hostname:
            raise ValueError(f"bad upstream target {target!r}")
        deadline = self.clock() + self.max_connection_s
        conn = self.connection_classes[url.scheme](url.hostname, url.port, timeout=min(self.connect_timeout_s, self._remaining(deadline)))
        try:
            conn.connect()
        except (OSError, socket.timeout) as exc:
            conn.close()
            raise ConnectionError(f"connect failed: {exc}") from exc
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", "content-length": str(len(body)), **self.headers}
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        # http.client drops conn.sock once a will-close response is read, so
        # keep our own handle for per-stage timeouts and the watchdog.
        sock = conn.sock
        expired = threading.Event()

        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        watchdog = None
        resp = None

        def closer():
            if watchdog is not None:
                watchdog.cancel()
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            conn.close()

        try:
            # connect() is bounded by the socket timeout only once a socket
            # exists; name resolution inside it is not, so the connection may
            # come back after the deadline. Everything from here runs under
            # the cleanup path so a late connect is closed, not leaked.
            watchdog = threading.Timer(self._remaining(deadline), expire)
            watchdog.daemon = True
            watchdog.start()
            sock.settimeout(self._stage(deadline))
            conn.request("POST", path, body=body, headers=headers)
            sock.settimeout(self._stage(deadline))
            resp = conn.getresponse()
            self._remaining(deadline)
        except (socket.timeout, UpstreamTimeout) as exc:
            closer()
            raise UpstreamTimeout("no response headers within budget") from exc
        except (OSError, http.client.HTTPException) as exc:
            closer()
            if expired.is_set():
                raise UpstreamTimeout("max_connection_s exceeded during headers") from exc
            raise ConnectionError(f"request failed: {exc}") from exc
        return UpstreamResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, self._chunks(sock, resp, closer, expired, deadline), closer=closer)

    def _remaining(self, deadline: float) -> float:
        left = deadline - self.clock()
        if left <= 0:
            raise UpstreamTimeout("max_connection_s exceeded")
        return left

    def _stage(self, deadline: float) -> float:
        """Timeout for one blocking read: the idle budget, capped by what is
        left of the connection budget."""
        return min(self.idle_timeout_s, self._remaining(deadline))

    def _chunks(self, sock, resp: http.client.HTTPResponse, closer, expired: threading.Event, deadline: float) -> Iterator[bytes]:
        """Yield bytes as they arrive. Closing the generator closes the
        connection, which is how client disconnect cancels the upstream read."""
        try:
            while True:
                if resp.isclosed():
                    return  # body fully consumed; http.client already released the socket
                sock.settimeout(self._stage(deadline))
                try:
                    chunk = resp.read1(self.read_size)
                except socket.timeout as exc:
                    raise UpstreamTimeout("idle timeout between chunks") from exc
                except (OSError, http.client.HTTPException) as exc:
                    if expired.is_set():
                        raise UpstreamTimeout("max_connection_s exceeded during body") from exc
                    raise
                self._remaining(deadline)
                if not chunk:
                    return
                yield chunk
        finally:
            closer()
