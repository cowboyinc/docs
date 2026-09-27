"""Offline inference route preparation using the shared Cowboy SDK encoder.

No chain client, deployment action, default budgets or fabricated bundle pins.
The JSON result is a review artifact; byte strings are explicitly tagged hex.
"""

import argparse
import json
import re
import sys
from pathlib import Path


def hex_bytes(value, size=None):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", value):
        raise ValueError("expected lowercase nonempty hex")
    raw = bytes.fromhex(value)
    if size is not None and len(raw) != size:
        raise ValueError(f"expected {size} bytes")
    return raw


def prepare(intent, deployment):
    from cowboy_sdk.runner_routes import RouteDeclaration, encode_sponsorship, validate_table_entry

    if (intent["format"] != "inference-route-intent-v1" or intent["target_kind"] != "runner_route"
            or intent["funding"] != "actor"):
        raise ValueError("unsupported inference route intent")
    if set(deployment) != {"bundle_hash", "runtime_hash", "ingress_verify_key", "actor_sponsorship", "replay_authority", "routes"}:
        raise ValueError("deployment fields must be explicit and exact")
    replay = deployment["replay_authority"]
    if replay not in ("management", "executor"):
        raise ValueError("unknown replay authority")
    bundle = hex_bytes(deployment["bundle_hash"], 32)
    runtime = hex_bytes(deployment["runtime_hash"], 32)
    ingress = hex_bytes(deployment["ingress_verify_key"], 32)
    if ingress == bytes(32):
        raise ValueError("ingress verification key cannot be zero")
    ids = [row["verb"] + " " + row["path"] for row in intent["routes"]]
    if len(set(ids)) != len(ids) or set(ids) != set(deployment["routes"]):
        raise ValueError("deployment must bind each intended route exactly once")
    prepared = []
    for identity, row in zip(ids, intent["routes"]):
        supplied = deployment["routes"][identity]
        if set(supplied) != {"enabled", "limits", "session_limits", "sponsorship", "pricing", "mounts", "secret_refs", "forward_headers", "metering_key"}:
            raise ValueError(f"{identity}: missing or extra route binding fields")
        expected = set(row["secrets"])
        if replay == "executor":
            expected.discard("management_nonce_volume_write")
        if set(supplied["secret_refs"]) != expected:
            raise ValueError(f"{identity}: secret bindings must exactly match intended authority")
        refs = {"read": [], "verify": [], "sign": []}
        metering = None
        for label, binding in supplied["secret_refs"].items():
            if set(binding) != {"purpose", "ref"}:
                raise ValueError(f"{identity}: invalid secret binding")
            ref = hex_bytes(binding["ref"], 52)
            purpose = binding["purpose"]
            if label == "metering_sign":
                if purpose != "metering" or supplied["metering_key"] is None:
                    raise ValueError("session metering authority must have its own key")
                metering = {"secret_ref": ref, "public_key": hex_bytes(supplied["metering_key"], 33)}
            elif purpose in refs:
                refs[purpose].append(ref)
            else:
                raise ValueError(f"{identity}: unsupported secret purpose")
        if metering is None and supplied["metering_key"] is not None:
            raise ValueError("undeclared metering key")
        headers = supplied["forward_headers"]
        if "bearer" in row and "authorization" not in headers:
            raise ValueError("inference handler must receive its bearer key")
        kinds = [{"per_call": 0, "session_open": 1, "session_close": 2}[kind] for kind in row["kinds"]]
        declaration = RouteDeclaration(verb=row["verb"], path=row["path"], handler=row["implementation"],
            bundle_hash=bundle, runtime_hash=runtime, kinds=kinds, auth={"public": 0, "account": 1}[row["auth"]],
            enabled=supplied["enabled"], limits=supplied["limits"], session_limits=supplied["session_limits"],
            sponsorship=supplied["sponsorship"], pricing={**supplied["pricing"], "price_advert_digest": hex_bytes(supplied["pricing"]["price_advert_digest"], 32)},
            ingress_verify_key=ingress if "bearer" in row else None, metering_key=metering,
            mounts=sorted(hex_bytes(mount) for mount in supplied["mounts"]),
            read_keys=sorted(refs["read"]), verify_keys=sorted(refs["verify"]), sign_keys=sorted(refs["sign"]),
            forward_headers=sorted(headers))
        hired = row["hire"] == "bound"
        declaration.validate(hired=hired)
        entry = declaration.table_entry(hired=hired)
        validate_table_entry(entry)
        prepared.append({"route": identity, "hire": row["hire"],
                         "route_id": declaration.route_id(hired=hired).hex(), "entry": entry})
    return {"format": "inference-prepared-routes-v1", "replay_authority": replay,
            "scope": "Offline validation only; not installed. Live persistence fit and admission remain unchecked.",
            "actor_sponsorship": encode_sponsorship(deployment["actor_sponsorship"]), "routes": prepared}


def report(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, list):
        return [report(item) for item in value]
    if isinstance(value, dict):
        return {key: report(item) for key, item in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-lib", required=True, help="node/pvm/Lib containing cowboy_sdk.runner_routes")
    parser.add_argument("--deployment", required=True, type=Path, help="explicit JSON pins, budgets and authority bindings")
    parser.add_argument("--output", required=True, type=Path, help="review JSON; does not install routes")
    args = parser.parse_args()
    # Append: prepending the PVM library would shadow CPython's stdlib.
    sys.path.append(args.sdk_lib)
    result = prepare(json.loads(Path(__file__).with_name("route-intent.json").read_text()),
                     json.loads(args.deployment.read_text()))
    args.output.write_text(json.dumps(report(result), indent=2) + "\n")


if __name__ == "__main__":
    main()
