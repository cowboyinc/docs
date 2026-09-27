import json

from cowboy_sdk import OWNER, SELF, Address, TokenId, actor, call, callable_by, codec, deferred, public, runtime


DEFAULT_ROUND_DURATION = 20
DEFAULT_FEE_BPS = 0
DEFAULT_AUTO_RESTART_DELAY = 1
DEFAULT_MAX_RECENT_ROUNDS = 32
DEFAULT_MAX_DETAILED_ROUNDS = 1

ROUND_SUMMARY_FIELDS = (
    "state",
    "detail_level",
    "open_height",
    "close_height",
    "resolved_height",
    "resolved_price",
    "total_pot",
    "bet_count",
    "winner_count",
    "winning_delta",
    "payouts",
)


def _round_key(round_id, suffix):
    return f"round:{round_id}:{suffix}"


def _schedule_cycle_timer(target_height, round_id):
    delay_blocks = max(1, int(target_height) - runtime.get_block_height())
    runtime.schedule_after(
        delay_blocks, "advance_cycle", codec.encode({"round_id": round_id})
    )


def _timer_payload(payload):
    """Decode the payload a timer fire delivers to a routed handler.

    A timer routed by `schedule_after` delivers the envelope's `_payload` as a
    JSON list of byte values — the leading `[` marks that form, and the bytes it
    holds are the CBOR that was scheduled. The JSON fallback keeps a
    differently-shaped fire from killing the auto-cycle, which nothing re-arms.
    """
    if not payload:
        return {}
    if payload[:1] == b"[":
        payload = bytes(json.loads(payload))
    try:
        return codec.decode(payload) or {}
    except Exception:
        return json.loads(payload) or {}


def _collect_bets(storage, round_id):
    ordered = []
    bettors = storage.get(_round_key(round_id, "bettors"), []) or []
    for bettor in bettors:
        value = storage.get(_round_key(round_id, f"bet:{bettor}"))
        if isinstance(value, dict):
            item = {
                "bettor": bettor,
                "guess": value.get("guess", 0),
                "stake": value.get("stake", 0),
                "net_stake": value.get("net_stake", 0),
            }
            inserted = False
            for index in range(len(ordered)):
                if item["bettor"] < ordered[index]["bettor"]:
                    ordered.insert(index, item)
                    inserted = True
                    break
            if not inserted:
                ordered.append(item)
    return ordered


def _resolve_winners(bets, price):
    winners = []
    best_delta = None
    for bet in bets:
        delta = int(bet["guess"]) - int(price)
        if delta < 0:
            delta = 0 - delta
        if best_delta is None or delta < best_delta:
            best_delta = delta
            winners = [bet]
        elif delta == best_delta:
            winners.append(bet)
    return {
        "winners": winners,
        "winning_delta": best_delta if best_delta is not None else 0,
    }


def _split_pot(total_pot, winners):
    if not winners:
        return []
    ordered = []
    for winner in winners:
        inserted = False
        for index in range(len(ordered)):
            if winner["bettor"] < ordered[index]["bettor"]:
                ordered.insert(index, winner)
                inserted = True
                break
        if not inserted:
            ordered.append(winner)
    base = total_pot // len(ordered)
    remainder = total_pot % len(ordered)
    payouts = []
    index = 0
    for winner in ordered:
        amount = base + (1 if index < remainder else 0)
        payouts.append({"bettor": winner["bettor"], "amount": amount})
        index += 1
    return payouts


def _delete_key(storage, key):
    if key in storage:
        del storage[key]


def _round_exists(storage, round_id):
    if storage.get(_round_key(round_id, "state"), ""):
        return True
    if storage.get(_round_key(round_id, "detail_level"), ""):
        return True
    if storage.get(_round_key(round_id, "payouts")):
        return True
    return False


def _round_detail_level(storage, round_id):
    level = storage.get(_round_key(round_id, "detail_level"), "")
    if level:
        return level
    if storage.get(_round_key(round_id, "state"), ""):
        return "full"
    return ""


