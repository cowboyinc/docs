"""route-intent.json lowers to the Gateway's CIP-15 Routes table with Runner targets."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import install_routes
from offchain.host import _cbor_decode

HERE = Path(__file__).resolve().parent
INTENT = json.loads((HERE.parent / "route-intent.json").read_text())
RUNNER = "0x" + "ab" * 20


def test_every_intended_route_becomes_a_pinned_runner_entry():
    table = install_routes.lower(INTENT, runner="0x" + ("ab" * 20).upper(), timeout_ms=120_000)
    assert table["version"] == 1 and len(table["routes"]) == len(INTENT["routes"])
    by_path = {(r["verb"], r["path"]): r for r in table["routes"]}
    delete = by_path[("DELETE", "/manage/api/keys/{id}")]
    assert delete["target"] == {"kind": "runner", "handler": "offchain.manage.Management.handle", "auth": "account",
                                "affinity": "none", "runner": RUNNER, "timeout_ms": 120_000}
    assert by_path[("POST", "/v1/chat/completions")]["target"]["handler"] == "offchain.inference.Service"
    assert by_path[("GET", "/v1/models")]["target"]["auth"] == "public"
    assert all(r["pays"] == "actor" and r["enabled"] and r["priority"] == 0 for r in table["routes"])


def test_blob_is_canonical_cbor_the_gateway_shape_decodes():
    table = install_routes.lower(INTENT, runner=RUNNER, timeout_ms=30_000)
    raw = install_routes.encode(table)
    decoded = _cbor_decode(raw)
    assert decoded == table
    assert raw == install_routes.encode(json.loads(json.dumps(table))), "encoding is deterministic"
    assert len(raw) < 8 * 1024


@pytest.mark.parametrize("bad", [dict(runner="ab" * 20), dict(runner=RUNNER, timeout_ms=0), dict(runner=RUNNER, timeout_ms=300_001)])
def test_bad_pin_or_timeout_is_refused(bad):
    kwargs = {"runner": RUNNER, "timeout_ms": 1000, **bad}
    with pytest.raises(ValueError):
        install_routes.lower(INTENT, **kwargs)


def test_path_parameters_and_duplicates():
    assert install_routes.gateway_path("/manage/api/keys/:id") == "/manage/api/keys/{id}"
    for bad in ("manage", "/a//b", "/a/{b}", "/a/:", "/a/b:c"):
        with pytest.raises(ValueError):
            install_routes.gateway_path(bad)
    dup = dict(INTENT, routes=INTENT["routes"][:1] * 2)
    with pytest.raises(ValueError):
        install_routes.lower(dup, runner=RUNNER, timeout_ms=1000)


def test_cli_writes_the_blob(tmp_path):
    out = tmp_path / "routes.cbor"
    proc = subprocess.run([sys.executable, str(HERE.parent / "install_routes.py"), "--runner", RUNNER, "--out", str(out)],
                          capture_output=True, text=True, check=True)
    summary = json.loads(proc.stdout)
    assert summary["routes"] == len(INTENT["routes"]) and out.stat().st_size == summary["bytes"]
    assert _cbor_decode(out.read_bytes())["routes"][0]["target"]["kind"] == "runner"


def test_workload_toml_emits_one_gateway_entry_per_route(tmp_path):
    import tomllib
    wl = "ab" * 32
    text = install_routes.workload_toml(INTENT, actor="0x" + "1a" * 20, workload_id=wl.upper(), idle_timeout_ms=120_000)
    entries = tomllib.loads(text)["workload_routes"]
    assert len(entries) == len(INTENT["routes"])
    by_key = {(e["verb"], e["path"]): e for e in entries}
    keys = by_key[("DELETE", "/manage/api/keys/{id}")]
    assert keys["actor"] == "1a" * 20 and keys["pays"] == "actor" and keys["enabled"] is True
    assert keys["target"] == {"kind": "workload", "workload": wl, "strip_prefix": False, "auth": "account", "idle_timeout_ms": 120_000}
    assert by_key[("GET", "/v1/models")]["target"]["auth"] == "public"
    assert by_key[("POST", "/v1/chat/completions")]["target"]["auth"] == "public"
    with pytest.raises(ValueError):
        install_routes.workload_toml(INTENT, actor="0x" + "1a" * 20, workload_id="abc", idle_timeout_ms=1)
    out = tmp_path / "wl.toml"
    assert install_routes.main(["--gateway-toml", "--actor", "0x" + "1a" * 20, "--workload-id", wl, "--out", str(out)]) == 0
    assert tomllib.loads(out.read_text())["workload_routes"] == entries
