"""Minimal CPython test double for SDK surfaces used by the dashboard.

The repository's scheduled sweep uses the real SDK from the private node
checkout. Pull-request CI cannot receive that cross-repository credential, so
the local unit suites use this deliberately small boundary double instead.
"""

from __future__ import annotations

import sys
import types


VERBS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "ANY"]


class AddressError(Exception):
    pass


class _Runner:
    def continuation(self, fn=None, **_kwargs):
        return (lambda value: value) if fn is None else fn

    async def http(self, *_args, **_kwargs):
        return {}

    async def llm(self, *_args, **_kwargs):
        return {}


def _to_address_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        decoded = bytes(value)
    elif isinstance(value, str):
        raw = value[2:] if value.startswith(("0x", "0X")) else value
        if len(raw) != 40:
            raise AddressError("address must have 40 hex characters")
        try:
            decoded = bytes.fromhex(raw)
        except ValueError as exc:
            raise AddressError("address must be hex") from exc
    else:
        raise AddressError("unsupported address")
    if len(decoded) != 20:
        raise AddressError("address must be 20 bytes")
    return decoded


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda value: value


def install() -> None:
    try:
        sdk = __import__("cowboy_sdk")
        routes = __import__("cowboy_sdk.routes", fromlist=["collect_routes"])
        if all(hasattr(sdk, name) for name in ("call", "codec", "runtime")) and all(
            hasattr(routes, name)
            for name in (
                "VERBS",
                "collect_routes",
                "get",
                "post",
                "put",
                "patch",
                "delete",
            )
        ):
            return
    except ImportError:
        pass

    sdk = types.ModuleType("cowboy_sdk")
    routes = types.ModuleType("cowboy_sdk.routes")
    runtime = types.SimpleNamespace(
        call_actor=lambda *_args, **_kwargs: b"{}",
        get_block_height=lambda: 100,
        get_timestamp_ms=lambda: 1_700_000_000_000,
        schedule_timer_ex=lambda *_args, **_kwargs: None,
    )

    def call(target, handler, payload, cycles_limit, access_limit=None, **_kwargs):
        if isinstance(target, str) and target.startswith("0x"):
            target = bytes.fromhex(target[2:])
        return runtime.call_actor(target, handler, payload, cycles_limit, access_limit)

    def route(verbs, path, *, pays="actor", price=None, priority=0):
        if isinstance(verbs, str):
            verbs = (verbs,)

        def decorate(fn):
            specs = tuple(getattr(fn, "_http_routes", ()))
            fn._http_routes = specs + (
                types.SimpleNamespace(
                    verbs=tuple(str(verb).upper() for verb in verbs),
                    path=path,
                    pays=pays,
                    price=price,
                    priority=priority,
                ),
            )
            return fn

        return decorate

    def get(path, **kwargs):
        return route(("GET", "HEAD"), path, **kwargs)

    def post(path, **kwargs):
        return route("POST", path, **kwargs)

    def put(path, **kwargs):
        return route("PUT", path, **kwargs)

    def patch(path, **kwargs):
        return route("PATCH", path, **kwargs)

    def delete(path, **kwargs):
        return route("DELETE", path, **kwargs)

    def collect_routes(actor_class):
        found = []
        for name in dir(actor_class):
            value = getattr(actor_class, name, None)
            for spec in getattr(value, "_http_routes", ()):
                found.append(("method", spec, name))
        return found

    sdk.OWNER = object()
    sdk.SELF = object()
    sdk.AddressError = AddressError
    sdk.actor = sdk.callable_by = sdk.public = sdk.pure = sdk.deferred = _identity
    sdk.bounded_loop = _identity
    sdk.BoundedRange = lambda value: range(value) if isinstance(value, int) else value
    sdk.call = call
    sdk.capture = lambda: types.SimpleNamespace()
    sdk.codec = types.SimpleNamespace(
        encode=lambda value: value,
        decode=lambda value: value,
    )
    sdk.runner = _Runner()
    sdk.runtime = runtime
    sdk.send = lambda *_args, **_kwargs: None
    sdk.to_address_bytes = _to_address_bytes
    routes.VERBS = VERBS
    routes.collect_routes = collect_routes
    routes.route = route
    routes.get = get
    routes.post = post
    routes.put = put
    routes.patch = patch
    routes.delete = delete
    sdk.routes = routes
    sys.modules["cowboy_sdk"] = sdk
    sys.modules["cowboy_sdk.routes"] = routes