def _round_payouts(storage, round_id):
    stored = storage.get(_round_key(round_id, "payouts"))
    if isinstance(stored, list):
        payouts = []
        for item in stored:
            if isinstance(item, dict) and item.get("bettor"):
                payouts.append(
                    {
                        "bettor": str(item.get("bettor")),
                        "amount": int(item.get("amount", 0)),
                    }
                )
        payouts.sort(key=lambda item: item["bettor"])
        return payouts

    payouts = []
    bettors = storage.get(_round_key(round_id, "bettors"), []) or []
    for bettor in bettors:
        amount = int(storage.get(_round_key(round_id, f"payout:{bettor}"), 0) or 0)
        if amount > 0:
            payouts.append({"bettor": bettor, "amount": amount})
    payouts.sort(key=lambda item: item["bettor"])
    return payouts


def _summary_round_payload(storage, round_id):
    return {
        "round_id": round_id,
        "state": storage.get(_round_key(round_id, "state"), ""),
        "detail_level": _round_detail_level(storage, round_id) or "summary",
        "open_height": int(storage.get(_round_key(round_id, "open_height"), 0)),
        "close_height": int(storage.get(_round_key(round_id, "close_height"), 0)),
        "resolved_height": int(storage.get(_round_key(round_id, "resolved_height"), 0)),
        "resolved_price": int(storage.get(_round_key(round_id, "resolved_price"), -1)),
        "total_pot": int(storage.get(_round_key(round_id, "total_pot"), 0)),
        "bet_count": int(storage.get(_round_key(round_id, "bet_count"), 0)),
        "winner_count": int(storage.get(_round_key(round_id, "winner_count"), 0)),
        "winning_delta": int(storage.get(_round_key(round_id, "winning_delta"), -1)),
        "payouts": _round_payouts(storage, round_id),
    }


