from cowboy_sdk import OWNER, SELF, Address, TokenId, actor, call, callable_by, codec, deferred, public, runtime


DEFAULT_INTERVAL = 15
DEFAULT_MIN_GUESS = 0
DEFAULT_MAX_GUESS = 1000
DEFAULT_STAKE = 100
ROUND_CHECK_RETRY_BLOCKS = 1


def _guess_for_height(min_guess, max_guess, block_height, sequence, entropy=""):
    span = max_guess - min_guess + 1
    if span <= 0:
        return min_guess
    entropy_value = 0
    for char in str(entropy):
        entropy_value = ((entropy_value * 131) + ord(char)) & 0x7FFFFFFF
    seed = (
        ((block_height + 11) * 1664525)
        + ((sequence + 17) * 1013904223)
        + entropy_value
    ) & 0x7FFFFFFF
    return min_guess + (seed % span)


def _next_attempt_delay(current_height, interval_blocks, round_state):
    if round_state.get("state", "") != "open":
        return min(interval_blocks, ROUND_CHECK_RETRY_BLOCKS)

    close_height = int(round_state.get("close_height", 0))
    if close_height <= current_height + 1:
        return interval_blocks

    return min(interval_blocks, ROUND_CHECK_RETRY_BLOCKS)


@actor
class AutoBettor:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        self.storage["owner"] = self.sender.to_hex(checksum=False)
        self.storage["self_actor"] = ""
        self.storage["casino_actor"] = ""
        self.storage["token_id"] = ""
        self.storage["bet_interval_blocks"] = DEFAULT_INTERVAL
        self.storage["min_guess"] = DEFAULT_MIN_GUESS
        self.storage["max_guess"] = DEFAULT_MAX_GUESS
        self.storage["bet_amount"] = DEFAULT_STAKE
        self.storage["enabled"] = 0
        self.storage["next_bet_height"] = 0
        self.storage["bet_sequence"] = 0
        self.storage["last_guess"] = 0
        self.storage["last_bet_height"] = 0
        runtime.emit_event("bettor.init", {"owner": self.storage["owner"]})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def configure(self, payload):
        data = payload or {}
        self_actor_raw = data.get("self_actor", self.storage.get("self_actor", ""))
        self_actor = Address(self_actor_raw).to_hex(checksum=False) if self_actor_raw else ""
        casino_actor_raw = data.get("casino_actor", data.get("casino", self.storage.get("casino_actor", "")))
        casino_actor = Address(casino_actor_raw).to_hex(checksum=False) if casino_actor_raw else ""
        token_id_raw = data.get("token_id", self.storage.get("token_id", ""))
        token_id = TokenId(token_id_raw).to_hex() if token_id_raw else ""
        min_guess = int(data.get("min_guess", data.get("range_min", self.storage.get("min_guess", DEFAULT_MIN_GUESS))))
        max_guess = int(data.get("max_guess", data.get("range_max", self.storage.get("max_guess", DEFAULT_MAX_GUESS))))
        interval_blocks = int(data.get("bet_interval_blocks", data.get("interval", self.storage.get("bet_interval_blocks", DEFAULT_INTERVAL))))
        bet_amount = int(data.get("bet_amount", data.get("base_stake", self.storage.get("bet_amount", DEFAULT_STAKE))))

        if not self_actor:
            runtime.emit_event("error", {"reason": "missing_self_actor"})
            return b"bad_self"
        if not casino_actor:
            runtime.emit_event("error", {"reason": "missing_casino_actor"})
            return b"bad_casino"
        if not token_id:
            runtime.emit_event("error", {"reason": "missing_token_id"})
            return b"bad_token"
        if min_guess > max_guess:
            runtime.emit_event("error", {"reason": "bad_guess_range"})
            return b"bad_range"
        if interval_blocks <= 0 or bet_amount <= 0:
            runtime.emit_event("error", {"reason": "bad_config"})
            return b"bad_config"

        self.storage["self_actor"] = self_actor
        self.storage["casino_actor"] = casino_actor
        self.storage["token_id"] = token_id
        self.storage["min_guess"] = min_guess
        self.storage["max_guess"] = max_guess
        self.storage["bet_interval_blocks"] = interval_blocks
        self.storage["bet_amount"] = bet_amount
        runtime.emit_event(
            "bettor.configured",
            {
                "self_actor": self_actor,
                "casino_actor": casino_actor,
                "token_id": token_id,
                "min_guess": min_guess,
                "max_guess": max_guess,
                "bet_interval_blocks": interval_blocks,
                "bet_amount": bet_amount,
            },
        )
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def setup(self, payload):
        return self.configure(payload)

    @callable_by(OWNER, decode="cbor")
    @deferred
    def start(self, payload):
        if not self.storage.get("casino_actor", ""):
            runtime.emit_event("error", {"reason": "missing_casino_actor"})
            return b"bad_casino"
        self.storage["enabled"] = 1
        initial_delay = min(int(self.storage.get("bet_interval_blocks", DEFAULT_INTERVAL)), ROUND_CHECK_RETRY_BLOCKS)
        self.storage["next_bet_height"] = runtime.get_block_height() + initial_delay
        runtime.schedule_after(initial_delay, "run")
        runtime.emit_event("bettor.started", {"next_bet_height": self.storage["next_bet_height"]})
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def start_auto_betting(self, payload):
        return self.start(payload)

    @callable_by(OWNER, decode="cbor")
    def stop(self, payload):
        self.storage["enabled"] = 0
        runtime.emit_event("bettor.stopped", {})
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def stop_auto_betting(self, payload):
        return self.stop(payload)

    @callable_by(SELF, decode="raw")
    @deferred
    def run(self, payload):
        if int(self.storage.get("enabled", 0)) != 1:
            return b"disabled"

        interval_blocks = int(self.storage.get("bet_interval_blocks", DEFAULT_INTERVAL))
        current_height = runtime.get_block_height()
        next_bet_height = int(self.storage.get("next_bet_height", 0))
        if current_height < next_bet_height:
            runtime.schedule_after(max(1, next_bet_height - current_height), "run")
            return b"waiting"

        sequence = int(self.storage.get("bet_sequence", 0))
        casino_actor = self.storage.get("casino_actor", "")
        round_state = call(casino_actor, "inspect_round", {}, 1_000_000)
        if round_state.get("state", "") != "open" or current_height >= int(round_state.get("close_height", 0)):
            next_delay = _next_attempt_delay(current_height, interval_blocks, round_state)
            self.storage["next_bet_height"] = current_height + next_delay
            runtime.schedule_after(next_delay, "run")
            runtime.emit_event(
                "bettor.skipped",
                {
                    "reason": "round_not_open",
                    "height": current_height,
                    "round_state": round_state.get("state", ""),
                    "next_bet_height": self.storage["next_bet_height"],
                },
            )
            return b"round_not_open"

        if any((bet or {}).get("bettor") == self.storage.get("self_actor", "") for bet in round_state.get("bets", [])):
            self.storage["next_bet_height"] = current_height + interval_blocks
            runtime.schedule_after(interval_blocks, "run")
            runtime.emit_event(
                "bettor.skipped",
                {
                    "reason": "already_joined_round",
                    "height": current_height,
                    "round_id": int(round_state.get("round_id", 0)),
                },
            )
            return b"already_joined_round"

        guess = _guess_for_height(
            int(self.storage.get("min_guess", DEFAULT_MIN_GUESS)),
            int(self.storage.get("max_guess", DEFAULT_MAX_GUESS)),
            current_height,
            sequence,
            self.storage.get("self_actor", ""),
        )
        token_id = self.storage.get("token_id", "")
        self_actor = self.storage.get("self_actor", "")
        bet_amount = int(self.storage.get("bet_amount", DEFAULT_STAKE))
        current_balance = int(runtime.token_balance_of(token_id, self_actor) or 0)
        if current_balance < bet_amount:
            self.storage["next_bet_height"] = current_height + interval_blocks
            runtime.schedule_after(interval_blocks, "run")
            runtime.emit_event(
                "bettor.skipped",
                {
                    "reason": "insufficient_funds",
                    "height": current_height,
                    "balance": current_balance,
                    "required": bet_amount,
                },
            )
            return b"insufficient_funds"

        runtime.token_transfer(
            token_id,
            casino_actor,
            bet_amount,
        )
        runtime.send_message(
            Address(casino_actor).to_bytes(),
            codec.encode(
                {
                    "cmd": "submit_bet",
                    "guess": guess,
                    "stake": bet_amount,
                    "bettor": self.storage.get("self_actor", ""),
                    "prepaid": True,
                }
            ),
        )
        next_delay = _next_attempt_delay(current_height, interval_blocks, round_state)
        self.storage["bet_sequence"] = sequence + 1
        self.storage["last_guess"] = guess
        self.storage["last_bet_height"] = current_height
        self.storage["next_bet_height"] = current_height + next_delay
        runtime.schedule_after(next_delay, "run")
        runtime.emit_event(
            "bettor.bet_sent",
            {
                "guess": guess,
                "stake": bet_amount,
                "height": current_height,
                "next_bet_height": self.storage["next_bet_height"],
            },
        )
        return b"ok"

    @callable_by(SELF, decode="raw")
    @deferred
    def make_bet(self, payload):
        return self.run(payload)

    @public(decode="cbor")
    def inspect(self, payload):
        return {
            "casino_actor": self.storage.get("casino_actor", ""),
            "self_actor": self.storage.get("self_actor", ""),
            "token_id": self.storage.get("token_id", ""),
            "bet_interval_blocks": int(self.storage.get("bet_interval_blocks", DEFAULT_INTERVAL)),
            "min_guess": int(self.storage.get("min_guess", DEFAULT_MIN_GUESS)),
            "max_guess": int(self.storage.get("max_guess", DEFAULT_MAX_GUESS)),
            "bet_amount": int(self.storage.get("bet_amount", DEFAULT_STAKE)),
            "enabled": bool(int(self.storage.get("enabled", 0))),
            "next_bet_height": int(self.storage.get("next_bet_height", 0)),
            "bet_sequence": int(self.storage.get("bet_sequence", 0)),
            "last_guess": int(self.storage.get("last_guess", 0)),
            "last_bet_height": int(self.storage.get("last_bet_height", 0)),
        }


def _get_actor():
    return AutoBettor()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def setup(payload):
    return _get_actor().setup(payload)


def start(payload):
    return _get_actor().start(payload)


def start_auto_betting(payload):
    return _get_actor().start_auto_betting(payload)


def stop(payload):
    return _get_actor().stop(payload)


def stop_auto_betting(payload):
    return _get_actor().stop_auto_betting(payload)


def run(payload):
    return _get_actor().run(payload)


def make_bet(payload):
    return _get_actor().make_bet(payload)


def inspect(payload):
    return _get_actor().inspect(payload)
