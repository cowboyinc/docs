from cowboy_sdk import OWNER, Address, actor, callable_by, public, pure, runtime


MINIMUM_LIQUIDITY = 1000

# CIP-20 quantities are u128 (token/src/types.rs), but the SDK's canonical CBOR
# encoder emits no bignum tags: a storage write of 2**64 raises OverflowError, an
# event payload raises CodecError. It *decodes* tags 2/3, so the asymmetry is
# silent until a write. This pool therefore tops out a u64 below CIP-20.
#
# Letting the encode fail would be safe — an uncaught raise discards the tx frame,
# token writes included — but the caller gets an opaque encode error after paying
# for the work. The preflight turns that into a named error return.
MAX_ENCODABLE_AMOUNT = 2**64 - 1


def _unencodable(*values):
    return any(value > MAX_ENCODABLE_AMOUNT for value in values)


def _is_initialized(storage):
    """True once init_pool has run, for either encoding of the flag.

    The flag is a native bool. A pool deployed before that and upgraded in place
    holds the strings "0" / "1" — and "0" is truthy, so a plain truthiness read
    would report an untouched pool as initialized and leave it that way forever.
    """
    flag = storage.get("initialized", False)
    if isinstance(flag, str):
        return flag == "1"
    return bool(flag)


def _sender_mismatch(data, sender):
    declared = data.get("sender", "")
    if not declared:
        return False
    return Address(declared) != sender


def _isqrt(n):
    if n <= 0:
        return 0
    x = n
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + n // x) // 2
    return x


