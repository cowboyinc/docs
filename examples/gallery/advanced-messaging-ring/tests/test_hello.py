"""The `hello` target is a minimal counter class with no PVM entrypoints; it
exists as the smallest possible actor-shaped module. Exercise it directly."""

import importlib

hello = importlib.import_module("actors.hello.main")


def test_counter_starts_at_zero() -> None:
    actor = hello.Actor()
    assert actor.get_count() == 0


def test_increment_advances_counter() -> None:
    actor = hello.Actor()
    assert actor.increment() == 1
    assert actor.increment() == 2
    assert actor.get_count() == 2
