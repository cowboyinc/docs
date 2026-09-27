"""Bounded subprocess driver shared by the cbfs-CLI adapters.

`run_cbfs` executes one cbfs command through a single non-blocking pipe pump
under one wall-clock deadline: stdin is written only as the child drains it,
stdout and stderr are read concurrently so a stderr burst cannot wedge stdout,
caps apply per chunk while draining (stdout overflow kills the child, stderr
is truncated before allocation), exit is awaited under the same deadline, and
every pipe handle is closed in finally.
"""

from __future__ import annotations

import os
import random
import selectors
import subprocess
import time

STDERR_CAP = 64 * 1024
RATE_LIMIT_MARKERS = ("429", "Too Many Requests", "upstream_busy")
# Propagation lag right after another writer's commit: the chain root is
# visible before the manifest-node placement for it is readable.
PROPAGATION_MARKERS = ("manifest node placement unavailable",)
# A relay at its connection limit refuses new QUIC connects; concurrent
# handler processes each open their own. Transient like a rate limit.
CONNECTION_MARKERS = ("refused to accept a new connect", "connection failed: aborted by peer")


class CbfsError(RuntimeError):
    pass


def is_rate_limited(message: str) -> bool:
    return any(m in message for m in RATE_LIMIT_MARKERS)


def is_transient(message: str) -> bool:
    """Never a semantic answer: rate limits and post-commit propagation lag."""
    return is_rate_limited(message) or any(m in message for m in PROPAGATION_MARKERS + CONNECTION_MARKERS)


def run_cbfs(binary: str, state_dir: str, args: list[str], *, rpc_url: str | None = None, extra_args: list[str] = (), stdin: bytes | None = None, timeout_s: float = 120, max_output_bytes: int = 16 * 1024 * 1024, ok_codes=(0,), rate_limit_retries: int = 6) -> subprocess.CompletedProcess:
    """Run one cbfs command. A chain-RPC or relayer rate limit (HTTP 429 /
    upstream_busy) or post-commit propagation lag is never a semantic answer,
    so it is retried here with a jittered exponential backoff before
    surfacing as CbfsError."""
    for attempt in range(rate_limit_retries + 1):
        try:
            return _run_once(binary, state_dir, args, rpc_url=rpc_url, extra_args=extra_args, stdin=stdin, timeout_s=timeout_s, max_output_bytes=max_output_bytes, ok_codes=ok_codes)
        except CbfsError as exc:
            if attempt >= rate_limit_retries or not is_transient(str(exc)) or "timed out" in str(exc) or "exceeds" in str(exc):
                raise
            time.sleep(min(0.25 * (2 ** attempt), 4.0) * (0.5 + random.random()))
    raise AssertionError("unreachable")


def _run_once(binary: str, state_dir: str, args: list[str], *, rpc_url: str | None, extra_args, stdin: bytes | None, timeout_s: float, max_output_bytes: int, ok_codes) -> subprocess.CompletedProcess:
    cmd = [binary, "--json", "--state-dir", state_dir]
    if rpc_url:
        cmd += ["--rpc-url", rpc_url]
    cmd += list(extra_args) + list(args)
    deadline = time.monotonic() + timeout_s
    # The SDK's pending-diff journal is process-global by default; pin it to
    # this state dir so concurrent handler processes never share a journal.
    env = dict(os.environ)
    env.setdefault("CBFS_PENDING_DIFF_DIR", os.path.join(state_dir, "pending-diffs"))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        out, err = _pump(proc, stdin, max_output_bytes, STDERR_CAP, deadline)
    except _Timeout as exc:
        _kill(proc)
        raise CbfsError(f"cbfs {args[0]} timed out after {timeout_s}s") from exc
    except _Overflow as exc:
        _kill(proc)
        raise CbfsError(f"cbfs {args[0]} output exceeds {max_output_bytes} bytes") from exc
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if proc.poll() is None:
            _kill(proc)
    result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if result.returncode not in ok_codes:
        raise CbfsError(f"cbfs {args[0]} failed ({result.returncode}): {result.stderr.decode(errors='replace').strip()[:500]}")
    return result


class _Timeout(Exception):
    pass


class _Overflow(Exception):
    pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _pump(proc: subprocess.Popen, stdin: bytes | None, out_cap: int, err_cap: int, deadline: float) -> tuple[bytes, bytes]:
    """Selector loop over the child's three pipes. Writes stdin only as the
    child accepts it, so a child that never reads cannot block us past the
    deadline; drains stdout/stderr as they arrive so neither can wedge the
    other; applies caps per chunk; waits for exit under the same deadline."""
    out, err = bytearray(), bytearray()
    err_truncated = False
    sel = selectors.DefaultSelector()
    pending = memoryview(stdin) if stdin is not None else None
    if proc.stdin is not None:
        if pending is not None and len(pending):
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
    for pipe in (proc.stdout, proc.stderr):
        os.set_blocking(pipe.fileno(), False)
        sel.register(pipe, selectors.EVENT_READ)
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Timeout()
            for key, _ in sel.select(remaining):
                pipe = key.fileobj
                if pipe is proc.stdin:
                    try:
                        n = os.write(pipe.fileno(), pending[:65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        n = len(pending)  # child stopped reading; nothing more to deliver
                    pending = pending[n:]
                    if not len(pending):
                        sel.unregister(pipe)
                        pipe.close()
                    continue
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(pipe)
                    continue
                if pipe is proc.stdout:
                    out += chunk
                    if len(out) > out_cap:
                        raise _Overflow()
                elif not err_truncated:
                    err += chunk[: max(0, err_cap - len(err))]
                    err_truncated = len(err) >= err_cap
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Timeout()
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _Timeout() from exc
    finally:
        sel.close()
    return bytes(out), bytes(err)
