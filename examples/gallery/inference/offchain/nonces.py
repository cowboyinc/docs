"""Durable management replay protection over one shared actor-wide CAS volume.

All management workers must address the same volume. Never put this store in
a per-job cache or a hirer-readable volume. Expired records can be pruned by
maintenance only after their recorded expiry plus 300 seconds; failed requests still consume
their nonce. The backend, not this class, provides durability and atomicity.
"""

import hashlib
import json
import secrets
import time

from . import cbor
from .volume import Volume, encode_json


class NonceStore:
    def __init__(self, volume: Volume, *, clock=lambda: int(time.time())):
        self.volume, self.clock = volume, clock

    def __call__(self, key: tuple, expiry: int, digest: bytes) -> bool:
        chain, network, actor, account, nonce = key
        if (type(chain) is not int or not 0 < chain < 2**64
                or not isinstance(network, str) or not network
                or not isinstance(actor, bytes) or len(actor) != 20
                or not isinstance(account, bytes) or len(account) != 20
                or not isinstance(nonce, bytes) or not 1 <= len(nonce) <= 256
                or type(expiry) is not int or not isinstance(digest, bytes) or len(digest) != 32):
            raise ValueError("invalid nonce identity")
        identity = cbor.encode([chain, network, actor, account, nonce])
        path = "nonces/" + hashlib.sha256(identity).hexdigest() + ".json"
        # Distinguish an internal CAS retry after a lost acknowledgement from
        # a new execution of the same signed request. The digest cannot do this:
        # every byte-identical replay has the same digest.
        attempt_id = secrets.token_hex(32)

        class Replay(Exception):
            pass

        class AlreadyConsumedHere(Exception):
            pass

        def consume(current):
            now = self.clock()
            if not now <= expiry <= now + 3600:
                raise Replay()
            raw = current.read(path)
            if raw is not None:
                if len(raw) > 256:
                    raise ValueError("invalid nonce record")
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("invalid nonce record")
                legacy = record.get("schema") == 1
                fields = {"schema", "expiry", "digest"} | (set() if legacy else {"attempt_id"})
                if (set(record) != fields or type(record["schema"]) is not int
                        or record["schema"] not in (1, 2)
                        or type(record["expiry"]) is not int
                        or not isinstance(record["digest"], str) or len(record["digest"]) != 64
                        or (not legacy and (not isinstance(record["attempt_id"], str)
                                            or len(record["attempt_id"]) != 64))):
                    raise ValueError("invalid nonce record")
                if record["expiry"] + 300 >= now:
                    if (not legacy and record["attempt_id"] == attempt_id
                            and record["digest"] == digest.hex() and record["expiry"] == expiry):
                        raise AlreadyConsumedHere()
                    raise Replay()
            return {path: encode_json({"schema": 2, "expiry": expiry, "digest": digest.hex(),
                                       "attempt_id": attempt_id})}

        try:
            self.volume.mutate(consume)
        except Replay:
            return False
        except AlreadyConsumedHere:
            return True
        return True
