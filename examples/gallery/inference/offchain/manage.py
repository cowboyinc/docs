"""Management handler core. Runtime adapters are required; see MANAGEMENT.md.

Request metadata is supplied by the admitted executor, never by HTTP JSON.
The proof verifier must authenticate the exact raw target, body and context.
Concrete verifier, nonce and CBFS adapters are composed in management_runtime;
the executor supplies authoritative chain readers and deployment configuration.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from .keys import KeyRecord
from .volume import ConflictError, Volume


class Rejected(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


@dataclass(frozen=True)
class Request:
    method: str
    raw_target: str
    body: bytes
    actor: bytes
    chain_id: int
    network: str
    handler: str
    proof: object
    hire: int | None = None
    executor_claim: object | None = None


@dataclass(frozen=True)
class Account:
    address: bytes
    expiry: int
    nonce: bytes
    digest: bytes


@dataclass(frozen=True)
class Hire:
    hirer: bytes
    pins: tuple[str, str, str]
    status: str


@dataclass(frozen=True)
class Slot:
    hire_id: int
    pins: tuple[str, str, str]
    public_base_url: str
    active_key_cap: int = 8


def response(status: int, value: dict) -> dict:
    return {"status": status, "headers": {
        "content-type": "application/json", "cache-control": "no-store",
        "x-content-type-options": "nosniff",
    }, "body": json.dumps(value, separators=(",", ":")).encode()}


def public_key(record: KeyRecord) -> dict:
    return {"key_id": record.key_id, "name": record.name,
            "prefix": record.prefix, "created_at": record.created_at,
            "revoked_at": record.revoked_at}


def stable_read(volume: Volume, read: Callable, retries: int = 4):
    for _ in range(retries):
        root = volume.root()
        result = read(volume)
        if volume.root() == root:
            return result
    raise ConflictError("read snapshot changed")


def key_records(volume: Volume) -> list[KeyRecord]:
    paths = volume.list("keys/")
    if len(paths) > 10000:
        raise Rejected(503, "key_scan_limit")
    result = []
    for path in paths:
        if not re.fullmatch(r"keys/[0-9a-f]{32}\.json", path):
            raise Rejected(503, "invalid_key_record")
        raw = volume.read(path)
        if raw is None or len(raw) > 4096:
            raise Rejected(503, "invalid_key_record")
        record = KeyRecord.from_json(raw)
        if record.path() != path:
            raise Rejected(503, "invalid_key_record")
        result.append(record)
    return result


class Management:
    def __init__(self, *, actor: bytes, chain_id: int, network: str,
                 slots: dict[str, Slot], hire_lookup: Callable[[int], Hire | None],
                 volume_for: Callable[[Slot], Volume],
                 verify_account: Callable[[Request], Account] | None = None,
                 consume_nonce: Callable[[tuple, int, bytes], bool] | None = None,
                 verify_replay: Callable[[Request, Account], bool] | None = None,
                 issue_key: Callable[[str, str, int], tuple[str, KeyRecord]] | None = None,
                 grants=None, handler: str = "offchain.manage.Management.handle",
                 clock: Callable[[], int] = lambda: int(time.time())):
        self.actor, self.chain_id, self.network = actor, chain_id, network
        self.slots, self.hire_lookup, self.volume_for = slots, hire_lookup, volume_for
        self.verify_account, self.consume_nonce = verify_account, consume_nonce
        self.verify_replay = verify_replay
        self.issue_key, self.clock = issue_key, clock
        self.grants, self.handler = grants, handler

    def handle(self, req: Request) -> dict:
        try:
            return self._handle(req)
        except Rejected as error:
            return response(error.status, {"error": error.code})
        except ConflictError:
            return response(503, {"error": "volume_busy"})
        except (ValueError, TypeError, KeyError, UnicodeError):
            # Stored malformed data must not become partial successful output.
            return response(503, {"error": "invalid_runtime_data"})
        except Exception:
            # Adapter failure must not expose credentials, request bodies or traces.
            return response(503, {"error": "runtime_unavailable"})

    def _handle(self, req: Request) -> dict:
        if (req.actor, req.chain_id, req.network, req.hire) != (
                self.actor, self.chain_id, self.network, None):
            raise Rejected(403, "access_denied")
        if len(req.raw_target) > 4096 or len(req.body) > 4096:
            raise Rejected(413, "request_too_large")
        # Keep raw_target intact for signature verification; parsing is routing only.
        if (not req.raw_target.startswith("/") or req.raw_target.startswith("//")
                or not req.raw_target.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in req.raw_target)
                or re.search(r"%(?![0-9A-Fa-f]{2})", req.raw_target)):
            raise Rejected(400, "invalid_target")
        target = urlsplit(req.raw_target)
        if target.fragment or target.scheme or target.netloc:
            raise Rejected(400, "invalid_target")
        if req.method == "GET" and target.path == "/manage/config" and not target.query:
            return response(200, {"actor": self.actor.hex(), "chain_id": str(self.chain_id),
                                  "network": self.network, "handler": self.handler})
        assets = {"/manage": ("index.html", "text/html; charset=utf-8"),
                  "/manage/": ("index.html", "text/html; charset=utf-8"),
                  "/manage/assets/manage.css": ("manage.css", "text/css; charset=utf-8"),
                  "/manage/assets/manage.js": ("manage.js", "text/javascript; charset=utf-8"),
                  "/manage/assets/wallet.js": ("wallet.js", "text/javascript; charset=utf-8")}
        for name in ("sha3.js", "_u64.js", "utils.js", "crypto.js"):
            assets["/manage/assets/vendor/" + name] = ("vendor/" + name, "text/javascript; charset=utf-8")
        if req.method == "GET" and target.path in assets:
            name, content_type = assets[target.path]
            return {"status": 200, "headers": {
                "content-type": content_type, "cache-control": "no-store",
                "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                "referrer-policy": "no-referrer", "x-content-type-options": "nosniff",
            }, "body": (Path(__file__).parent / "page" / name).read_bytes()}
        try:
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise Rejected(400, "invalid_query") from None
        query = dict(pairs)
        if len(query) != len(pairs):
            raise Rejected(400, "duplicate_parameter")
        if self.verify_account is None or (self.consume_nonce is None and self.verify_replay is None):
            raise Rejected(503, "account_verifier_unavailable")
        try:
            account = self.verify_account(req)
        except Exception:
            raise Rejected(403, "access_denied") from None
        now = self.clock()
        if (not isinstance(account, Account) or len(account.address) != 20
                or not 1 <= len(account.nonce) <= 256
                or len(account.digest) != 32
                or not now <= account.expiry <= now + 3600):
            raise Rejected(403, "access_denied")
        slot_name = query.get("slot", "")
        slot = self.slots.get(slot_name)
        hire = self.hire_lookup(slot.hire_id) if slot else None
        if hire is None or hire.hirer != account.address or hire.pins != slot.pins:
            raise Rejected(403, "access_denied")
        nonce_key = (self.chain_id, self.network, self.actor, account.address, account.nonce)
        replay_ok = (self.verify_replay(req, account) if self.verify_replay is not None
                     else self.consume_nonce(nonce_key, account.expiry, account.digest))
        if replay_ok is not True:
            raise Rejected(403, "access_denied")

        path, method = target.path, req.method
        allowed = {"slot", "from", "to", "key_id", "model"} if path == "/manage/api/usage" else {"slot"}
        if set(query) - allowed:
            raise Rejected(400, "unknown_parameter")
        volume = self.volume_for(slot)
        if path == "/manage/api/keys" and method == "GET":
            records = stable_read(volume, key_records)
            return response(200, {"keys": [public_key(r) for r in records], "hire_status": hire.status})
        if path == "/manage/api/keys" and method == "POST":
            if hire.status != "active":
                raise Rejected(409, "hire_not_active")
            if self.issue_key is None:
                raise Rejected(503, "key_issuer_unavailable")
            try:
                data = json.loads(req.body)
                name = data["name"]
                valid = set(data) == {"name"} and isinstance(name, str) and 1 <= len(name.strip()) <= 80
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                raise Rejected(400, "invalid_key_name")
            token, record = self.issue_key(slot_name, name.strip(), now)

            def create(current):
                retained = self.hire_lookup(slot.hire_id)
                if retained != hire:
                    raise Rejected(409, "hire_changed")
                records = key_records(current)
                if sum(r.revoked_at is None for r in records) >= slot.active_key_cap:
                    raise Rejected(409, "active_key_limit")
                if current.read(record.path()) is not None:
                    raise Rejected(503, "key_id_collision")
                return {record.path(): record.to_json()}

            volume.mutate(create)
            return response(201, {"key": public_key(record), "secret": token})
        match = re.fullmatch(r"/manage/api/keys/([0-9a-f]{32})", path)
        if match and method == "DELETE":
            result = []

            def revoke(current):
                raw = current.read(f"keys/{match[1]}.json")
                if raw is None:
                    raise Rejected(404, "key_not_found")
                record = KeyRecord.from_json(raw)
                if record.key_id != match[1]:
                    raise Rejected(503, "invalid_key_record")
                updated = replace(record, revoked_at=record.revoked_at if record.revoked_at is not None else now)
                result[:] = [updated]
                return {updated.path(): updated.to_json()}

            volume.mutate(revoke)
            return response(200, {"key": public_key(result[0])})
        if path == "/manage/api/catalog" and method == "GET":
            def catalog(current):
                raw = current.read("catalog.json")
                if raw is None or len(raw) > 65536:
                    raise Rejected(503, "catalog_unavailable")
                data = json.loads(raw)
                # Serving URLs, secret references and delegation never leave the handler.
                models = [{"id": model, "context_tokens": item["context_tokens"],
                           "max_output_tokens": item["max_output_tokens"]}
                          for model, item in sorted(data["models"].items())]
                return {"models": models, "public_base_url": slot.public_base_url,
                        "hire_status": hire.status, "direct_access_available": self.grants is not None,
                        "limits": {key: data["limits"][key] for key in ("max_requests", "max_concurrency")}}
            return response(200, stable_read(volume, catalog))
        if path == "/manage/api/usage" and method == "GET":
            return response(200, stable_read(volume, lambda current: usage(current, query, slot_name, slot.hire_id, now)))
        if path == "/manage/api/read-grant" and method in ("PUT", "DELETE"):
            if self.grants is None:
                raise Rejected(503, "grant_adapter_unavailable")
            # Read grants belong to the authenticated retained hirer, never to
            # a caller-selected address. Paused hires retain read access.
            if self.hire_lookup(slot.hire_id) != hire:
                raise Rejected(409, "hire_changed")
            if method == "DELETE":
                if req.body not in (b"", b"{}"):
                    raise Rejected(400, "invalid_grant")
                self.grants.remove(slot, account.address)
                return response(200, {"removed": True})
            try:
                data = json.loads(req.body)
                if set(data) != {"signing_key", "encryption_key"}:
                    raise ValueError()
                for value in data.values():
                    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                        raise ValueError()
                signing_key, encryption_key = bytes.fromhex(data["signing_key"]), bytes.fromhex(data["encryption_key"])
                if signing_key == bytes(32) or encryption_key == bytes(32):
                    raise ValueError()
            except (ValueError, TypeError):
                raise Rejected(400, "invalid_grant") from None
            result = self.grants.put(slot, account.address, signing_key, encryption_key)
            # Only public connection coordinates leave this adapter.
            owner, name = result["owner"], result["volume"]
            if (not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{40}", owner)
                    or not isinstance(name, str) or not 1 <= len(name) <= 256):
                raise ValueError("invalid grant result")
            return response(200, {"owner": owner, "volume": name, "access": "read_only"})
        raise Rejected(404, "not_found")


def usage(volume: Volume, query: dict, slot: str, hire_id: int, now: int) -> dict:
    """Bounded v1 scan. Refuse an oversized ledger instead of truncating totals.

    Pricing is integer USD nano-units per token. Missing rates are unavailable.
    Existing deployments must supply this versioned schema explicitly.
    """
    today = datetime.fromtimestamp(now, timezone.utc).date()
    try:
        start = datetime.strptime(query.get("from", str(today - timedelta(days=6))), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(query.get("to", str(today)), "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        if not 0 < (end - start).days <= 31:
            raise ValueError()
    except ValueError:
        raise Rejected(400, "invalid_date_range") from None
    paths = volume.list("usage/")
    if len(paths) > 4096:
        raise Rejected(503, "usage_scan_limit")
    latest, total_bytes, last_record, last_committed = {}, 0, None, None
    segments = []
    for path in paths:
        match = re.fullmatch(r"usage/([A-Za-z0-9_-]{1,128})/([0-9]{1,20})\.jsonl", path)
        if not match:
            raise Rejected(503, "invalid_usage_segment")
        segments.append((match[1], int(match[2]), path))
    for session_id, segment, path in sorted(segments):
        raw = volume.read(path)
        if raw is None:
            raise Rejected(503, "usage_unavailable")
        total_bytes += len(raw)
        if total_bytes > 16 * 1024 * 1024:
            raise Rejected(503, "usage_scan_limit")
        for line in raw.splitlines():
            record = json.loads(line)
            if record.get("kind") == "segment":
                stamp = record["committed_at"]
                if (type(stamp) is not int or stamp < 0
                        or record["session_id"] != session_id or record["segment"] != segment):
                    raise Rejected(503, "invalid_usage_segment")
                last_committed = max(stamp, last_committed or stamp)
                continue
            if (record["slot"] != slot or record["hire_id"] != hire_id
                    or record["session_id"] != session_id):
                raise Rejected(503, "invalid_usage_scope")
            identity = (record["session_id"], record["request_id"])
            latest[identity] = record
            if len(latest) > 50000:
                raise Rejected(503, "usage_scan_limit")
            # This is event time, not commit time. Do not pretend otherwise.
            stamp = record.get("finished_ts") or record["ts"]
            last_record = max(stamp, last_record or stamp)
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
              "unknown_usage_requests": 0, "known_usage_requests": 0,
              "usage_cost_nano_usd": 0, "unpriced_requests": 0}
    groups, prices = {}, {}
    for record in latest.values():
        if not start.timestamp() <= record["ts"] < end.timestamp():
            continue
        if any(query.get(field) is not None and query[field] != record[field] for field in ("key_id", "model")):
            continue
        group = groups.setdefault((record["key_id"], record["model"]), {k: 0 for k in totals})
        delta = {k: 0 for k in totals}
        delta["requests"] = 1
        recorded_usage = record["usage"]
        if not (set(recorded_usage) == {"known"} or recorded_usage == {"pending": True}
                or recorded_usage == {"unknown": True}):
            raise Rejected(503, "invalid_usage_record")
        known = recorded_usage.get("known")
        if known is None:
            delta["unknown_usage_requests"] = 1
            delta["unpriced_requests"] = 1
        else:
            if set(known) != {"input", "output"} or any(type(n) is not int or not 0 <= n < 1 << 64 for n in known.values()):
                raise Rejected(503, "invalid_usage_record")
            delta.update(input_tokens=known["input"], output_tokens=known["output"], known_usage_requests=1)
            version = record.get("price_version")
            if version is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", version):
                raise Rejected(503, "invalid_price_version")
            rates = None
            if version is not None and version not in prices:
                if len(prices) >= 128:
                    raise Rejected(503, "price_scan_limit")
                raw = volume.read(f"pricing/{version}.json")
                table = None
                if raw is not None:
                    if len(raw) > 65536:
                        raise Rejected(503, "invalid_price_table")
                    table = json.loads(raw)
                    if table.get("schema") != 1 or table.get("unit") != "nano_usd_per_token":
                        raise Rejected(503, "invalid_price_table")
                prices[version] = table
            table = prices.get(version)
            if table is not None:
                rates = table["models"].get(record["model"])
            if rates is None:
                delta["unpriced_requests"] = 1
            else:
                if set(rates) != {"input", "output"} or any(type(n) is not int or not 0 <= n < 1 << 64 for n in rates.values()):
                    raise Rejected(503, "invalid_price_table")
                delta["usage_cost_nano_usd"] = sum(known[k] * rates[k] for k in rates)
        for key, value in delta.items():
            totals[key] += value
            group[key] += value

    def presented(values):
        return dict(values, input_tokens=str(values["input_tokens"]) if values["known_usage_requests"] else None,
                    output_tokens=str(values["output_tokens"]) if values["known_usage_requests"] else None,
                    usage_cost_nano_usd=str(values["usage_cost_nano_usd"]) if values["requests"] > values["unpriced_requests"] else None,
                    partial=values["unknown_usage_requests"] > 0,
                    cost_partial=values["unpriced_requests"] > 0)

    return {"totals": presented(totals), "breakdown": [dict(key_id=key, model=model, **presented(values)) for (key, model), values in sorted(groups.items())],
            "from": str(start.date()), "to": str((end - timedelta(days=1)).date()), "timezone": "UTC",
            "latest_record_at": last_record, "last_committed_at": last_committed}