@actor
class PredictionCasino:
    def init(self, payload):
        runtime.assume_ownership_if_unowned()
        self.storage["owner"] = self.sender.to_hex(checksum=False)
        self.storage["self_actor"] = ""
        self.storage["feed_actor"] = ""
        self.storage["token_id"] = ""
        self.storage["round_duration_blocks"] = DEFAULT_ROUND_DURATION
        self.storage["fee_bps"] = DEFAULT_FEE_BPS
        self.storage["auto_enabled"] = 0
        self.storage["auto_restart_delay_blocks"] = DEFAULT_AUTO_RESTART_DELAY
        self.storage["round_counter"] = 0
        self.storage["active_round_id"] = 0
        self.storage["vault_balance"] = 0
        self.storage["house_balance"] = 0
        self.storage["max_recent_rounds"] = DEFAULT_MAX_RECENT_ROUNDS
        self.storage["max_detailed_rounds"] = DEFAULT_MAX_DETAILED_ROUNDS
        self.storage["resolved_round_ids"] = []
        self.storage["pruned_before_round_id"] = 0
        runtime.emit_event("casino.init", {"owner": self.storage["owner"]})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def configure(self, payload):
        data = payload or {}
        self_actor_raw = data.get("self_actor", self.storage.get("self_actor", ""))
        self_actor = Address(self_actor_raw).to_hex(checksum=False) if self_actor_raw else ""
        feed_actor_raw = data.get("feed_actor", data.get("feed", self.storage.get("feed_actor", "")))
        feed_actor = Address(feed_actor_raw).to_hex(checksum=False) if feed_actor_raw else ""
        token_id_raw = data.get("token_id", self.storage.get("token_id", ""))
        token_id = TokenId(token_id_raw).to_hex() if token_id_raw else ""
        round_duration_blocks = int(data.get("round_duration_blocks", self.storage.get("round_duration_blocks", DEFAULT_ROUND_DURATION)))
        fee_bps = int(data.get("fee_bps", self.storage.get("fee_bps", DEFAULT_FEE_BPS)))
        auto_restart_delay_blocks = int(
            data.get(
                "auto_restart_delay_blocks",
                self.storage.get("auto_restart_delay_blocks", DEFAULT_AUTO_RESTART_DELAY),
            )
        )
        max_recent_rounds = int(data.get("max_recent_rounds", self.storage.get("max_recent_rounds", DEFAULT_MAX_RECENT_ROUNDS)))
        max_detailed_rounds = int(data.get("max_detailed_rounds", self.storage.get("max_detailed_rounds", DEFAULT_MAX_DETAILED_ROUNDS)))

        if not self_actor:
            runtime.emit_event("error", {"reason": "missing_self_actor"})
            return b"bad_self"
        if not feed_actor:
            runtime.emit_event("error", {"reason": "missing_feed_actor"})
            return b"bad_feed"
        if not token_id:
            runtime.emit_event("error", {"reason": "missing_token_id"})
            return b"bad_token"
        if round_duration_blocks <= 0:
            runtime.emit_event("error", {"reason": "bad_round_duration"})
            return b"bad_duration"
        if fee_bps < 0 or fee_bps > 10000:
            runtime.emit_event("error", {"reason": "bad_fee_bps"})
            return b"bad_fee"
        if auto_restart_delay_blocks < 0:
            runtime.emit_event("error", {"reason": "bad_auto_restart_delay"})
            return b"bad_auto_delay"
        if max_recent_rounds < 0 or max_detailed_rounds < 0 or max_detailed_rounds > max_recent_rounds:
            runtime.emit_event("error", {"reason": "bad_retention"})
            return b"bad_retention"

        self.storage["self_actor"] = self_actor
        self.storage["feed_actor"] = feed_actor
        self.storage["token_id"] = token_id
        self.storage["round_duration_blocks"] = round_duration_blocks
        self.storage["fee_bps"] = fee_bps
        self.storage["auto_restart_delay_blocks"] = auto_restart_delay_blocks
        self.storage["max_recent_rounds"] = max_recent_rounds
        self.storage["max_detailed_rounds"] = max_detailed_rounds
        token_balance = int(runtime.token_balance_of(token_id, self_actor) or 0)
        vault_balance = int(self.storage.get("vault_balance", 0))
        self.storage["house_balance"] = max(0, token_balance - vault_balance)
        self._apply_round_retention()
        runtime.emit_event(
            "casino.configured",
            {
                "self_actor": self_actor,
                "feed_actor": feed_actor,
                "token_id": token_id,
                "round_duration_blocks": round_duration_blocks,
                "fee_bps": fee_bps,
                "auto_restart_delay_blocks": auto_restart_delay_blocks,
                "max_recent_rounds": max_recent_rounds,
                "max_detailed_rounds": max_detailed_rounds,
                "house_balance": int(self.storage.get("house_balance", 0)),
            },
        )
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def config_feed(self, payload):
        data = payload or {}
        return self.configure(
            codec.encode(
                {
                    "feed_actor": data.get("feed", data.get("feed_actor", "")),
                    "self_actor": data.get("self_actor", self.storage.get("self_actor", "")),
                    "token_id": data.get("token_id", self.storage.get("token_id", "")),
                    "round_duration_blocks": data.get("round_duration_blocks", self.storage.get("round_duration_blocks", DEFAULT_ROUND_DURATION)),
                    "fee_bps": data.get("fee_bps", self.storage.get("fee_bps", DEFAULT_FEE_BPS)),
                    "max_recent_rounds": data.get("max_recent_rounds", self.storage.get("max_recent_rounds", DEFAULT_MAX_RECENT_ROUNDS)),
                    "max_detailed_rounds": data.get("max_detailed_rounds", self.storage.get("max_detailed_rounds", DEFAULT_MAX_DETAILED_ROUNDS)),
                }
            )
        )

    def _record_resolved_round(self, round_id):
        resolved_round_ids = list(self.storage.get("resolved_round_ids", []) or [])
        if round_id not in resolved_round_ids:
            resolved_round_ids.append(round_id)
        resolved_round_ids.sort()
        self.storage["resolved_round_ids"] = resolved_round_ids

    def _downgrade_round_to_summary(self, round_id):
        if _round_detail_level(self.storage, round_id) != "full":
            return False

        bettors = list(self.storage.get(_round_key(round_id, "bettors"), []) or [])
        payouts = _round_payouts(self.storage, round_id)
        self.storage[_round_key(round_id, "payouts")] = payouts
        self.storage[_round_key(round_id, "detail_level")] = "summary"
        for bettor in bettors:
            _delete_key(self.storage, _round_key(round_id, f"bet:{bettor}"))
            _delete_key(self.storage, _round_key(round_id, f"payout:{bettor}"))
        _delete_key(self.storage, _round_key(round_id, "bettors"))
        runtime.emit_event(
            "round_pruned",
            {
                "round_id": round_id,
                "pruned_detail": True,
                "pruned_summary": False,
            },
        )
        return True

    def _prune_round_fully(self, round_id):
        if not _round_exists(self.storage, round_id):
            return False

        detail_level = _round_detail_level(self.storage, round_id)
        bettors = list(self.storage.get(_round_key(round_id, "bettors"), []) or [])
        for bettor in bettors:
            _delete_key(self.storage, _round_key(round_id, f"bet:{bettor}"))
            _delete_key(self.storage, _round_key(round_id, f"payout:{bettor}"))
        _delete_key(self.storage, _round_key(round_id, "bettors"))
        for field in ROUND_SUMMARY_FIELDS:
            _delete_key(self.storage, _round_key(round_id, field))
        self.storage["pruned_before_round_id"] = max(int(self.storage.get("pruned_before_round_id", 0)), round_id)
        runtime.emit_event(
            "round_pruned",
            {
                "round_id": round_id,
                "pruned_detail": detail_level == "full",
                "pruned_summary": True,
            },
        )
        return True

    def _apply_round_retention(self):
        resolved_round_ids = list(self.storage.get("resolved_round_ids", []) or [])
        max_recent_rounds = int(self.storage.get("max_recent_rounds", DEFAULT_MAX_RECENT_ROUNDS))
        max_detailed_rounds = int(self.storage.get("max_detailed_rounds", DEFAULT_MAX_DETAILED_ROUNDS))

        while len(resolved_round_ids) > max_recent_rounds:
            evicted_round_id = resolved_round_ids.pop(0)
            self._prune_round_fully(evicted_round_id)

        if max_detailed_rounds <= 0:
            downgrade_ids = list(resolved_round_ids)
        else:
            downgrade_ids = resolved_round_ids[:-max_detailed_rounds]
        for round_id in downgrade_ids:
            self._downgrade_round_to_summary(round_id)

        self.storage["resolved_round_ids"] = resolved_round_ids

    @callable_by(OWNER, decode="cbor")
    def open_round(self, payload):
        active_round_id = int(self.storage.get("active_round_id", 0))
        if active_round_id:
            state = self.storage.get(_round_key(active_round_id, "state"), "")
            if state in ("open", "closing"):
                runtime.emit_event("error", {"reason": "round_in_progress"})
                return b"busy"

        round_id = int(self.storage.get("round_counter", 0)) + 1
        close_height = runtime.get_block_height() + int(self.storage.get("round_duration_blocks", DEFAULT_ROUND_DURATION))

        self.storage["round_counter"] = round_id
        self.storage["active_round_id"] = round_id
        self.storage[_round_key(round_id, "state")] = "open"
        self.storage[_round_key(round_id, "detail_level")] = "full"
        self.storage[_round_key(round_id, "open_height")] = runtime.get_block_height()
        self.storage[_round_key(round_id, "close_height")] = close_height
        self.storage[_round_key(round_id, "resolved_height")] = 0
        self.storage[_round_key(round_id, "total_pot")] = 0
        self.storage[_round_key(round_id, "bet_count")] = 0
        self.storage[_round_key(round_id, "winner_count")] = 0
        self.storage[_round_key(round_id, "bettors")] = []
        self.storage[_round_key(round_id, "payouts")] = []
        self.storage[_round_key(round_id, "resolved_price")] = -1
        self.storage[_round_key(round_id, "winning_delta")] = -1
        if int(self.storage.get("auto_enabled", 0)) == 1:
            _schedule_cycle_timer(close_height, round_id)
        runtime.emit_event(
            "round_opened",
            {
                "round_id": round_id,
                "open_height": int(self.storage.get(_round_key(round_id, "open_height"), 0)),
                "close_height": close_height,
            },
        )
        return str(round_id).encode()

    @callable_by(OWNER, decode="raw")
    def create_round(self, payload):
        return self.open_round(payload)

    @public(decode="cbor")
    def place_bet(self, payload):
        data = payload or {}
        round_id = int(data.get("round_id", self.storage.get("active_round_id", 0)))
        if round_id <= 0:
            runtime.emit_event("error", {"reason": "no_active_round"})
            return b"no_round"
        if self.storage.get(_round_key(round_id, "state"), "") != "open":
            runtime.emit_event("error", {"reason": "round_not_open"})
            return b"not_open"
        if runtime.get_block_height() >= int(self.storage.get(_round_key(round_id, "close_height"), 0)):
            runtime.emit_event("error", {"reason": "betting_closed"})
            return b"closed"

        stake = int(data.get("stake", 0))
        guess = int(data.get("guess", 0))
        if stake <= 0:
            runtime.emit_event("error", {"reason": "bad_stake"})
            return b"bad_stake"

        bettor_arg = data.get("bettor")
        if bettor_arg:
            bettor = Address(bettor_arg).to_hex(checksum=False)
        else:
            bettor = self.sender.to_hex(checksum=False)
        bet_key = _round_key(round_id, f"bet:{bettor}")
        if self.storage.get(bet_key):
            runtime.emit_event("error", {"reason": "duplicate_bet"})
            return b"duplicate"

        fee_bps = int(self.storage.get("fee_bps", DEFAULT_FEE_BPS))
        fee = (stake * fee_bps) // 10000
        net_stake = stake - fee
        if net_stake <= 0:
            runtime.emit_event("error", {"reason": "stake_consumed_by_fee"})
            return b"bad_stake"

        token_id = self.storage.get("token_id", "")
        casino_actor = self.storage.get("self_actor", "")
        prepaid = bool(data.get("prepaid", False))
        if prepaid:
            current_balance = int(runtime.token_balance_of(token_id, casino_actor) or 0)
            expected_balance = int(self.storage.get("house_balance", 0)) + int(self.storage.get("vault_balance", 0)) + stake
            if current_balance < expected_balance:
                runtime.emit_event(
                    "error",
                    {
                        "reason": "missing_escrow",
                        "current_balance": current_balance,
                        "expected_balance": expected_balance,
                    },
                )
                return b"missing_escrow"
        else:
            runtime.token_transfer_from(token_id, bettor, casino_actor, stake)

        bettors = self.storage.get(_round_key(round_id, "bettors"), []) or []
        bettors.append(bettor)
        self.storage[bet_key] = {"guess": guess, "stake": stake, "net_stake": net_stake}
        self.storage[_round_key(round_id, "bettors")] = bettors
        bet_count = int(self.storage.get(_round_key(round_id, "bet_count"), 0)) + 1
        total_pot = int(self.storage.get(_round_key(round_id, "total_pot"), 0)) + net_stake
        self.storage[_round_key(round_id, "bet_count")] = bet_count
        self.storage[_round_key(round_id, "total_pot")] = total_pot
        self.storage["vault_balance"] = int(self.storage.get("vault_balance", 0)) + stake
        runtime.emit_event(
            "bet_placed",
            {
                "round_id": round_id,
                "bettor": bettor,
                "guess": guess,
                "stake": stake,
                "net_stake": net_stake,
                "pot_after": total_pot,
                "bet_count_after": bet_count,
            },
        )
        return b"ok"

    @public(decode="raw")
    def join_round(self, payload):
        return self.place_bet(payload)

    @public(decode="cbor")
    @deferred
    def handle_message(self, payload):
        data = payload or {}
        cmd = data.get("cmd", "")
        if cmd == "submit_bet":
            return self.place_bet(codec.encode(data))
        if cmd == "settle_round":
            return self.settle_round(codec.encode(data))
        runtime.emit_event("error", {"reason": "unknown_message", "cmd": cmd})
        return b"unknown_cmd"

    @public(decode="cbor")
    @deferred
    def close_round(self, payload):
        round_id = int((payload or {}).get("round_id", self.storage.get("active_round_id", 0)))
        if round_id <= 0:
            runtime.emit_event("error", {"reason": "no_round"})
            return b"no_round"
        if self.storage.get(_round_key(round_id, "state"), "") != "open":
            runtime.emit_event("error", {"reason": "round_not_open"})
            return b"not_open"

        feed_actor = self.storage.get("feed_actor", "")
        self_actor = self.storage.get("self_actor", "")
        if not feed_actor or not self_actor:
            runtime.emit_event("error", {"reason": "casino_not_configured"})
            return b"bad_config"
        self.storage[_round_key(round_id, "state")] = "closing"
        feed_result = call(feed_actor, "generate_price", {}, 1_000_000)
        settlement_payload = codec.encode(
            {
                "round_id": round_id,
                "price": int(feed_result.get("price", 0)),
                "price_height": int(feed_result.get("height", runtime.get_block_height())),
            }
        )
        runtime.emit_event("casino.round_closing", {"round_id": round_id, "feed_actor": feed_actor})
        return self.settle_round(settlement_payload)

    @public(decode="raw")
    def finalize_round(self, payload):
        return self.close_round(payload)

    @callable_by(OWNER, decode="cbor")
    @deferred
    def start_auto(self, payload):
        self.storage["auto_enabled"] = 1
        data = payload or {}
        if "auto_restart_delay_blocks" in data:
            self.storage["auto_restart_delay_blocks"] = max(0, int(data.get("auto_restart_delay_blocks", DEFAULT_AUTO_RESTART_DELAY)))

        active_round_id = int(self.storage.get("active_round_id", 0))
        if active_round_id > 0:
            close_height = int(self.storage.get(_round_key(active_round_id, "close_height"), 0))
            _schedule_cycle_timer(close_height, active_round_id)
            runtime.emit_event("casino.auto_started", {"active_round_id": active_round_id, "close_height": close_height})
            return b"ok"

        round_id = int(self.open_round(codec.encode({})).decode())
        runtime.emit_event("casino.auto_started", {"active_round_id": round_id})
        return b"ok"

    @callable_by(OWNER, decode="cbor")
    def stop_auto(self, payload):
        self.storage["auto_enabled"] = 0
        runtime.emit_event("casino.auto_stopped", {})
        return b"ok"

    @callable_by(SELF, decode="raw")
    @deferred
    def advance_cycle(self, payload):
        if int(self.storage.get("auto_enabled", 0)) != 1:
            return b"disabled"

        data = _timer_payload(payload)
        current_round_id = int(self.storage.get("active_round_id", 0))
        expected_round_id = int(data.get("round_id", current_round_id))
        current_state = self.storage.get(_round_key(current_round_id, "state"), "") if current_round_id else ""

        if current_round_id > 0 and current_state in ("open", "closing"):
            if expected_round_id and expected_round_id != current_round_id:
                runtime.emit_event(
                    "casino.auto_skipped",
                    {"reason": "stale_round_timer", "expected_round_id": expected_round_id, "active_round_id": current_round_id},
                )
                return b"stale"
            self.close_round(codec.encode({"round_id": current_round_id}))

        if int(self.storage.get("auto_enabled", 0)) != 1:
            return b"stopped"

        restart_delay = int(self.storage.get("auto_restart_delay_blocks", DEFAULT_AUTO_RESTART_DELAY))
        if restart_delay > 0:
            target_height = runtime.get_block_height() + restart_delay
            runtime.schedule_after(restart_delay, "open_next_round")
            runtime.emit_event("casino.auto_waiting", {"next_open_height": target_height})
            return b"waiting_to_reopen"

        self.open_round(codec.encode({}))
        return b"ok"

    @callable_by(SELF, decode="raw")
    @deferred
    def open_next_round(self, payload):
        if int(self.storage.get("auto_enabled", 0)) != 1:
            return b"disabled"
        self.open_round(codec.encode({}))
        return b"ok"

    @callable_by(SELF, decode="cbor")
    def settle_round(self, payload):
        data = payload or {}
        round_id = int(data.get("round_id", 0))
        price = int(data.get("price", 0))
        if round_id <= 0:
            runtime.emit_event("error", {"reason": "bad_round"})
            return b"bad_round"
        if self.storage.get(_round_key(round_id, "state"), "") != "closing":
            runtime.emit_event("error", {"reason": "round_not_closing"})
            return b"bad_state"

        bets = _collect_bets(self.storage, round_id)
        self.storage[_round_key(round_id, "resolved_price")] = price
        resolved_height = int(data.get("price_height", runtime.get_block_height()))
        self.storage[_round_key(round_id, "resolved_height")] = resolved_height

        if not bets:
            self.storage[_round_key(round_id, "state")] = "resolved"
            self.storage[_round_key(round_id, "winning_delta")] = -1
            self.storage[_round_key(round_id, "winner_count")] = 0
            self.storage[_round_key(round_id, "payouts")] = []
            self.storage["active_round_id"] = 0
            self._record_resolved_round(round_id)
            self._apply_round_retention()
            runtime.emit_event(
                "round_settled",
                {
                    "round_id": round_id,
                    "resolved_height": resolved_height,
                    "resolved_price": price,
                    "total_pot": 0,
                    "winner_count": 0,
                    "winning_delta": -1,
                },
            )
            return b"ok"

        resolution = _resolve_winners(bets, price)
        winners = resolution["winners"]
        winning_delta = resolution["winning_delta"]
        total_pot = int(self.storage.get(_round_key(round_id, "total_pot"), 0))
        payouts = _split_pot(total_pot, winners)
        token_id = self.storage.get("token_id", "")

        for payout in payouts:
            bettor = payout["bettor"]
            amount = payout["amount"]
            runtime.token_transfer(token_id, bettor, amount)
            self.storage[_round_key(round_id, f"payout:{bettor}")] = amount
            self.storage["vault_balance"] = int(self.storage.get("vault_balance", 0)) - amount
            runtime.emit_event(
                "payout_issued",
                {
                    "round_id": round_id,
                    "winner": bettor,
                    "payout": amount,
                },
            )

        self.storage[_round_key(round_id, "state")] = "resolved"
        self.storage[_round_key(round_id, "winning_delta")] = winning_delta
        self.storage[_round_key(round_id, "winner_count")] = len(winners)
        self.storage[_round_key(round_id, "payouts")] = payouts
        self.storage["active_round_id"] = 0
        self._record_resolved_round(round_id)
        self._apply_round_retention()
        runtime.emit_event(
            "round_settled",
            {
                "round_id": round_id,
                "resolved_height": resolved_height,
                "resolved_price": price,
                "winning_delta": winning_delta,
                "winner_count": len(winners),
                "total_pot": total_pot,
            },
        )
        return b"ok"

    @public(decode="cbor")
    def inspect_round(self, payload):
        data = payload or {}
        round_id = int(data.get("round_id", 0))
        if round_id <= 0:
            round_id = int(self.storage.get("active_round_id", 0))
        if round_id <= 0:
            round_id = int(self.storage.get("round_counter", 0))
        if round_id <= 0:
            return {"round_id": 0, "state": "none"}
        if not _round_exists(self.storage, round_id):
            if round_id <= int(self.storage.get("pruned_before_round_id", 0)) or round_id <= int(self.storage.get("round_counter", 0)):
                return {"round_id": round_id, "state": "pruned", "detail_level": "pruned"}
            return {"round_id": round_id, "state": "none"}

        detail_level = _round_detail_level(self.storage, round_id) or "full"
        bets = _collect_bets(self.storage, round_id) if detail_level == "full" else []
        payouts = _round_payouts(self.storage, round_id)

        return {
            "round_id": round_id,
            "state": self.storage.get(_round_key(round_id, "state"), ""),
            "detail_level": detail_level,
            "open_height": int(self.storage.get(_round_key(round_id, "open_height"), 0)),
            "close_height": int(self.storage.get(_round_key(round_id, "close_height"), 0)),
            "resolved_height": int(self.storage.get(_round_key(round_id, "resolved_height"), 0)),
            "total_pot": int(self.storage.get(_round_key(round_id, "total_pot"), 0)),
            "bet_count": int(self.storage.get(_round_key(round_id, "bet_count"), 0)),
            "winner_count": int(self.storage.get(_round_key(round_id, "winner_count"), 0)),
            "resolved_price": int(self.storage.get(_round_key(round_id, "resolved_price"), -1)),
            "winning_delta": int(self.storage.get(_round_key(round_id, "winning_delta"), -1)),
            "bets": bets,
            "payouts": payouts,
        }

    @public(decode="cbor")
    def get_state(self, payload):
        active_round_id = int(self.storage.get("active_round_id", 0))
        return {
            "feed_actor": self.storage.get("feed_actor", ""),
            "self_actor": self.storage.get("self_actor", ""),
            "token_id": self.storage.get("token_id", ""),
            "round_duration_blocks": int(self.storage.get("round_duration_blocks", DEFAULT_ROUND_DURATION)),
            "fee_bps": int(self.storage.get("fee_bps", DEFAULT_FEE_BPS)),
            "round_counter": int(self.storage.get("round_counter", 0)),
            "active_round_id": active_round_id,
            "active_round_state": self.storage.get(_round_key(active_round_id, "state"), "") if active_round_id else "",
            "vault_balance": int(self.storage.get("vault_balance", 0)),
            "house_balance": int(self.storage.get("house_balance", 0)),
            "auto_enabled": bool(int(self.storage.get("auto_enabled", 0))),
            "auto_restart_delay_blocks": int(self.storage.get("auto_restart_delay_blocks", DEFAULT_AUTO_RESTART_DELAY)),
            "max_recent_rounds": int(self.storage.get("max_recent_rounds", DEFAULT_MAX_RECENT_ROUNDS)),
            "max_detailed_rounds": int(self.storage.get("max_detailed_rounds", DEFAULT_MAX_DETAILED_ROUNDS)),
            "pruned_before_round_id": int(self.storage.get("pruned_before_round_id", 0)),
        }


def _get_actor():
    return PredictionCasino()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def config_feed(payload):
    return _get_actor().config_feed(payload)


def open_round(payload):
    return _get_actor().open_round(payload)


def create_round(payload):
    return _get_actor().create_round(payload)


def place_bet(payload):
    return _get_actor().place_bet(payload)


def join_round(payload):
    return _get_actor().join_round(payload)


def close_round(payload):
    return _get_actor().close_round(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)


def finalize_round(payload):
    return _get_actor().finalize_round(payload)


def settle_round(payload):
    return _get_actor().settle_round(payload)


def start_auto(payload):
    return _get_actor().start_auto(payload)


def stop_auto(payload):
    return _get_actor().stop_auto(payload)


def advance_cycle(payload):
    return _get_actor().advance_cycle(payload)


def open_next_round(payload):
    return _get_actor().open_next_round(payload)


def inspect_round(payload):
    return _get_actor().inspect_round(payload)


def get_state(payload):
    return _get_actor().get_state(payload)
