"""Exercise the actual SDK builder; fixtures here are not deployment inputs."""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

if os.environ.get("COWBOY_SDK_PATH"):
    sys.path.append(os.environ["COWBOY_SDK_PATH"])
rr = pytest.importorskip("cowboy_sdk.runner_routes", reason="Set COWBOY_SDK_PATH to the SDK route-builder checkout")
from prepare_routes import prepare, report


def inputs():
    intent = json.loads((Path(__file__).parents[1] / "route-intent.json").read_text())
    sponsorship = dict(window_blocks=10, admissions_per_window=10, max_active_jobs=5, max_active_sessions=2,
                       max_reserved_wei=1000, max_admitted_wei_per_window=1000)
    deployment = dict(bundle_hash="11" * 32, runtime_hash="22" * 32, ingress_verify_key="33" * 32,
                      actor_sponsorship=sponsorship, replay_authority="management", routes={})
    for row in intent["routes"]:
        deployment["routes"][row["verb"] + " " + row["path"]] = dict(enabled=True,
            limits=dict(request_bytes=4096, response_bytes=65536, first_byte_ms=1000, idle_ms=1000, wall_ms=10000,
                        max_tokens=100, memory_bytes=1048576, max_concurrency=1, max_job_budget=10),
            session_limits=dict(lifetime_blocks=10, idle_ms=1000, max_requests=10, max_concurrency=1,
                                max_tokens=100, max_budget=100, reorder_window=8, drain_ms=1000),
            sponsorship=dict(sponsorship), pricing=dict(allow_zero=False, price_advert_digest="44" * 32), mounts=[],
            secret_refs={label: {"purpose": "metering" if label == "metering_sign" else "read",
                                 "ref": rr.secret_ref(b"a" * 20, label).hex()} for label in row["secrets"]},
            forward_headers=["authorization"] if "bearer" in row else [],
            metering_key=("02" + "55" * 32) if "session_open" in row["kinds"] else None)
    return intent, deployment


def test_every_route_lowers_through_shared_sdk_with_exact_projection():
    intent, deployment = inputs()
    result = prepare(intent, deployment)
    assert len(result["routes"]) == len(intent["routes"])
    for item, row in zip(result["routes"], intent["routes"]):
        declaration = rr.validate_table_entry(item["entry"])
        assert declaration.route_id().hex() == item["route_id"]
        assert declaration.verb == row["verb"] and declaration.path == row["path"]
        assert declaration.handler == row["implementation"]
        assert declaration.auth == (1 if row["auth"] == "account" else 0)
        assert declaration.kinds == ([1, 2] if row["path"] == "/v1/chat/completions" else [0])
        assert item["entry"]["target"]["kind"] == "runner_route"
    assert rr.decode_sponsorship(result["actor_sponsorship"]) == deployment["actor_sponsorship"]
    serialized = json.dumps(report(result))
    assert '"hex"' in serialized and "not installed" in serialized


def test_executor_mode_removes_only_management_nonce_authority():
    intent, deployment = inputs()
    nonce = rr.secret_ref(b"a" * 20, "management_nonce_volume_write")
    local = prepare(intent, deployment)
    assert any(nonce in rr.validate_table_entry(r["entry"]).read_keys for r in local["routes"])
    deployment["replay_authority"] = "executor"
    # Explicitly changing deployment authority must accompany the mode switch.
    with pytest.raises(ValueError, match="secret bindings"):
        prepare(intent, deployment)
    for bindings in deployment["routes"].values():
        bindings["secret_refs"].pop("management_nonce_volume_write", None)
    native = prepare(intent, deployment)
    assert all(nonce not in rr.validate_table_entry(r["entry"]).read_keys for r in native["routes"])


@pytest.mark.parametrize("change", ["missing_route", "extra_route", "bundle", "budget", "auth_header", "extra_authority", "session_pair"])
def test_missing_or_invalid_deployment_input_is_refused(change):
    intent, deployment = inputs()
    route = deployment["routes"]["POST /v1/chat/completions"]
    if change == "missing_route": del deployment["routes"]["GET /manage"]
    if change == "extra_route": deployment["routes"]["POST /admin"] = copy.deepcopy(route)
    if change == "bundle": deployment["bundle_hash"] = "00" * 32
    if change == "budget": route["limits"]["max_job_budget"] = 0
    if change == "auth_header": route["forward_headers"] = []
    if change == "extra_authority": route["secret_refs"]["other"] = dict(purpose="read", ref=rr.secret_ref(b"a" * 20, "other").hex())
    if change == "session_pair": intent["routes"][-1]["kinds"] = ["session_open"]
    with pytest.raises(ValueError):
        prepare(intent, deployment)
