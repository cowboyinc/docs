import json

from cowboy_sdk import OWNER, Address, actor, callable_by, codec, public, pure, runtime


@actor
class AuthRoles:
    """Owner-gated admin handlers over a delegated writer allowlist."""

    def _owner_hex(self):
        """The runtime-tracked owner as lowercase hex, or "" before init."""
        owner = runtime.get_owner()
        return Address(owner).to_hex(checksum=False) if owner else ""

    @public(decode="raw")
    def init(self, payload):
        # Ownership lives in a reserved slot @callable_by(OWNER) resolves against,
        # not in self.storage. Claiming it from init is safe because deploy invokes
        # init in the deploy transaction, closing the first-caller-wins window.
        if runtime.is_initialized():
            return codec.encode({"status": "already_initialized",
                                 "owner": self._owner_hex()})
        runtime.assume_ownership_if_unowned()
        requested = (json.loads(payload) if payload else {}).get("owner")
        if requested and Address(requested).to_bytes() != runtime.get_owner():
            runtime.transfer_ownership(Address(requested).to_bytes())
        owner = self._owner_hex()

        self.storage["__name__"] = "Auth Roles"
        self.storage["__description__"] = "Role-based access control for actor handlers"
        self.storage["__version__"] = "1.0.0"
        self.storage["roles"] = {"writer": []}
        self.storage["entries"] = []
        runtime.emit_event("auth_roles.init", {"owner": owner})
        return codec.encode({"status": "initialized", "owner": owner})

    @callable_by(OWNER, decode="cbor")
    def grant_role(self, payload):
        obj = payload or {}
        role = str(obj.get("role", "")).strip()
        raw_address = str(obj.get("address", "")).strip()
        if not role or not raw_address:
            return {"error": "INVALID_ROLE_REQUEST"}
        address = Address(raw_address).to_hex(checksum=False)
        roles = self.storage.get("roles", {})
        members = roles.get(role, [])
        if address not in members:
            members = sorted(members + [address])
        roles[role] = members
        self.storage["roles"] = roles
        runtime.emit_event("auth_roles.grant_role", {"role": role, "address": address})
        return {"status": "granted", "role": role, "address": address}

    @callable_by(OWNER, decode="cbor")
    def revoke_role(self, payload):
        obj = payload or {}
        role = str(obj.get("role", "")).strip()
        raw_address = str(obj.get("address", "")).strip()
        if not role or not raw_address:
            return {"error": "INVALID_ROLE_REQUEST"}
        address = Address(raw_address).to_hex(checksum=False)
        roles = self.storage.get("roles", {})
        roles[role] = [member for member in roles.get(role, []) if member != address]
        self.storage["roles"] = roles
        runtime.emit_event("auth_roles.revoke_role", {"role": role, "address": address})
        return {"status": "revoked", "role": role, "address": address}

    @public(decode="cbor")
    def write_entry(self, payload):
        obj = payload or {}
        # The writer allowlist is ordinary actor state, a layer @callable_by
        # cannot express — hence @public plus a membership check here.
        sender = self.sender.to_hex(checksum=False)
        writers = self.storage.get("roles", {}).get("writer", [])
        if sender != self._owner_hex() and sender not in writers:
            return {"error": "UNAUTHORIZED"}
        content = str(obj.get("content", "")).strip()
        if not content:
            return {"error": "INVALID_CONTENT"}
        entries = self.storage.get("entries", [])
        entry = {"entry_id": len(entries) + 1, "author": sender, "content": content}
        entries.append(entry)
        self.storage["entries"] = entries
        runtime.emit_event("auth_roles.write_entry", entry)
        return {"status": "written", "entry": entry}

    @pure
    @public(decode="cbor")
    def get_entries(self, payload):
        return {"entries": self.storage.get("entries", [])}

    @pure
    @public(decode="cbor")
    def get_roles(self, payload):
        return {"owner": self._owner_hex(), "roles": self.storage.get("roles", {})}


def _get_actor():
    return AuthRoles()


def init(payload):
    return _get_actor().init(payload)


def grant_role(payload):
    return _get_actor().grant_role(payload)


def revoke_role(payload):
    return _get_actor().revoke_role(payload)


def write_entry(payload):
    return _get_actor().write_entry(payload)


def get_entries(payload):
    return _get_actor().get_entries(payload)


def get_roles(payload):
    return _get_actor().get_roles(payload)