@actor
class V2Pool:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        self.storage["__name__"] = "V2 Liquidity Pool"
        self.storage["__description__"] = "Constant-product AMM pool (CIP-21)"
        self.storage["__version__"] = "1.0.0"
        self.storage["initialized"] = False
        self.storage["reserve_a"] = 0
        self.storage["reserve_b"] = 0
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def init_pool(self, payload):
        if _is_initialized(self.storage):
            return {"error": "already initialized"}
        data = payload or {}
        token_a = data["token_a"]
        token_b = data["token_b"]
        fee_bps = int(data.get("fee_bps", 30))
        pool_address = data.get("pool_address", "")
        if token_a == token_b:
            return {"error": "token_a and token_b must differ"}
        if fee_bps < 0 or fee_bps >= 10000:
            return {"error": "invalid fee_bps"}

        lp_token = runtime.token_create(b"V2 Pool LP Token", b"V2-LP", 18, 0, None, None, None)
        lp_hex = bytes(lp_token).hex()

        self.storage["initialized"] = True
        self.storage["token_a"] = token_a
        self.storage["token_b"] = token_b
        self.storage["lp_token"] = lp_hex
        self.storage["fee_bps"] = fee_bps
        self.storage["pool_address"] = pool_address

        result = {
            "status": "initialized",
            "token_a": token_a,
            "token_b": token_b,
            "lp_token": lp_hex,
            "fee_bps": fee_bps,
        }
        runtime.emit_event("pool_initialized", result)
        return result

    @public(decode="cbor")
    def add_liquidity(self, payload):
        data = payload or {}
        token_a = self.storage["token_a"]
        token_b = self.storage["token_b"]
        lp_token = self.storage["lp_token"]
        sender = self.sender
        if _sender_mismatch(data, sender):
            return {"error": "sender mismatch"}
        pool_addr = self.storage.get("pool_address", "") or runtime.get_actor_address()
        amount_a = int(data["amount_a"])
        amount_b = int(data["amount_b"])
        if amount_a <= 0 or amount_b <= 0:
            return {"error": "amounts must be positive"}
        reserve_a = int(self.storage.get("reserve_a", 0))
        reserve_b = int(self.storage.get("reserve_b", 0))

        # Reads and arithmetic first, mutations after: the rejections below are
        # ordinary returns, which commit, so nothing may have moved yet. The
        # LP-supply read is unaffected by the token_a/token_b transfers, so it
        # hoists above them safely.
        total_supply = int(runtime.token_total_supply(lp_token))
        if total_supply == 0:
            liquidity = _isqrt(amount_a * amount_b) - MINIMUM_LIQUIDITY
        else:
            liquidity = min((amount_a * total_supply) // reserve_a, (amount_b * total_supply) // reserve_b)

        if liquidity <= 0:
            return {"error": "insufficient liquidity minted"}

        reserve_a += amount_a
        reserve_b += amount_b
        if _unencodable(amount_a, amount_b, liquidity, reserve_a, reserve_b):
            return {"error": "amount exceeds the pool's u64 ceiling"}

        runtime.token_transfer_from(token_a, sender, pool_addr, amount_a)
        runtime.token_transfer_from(token_b, sender, pool_addr, amount_b)
        if total_supply == 0:
            runtime.token_mint(lp_token, pool_addr, MINIMUM_LIQUIDITY)
        runtime.token_mint(lp_token, sender, liquidity)
        self.storage["reserve_a"] = reserve_a
        self.storage["reserve_b"] = reserve_b

        result = {
            "status": "liquidity_added",
            "amount_a": amount_a,
            "amount_b": amount_b,
            "lp_minted": liquidity,
            "reserve_a": reserve_a,
            "reserve_b": reserve_b,
        }
        runtime.emit_event("liquidity_added", result)
        return result

    @public(decode="cbor")
    def swap(self, payload):
        data = payload or {}
        sender = self.sender
        if _sender_mismatch(data, sender):
            return {"error": "sender mismatch"}
        token_in_hex = str(data["token_in"])
        amount_in = int(data["amount_in"])
        min_amount_out = int(data.get("min_amount_out", 0))
        if amount_in <= 0:
            return {"error": "amount_in must be positive"}
        pool_addr = self.storage.get("pool_address", "") or runtime.get_actor_address()

        token_a_hex = self.storage["token_a"]
        token_b_hex = self.storage["token_b"]
        reserve_a = int(self.storage.get("reserve_a", 0))
        reserve_b = int(self.storage.get("reserve_b", 0))
        fee_bps = int(self.storage.get("fee_bps", 30))

        if token_in_hex == token_a_hex:
            reserve_in, reserve_out = reserve_a, reserve_b
            token_in = token_a_hex
            token_out = token_b_hex
            direction = "a_to_b"
        elif token_in_hex == token_b_hex:
            reserve_in, reserve_out = reserve_b, reserve_a
            token_in = token_b_hex
            token_out = token_a_hex
            direction = "b_to_a"
        else:
            return {"error": "token_in not in this pool"}

        amount_in_with_fee = amount_in * (10000 - fee_bps)
        amount_out = (amount_in_with_fee * reserve_out) // (reserve_in * 10000 + amount_in_with_fee)
        if amount_out < min_amount_out or amount_out <= 0:
            return {"error": "slippage"}

        if direction == "a_to_b":
            reserve_a += amount_in
            reserve_b -= amount_out
        else:
            reserve_a -= amount_out
            reserve_b += amount_in
        if _unencodable(amount_in, amount_out, reserve_a, reserve_b):
            return {"error": "amount exceeds the pool's u64 ceiling"}

        runtime.token_transfer_from(token_in, sender, pool_addr, amount_in)
        runtime.token_transfer(token_out, sender, amount_out)

        self.storage["reserve_a"] = reserve_a
        self.storage["reserve_b"] = reserve_b

        result = {
            "status": "swapped",
            "token_in": token_in_hex,
            "amount_in": amount_in,
            "amount_out": amount_out,
            "reserve_a": reserve_a,
            "reserve_b": reserve_b,
        }
        runtime.emit_event("swap", result)
        return result

    @public(decode="cbor")
    def remove_liquidity(self, payload):
        data = payload or {}
        sender = self.sender
        if _sender_mismatch(data, sender):
            return {"error": "sender mismatch"}
        lp_amount = int(data["lp_amount"])
        pool_addr = self.storage.get("pool_address", "") or runtime.get_actor_address()

        token_a = self.storage["token_a"]
        token_b = self.storage["token_b"]
        lp_token = self.storage["lp_token"]
        reserve_a = int(self.storage.get("reserve_a", 0))
        reserve_b = int(self.storage.get("reserve_b", 0))
        total_supply = int(runtime.token_total_supply(lp_token))
        if lp_amount <= 0 or total_supply <= 0:
            return {"error": "invalid lp_amount"}

        amount_a = (lp_amount * reserve_a) // total_supply
        amount_b = (lp_amount * reserve_b) // total_supply
        if _unencodable(lp_amount, amount_a, amount_b):
            return {"error": "amount exceeds the pool's u64 ceiling"}

        runtime.token_transfer_from(lp_token, sender, pool_addr, lp_amount)
        runtime.token_burn(lp_token, lp_amount)
        runtime.token_transfer(token_a, sender, amount_a)
        runtime.token_transfer(token_b, sender, amount_b)

        reserve_a -= amount_a
        reserve_b -= amount_b
        self.storage["reserve_a"] = reserve_a
        self.storage["reserve_b"] = reserve_b

        result = {
            "status": "liquidity_removed",
            "lp_burned": lp_amount,
            "amount_a": amount_a,
            "amount_b": amount_b,
            "reserve_a": reserve_a,
            "reserve_b": reserve_b,
        }
        runtime.emit_event("liquidity_removed", result)
        return result

    @pure
    @public(decode="cbor")
    def get_reserves(self, payload):
        lp_total_supply = 0
        if self.storage.get("lp_token"):
            lp_total_supply = int(runtime.token_total_supply(self.storage["lp_token"]))
        # The reserves are bounded by the write guards, but the LP supply comes
        # straight from the u128 host — don't let it into the emit unchecked.
        if _unencodable(lp_total_supply):
            return {"error": "lp_total_supply exceeds the pool's u64 ceiling"}
        result = {
            "token_a": self.storage.get("token_a", ""),
            "token_b": self.storage.get("token_b", ""),
            "reserve_a": int(self.storage.get("reserve_a", 0)),
            "reserve_b": int(self.storage.get("reserve_b", 0)),
            "lp_total_supply": lp_total_supply,
            "fee_bps": int(self.storage.get("fee_bps", 30)),
        }
        runtime.emit_event("reserves", result)
        return result


def _get_actor():
    return V2Pool()


def init(payload):
    return _get_actor().init(payload)


def init_pool(payload):
    return _get_actor().init_pool(payload)


def add_liquidity(payload):
    return _get_actor().add_liquidity(payload)


def swap(payload):
    return _get_actor().swap(payload)


def remove_liquidity(payload):
    return _get_actor().remove_liquidity(payload)


def get_reserves(payload):
    return _get_actor().get_reserves(payload)
