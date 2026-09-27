import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offchain.volume import ConflictError, LocalVolume


def test_cas_conflict_reapplies(tmp_path):
    vol = LocalVolume(tmp_path)
    prev = vol.root()
    vol.commit(prev, {"keys/a.json": b"1"})
    with pytest.raises(ConflictError):
        vol.commit(prev, {"keys/b.json": b"2"})
    # mutate() re-reads and reapplies.
    vol.mutate(lambda v: {"keys/b.json": b"2"})
    assert vol.list("keys/") == ["keys/a.json", "keys/b.json"]


def test_concurrent_key_creates_respect_a_cap(tmp_path):
    vol = LocalVolume(tmp_path)
    cap = 3

    def create(i):
        def apply(v):
            if len(v.list("keys/")) >= cap:
                return {}
            return {f"keys/{i}.json": b"x"}

        vol.mutate(apply)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(vol.list("keys/")) == cap


def test_path_traversal_refused(tmp_path):
    vol = LocalVolume(tmp_path)
    with pytest.raises(ValueError):
        vol.commit(vol.root(), {"../x": b"1"})
