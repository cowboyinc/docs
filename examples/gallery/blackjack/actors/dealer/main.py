import json
from cowboy_sdk import OWNER, Address, actor, callable_by, public, pure, runtime, codec


SUITS = ("S", "H", "D", "C")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K")
TEN_VALUE_RANKS = {"T", "J", "Q", "K"}
PHASE_BETTING = "BETTING"
PHASE_INSURANCE = "INSURANCE"
PHASE_PLAYER_TURN = "PLAYER_TURN"
PHASE_DEALER_TURN = "DEALER_TURN"
PHASE_ROUND_COMPLETE = "ROUND_COMPLETE"


def _rank(card):
    return str(card or "").strip().upper()[:1]


def _normalize_card(card):
    text = str(card or "").strip().upper()
    if len(text) != 2 or text[0] not in RANKS or text[1] not in SUITS:
        raise ValueError(f"invalid card: {card}")
    return text


def _build_shoe(num_decks=6):
    shoe = []
    for _ in range(int(num_decks)):
        for rank in RANKS:
            for suit in SUITS:
                shoe.append(f"{rank}{suit}")
    return shoe


def _hand_value(cards):
    total = 0
    aces = 0
    for card in cards or []:
        rank = _rank(card)
        if rank == "A":
            total += 11
            aces += 1
        elif rank in TEN_VALUE_RANKS:
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return {"total": total, "soft": aces > 0}


def _is_blackjack(cards, from_split=False):
    return len(cards or []) == 2 and _hand_value(cards)["total"] == 21 and not bool(from_split)


def _can_split(cards):
    return len(cards or []) == 2 and _rank(cards[0]) == _rank(cards[1])


def _dealer_should_hit(cards):
    return _hand_value(cards)["total"] < 17


def _hand_snapshot(hand):
    value = _hand_value(hand.get("cards", []))
    return {
        "cards": list(hand.get("cards", [])),
        "wager": int(hand.get("wager", 0)),
        "status": str(hand.get("status", "ACTIVE")),
        "split": bool(hand.get("split", False)),
        "split_aces": bool(hand.get("split_aces", False)),
        "doubled": bool(hand.get("doubled", False)),
        "total": value["total"],
        "soft": value["soft"],
        "blackjack": _is_blackjack(hand.get("cards", []), hand.get("split", False)),
        "result": hand.get("result"),
        "payout": int(hand.get("payout", 0)),
    }


def _settle_hand(hand, dealer_cards):
    wager = int(hand.get("wager", 0))
    player_cards = list(hand.get("cards", []))
    dealer_value = _hand_value(dealer_cards)
    player_value = _hand_value(player_cards)
    player_blackjack = _is_blackjack(player_cards, hand.get("split", False))
    dealer_blackjack = _is_blackjack(dealer_cards, False)

    if dealer_blackjack:
        if player_blackjack:
            return ("push", wager)
        return ("lose", 0)
    if player_blackjack:
        return ("blackjack", (wager * 5) // 2)
    if player_value["total"] > 21:
        return ("bust", 0)
    if dealer_value["total"] > 21:
        return ("win", wager * 2)
    if player_value["total"] > dealer_value["total"]:
        return ("win", wager * 2)
    if player_value["total"] == dealer_value["total"]:
        return ("push", wager)
    return ("lose", 0)


@actor
class BlackjackDealer:
    """Single-table blackjack example with CIP-20 wager escrow."""

    def _owner(self):
        owner_bytes = runtime.get_owner()
        return Address(owner_bytes).to_hex(checksum=False) if owner_bytes else ""

    def _token_id(self):
        return str(self.storage.get("token_id", ""))

    def _joined_players(self):
        players = self.storage.get("players", [])
        return list(players) if isinstance(players, list) else []

    def _bets(self):
        bets = self.storage.get("bets", {})
        return dict(bets) if isinstance(bets, dict) else {}

    def _current_round(self):
        current = self.storage.get("current_round", {})
        return dict(current) if isinstance(current, dict) else {}

    def _set_current_round(self, round_state):
        self.storage["current_round"] = round_state

    def _require_owner(self):
        return self.sender.to_hex(checksum=False) == self._owner()

    def _ensure_shoe(self):
        shoe = self.storage.get("shoe", [])
        if not isinstance(shoe, list) or not shoe:
            round_id = int(self.storage.get("round_id", 0))
            fresh = _build_shoe(int(self.storage.get("num_decks", 6)))
            if fresh:
                offset = round_id % len(fresh)
                shoe = fresh[offset:] + fresh[:offset]
            self.storage["shoe"] = shoe
        return list(self.storage.get("shoe", []))

    def _draw_card(self):
        shoe = self._ensure_shoe()
        if not shoe:
            raise ValueError("shoe empty")
        card = _normalize_card(shoe.pop(0))
        self.storage["shoe"] = shoe
        return card

    def _round_players(self, round_state):
        return list(round_state.get("player_order", []))

    def _player_entry(self, round_state, player):
        players = dict(round_state.get("players", {}))
        return dict(players.get(player, {}))

    def _save_player_entry(self, round_state, player, entry):
        players = dict(round_state.get("players", {}))
        players[player] = entry
        round_state["players"] = players

    def _active_cursor(self, round_state):
        return int(round_state.get("active_player_index", 0)), int(round_state.get("active_hand_index", 0))

    def _set_active_cursor(self, round_state, player_index, hand_index):
        round_state["active_player_index"] = int(player_index)
        round_state["active_hand_index"] = int(hand_index)

    def _active_player(self, round_state):
        order = self._round_players(round_state)
        if not order:
            return ""
        idx = int(round_state.get("active_player_index", 0))
        if idx < 0 or idx >= len(order):
            return ""
        return order[idx]

    def _mark_hand_terminal(self, hand):
        total = _hand_value(hand.get("cards", []))["total"]
        if total > 21:
            hand["status"] = "BUST"
        elif _is_blackjack(hand.get("cards", []), hand.get("split", False)):
            hand["status"] = "BLACKJACK"
        elif hand.get("status") == "ACTIVE":
            hand["status"] = "STAND"
        return hand

    def _resolve_dealer_blackjack(self, round_state):
        dealer_has_blackjack = _is_blackjack(round_state.get("dealer_cards", []), False)
        payouts = {}
        player_results = {}
        for player in self._round_players(round_state):
            entry = self._player_entry(round_state, player)
            total_payout = 0
            insurance_bet = int(entry.get("insurance_bet", 0))
            if insurance_bet > 0 and dealer_has_blackjack:
                total_payout += insurance_bet * 3
            hands = []
            for hand in entry.get("hands", []):
                outcome, payout = _settle_hand(hand, round_state.get("dealer_cards", []))
                hand["status"] = "SETTLED"
                hand["result"] = outcome
                hand["payout"] = payout
                hands.append(hand)
                total_payout += payout
            entry["hands"] = hands
            self._save_player_entry(round_state, player, entry)
            payouts[player] = total_payout
            player_results[player] = [_hand_snapshot(hand) for hand in hands]
        round_state["phase"] = PHASE_ROUND_COMPLETE
        round_state["dealer_has_blackjack"] = dealer_has_blackjack
        return payouts, player_results

    def _begin_player_turns(self, round_state):
        players = self._round_players(round_state)
        for player_index, player in enumerate(players):
            entry = self._player_entry(round_state, player)
            hands = []
            first_playable = None
            for hand_index, hand in enumerate(entry.get("hands", [])):
                if _is_blackjack(hand.get("cards", []), hand.get("split", False)):
                    hand["status"] = "BLACKJACK"
                elif _hand_value(hand.get("cards", []))["total"] > 21:
                    hand["status"] = "BUST"
                else:
                    hand["status"] = "ACTIVE"
                    if first_playable is None:
                        first_playable = hand_index
                hands.append(hand)
            entry["hands"] = hands
            self._save_player_entry(round_state, player, entry)
            if first_playable is not None:
                round_state["phase"] = PHASE_PLAYER_TURN
                self._set_active_cursor(round_state, player_index, first_playable)
                return
        self._play_dealer_and_settle(round_state)

    def _advance_turn(self, round_state):
        order = self._round_players(round_state)
        current_player_index, current_hand_index = self._active_cursor(round_state)
        for player_index in range(current_player_index, len(order)):
            player = order[player_index]
            entry = self._player_entry(round_state, player)
            hands = list(entry.get("hands", []))
            start_hand = current_hand_index + 1 if player_index == current_player_index else 0
            for hand_index in range(start_hand, len(hands)):
                if hands[hand_index].get("status") == "ACTIVE":
                    self._set_active_cursor(round_state, player_index, hand_index)
                    round_state["phase"] = PHASE_PLAYER_TURN
                    return
        self._play_dealer_and_settle(round_state)

    def _play_dealer_and_settle(self, round_state):
        round_state["phase"] = PHASE_DEALER_TURN
        while _dealer_should_hit(round_state.get("dealer_cards", [])):
            round_state["dealer_cards"].append(self._draw_card())
        payouts = {}
        player_results = {}
        dealer_has_blackjack = _is_blackjack(round_state.get("dealer_cards", []), False)
        for player in self._round_players(round_state):
            entry = self._player_entry(round_state, player)
            total_payout = 0
            if dealer_has_blackjack and int(entry.get("insurance_bet", 0)) > 0:
                total_payout += int(entry.get("insurance_bet", 0)) * 3
            hands = []
            for hand in entry.get("hands", []):
                outcome, payout = _settle_hand(hand, round_state.get("dealer_cards", []))
                hand["status"] = "SETTLED"
                hand["result"] = outcome
                hand["payout"] = payout
                hands.append(hand)
                total_payout += payout
            entry["hands"] = hands
            self._save_player_entry(round_state, player, entry)
            payouts[player] = total_payout
            player_results[player] = [_hand_snapshot(hand) for hand in hands]
        round_state["phase"] = PHASE_ROUND_COMPLETE
        round_state["dealer_has_blackjack"] = dealer_has_blackjack
        self._payout_and_close(round_state, payouts, player_results)

    def _payout_and_close(self, round_state, payouts, player_results):
        token_id = self._token_id()
        for player, amount in payouts.items():
            if int(amount) > 0:
                runtime.token_transfer(token_id, player, int(amount))
        last_round = {
            "round_id": int(round_state.get("round_id", 0)),
            "phase": PHASE_ROUND_COMPLETE,
            "dealer_cards": list(round_state.get("dealer_cards", [])),
            "dealer_total": _hand_value(round_state.get("dealer_cards", []))["total"],
            "dealer_has_blackjack": bool(round_state.get("dealer_has_blackjack", False)),
            "player_results": player_results,
            "payouts": payouts,
        }
        self.storage["last_round"] = last_round
        self.storage["current_round"] = {}
        self.storage["bets"] = {}
        self.storage["phase"] = PHASE_ROUND_COMPLETE
        runtime.emit_event("blackjack.round_complete", last_round)

    def init(self, payload):
        owner_bytes = runtime.assume_ownership_if_unowned()
        owner = Address(owner_bytes).to_hex(checksum=False) if owner_bytes else ""
        data = json.loads(payload) if payload else {}
        token_id = str(data.get("token_id", "")).strip()
        self.storage["__name__"] = "Blackjack Dealer"
        self.storage["__description__"] = "Vegas-style blackjack table using CIP-20 chip escrow"
        self.storage["__version__"] = "1.0.0"
        self.storage["owner"] = owner
        self.storage["token_id"] = token_id
        self.storage["min_bet"] = int(data.get("min_bet", 10))
        self.storage["max_bet"] = int(data.get("max_bet", 500))
        self.storage["max_players"] = int(data.get("max_players", 7))
        self.storage["num_decks"] = int(data.get("num_decks", 6))
        self.storage["players"] = []
        self.storage["bets"] = {}
        self.storage["phase"] = PHASE_BETTING
        self.storage["round_id"] = 0
        self.storage["current_round"] = {}
        self.storage["last_round"] = {}
        self.storage["shoe"] = [_normalize_card(card) for card in data.get("shoe", _build_shoe(self.storage["num_decks"]))]
        runtime.emit_event(
            "blackjack.init",
            {
                "owner": owner,
                "token_id": token_id,
                "min_bet": int(self.storage["min_bet"]),
                "max_bet": int(self.storage["max_bet"]),
                "max_players": int(self.storage["max_players"]),
            },
        )
        return codec.encode({"status": "initialized", "owner": owner, "token_id": token_id})

    @callable_by(OWNER, decode="cbor")
    def fund_bankroll(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        amount = int(data.get("amount", 0))
        if amount <= 0:
            return {"error": "INVALID_AMOUNT"}
        runtime.token_transfer_from(self._token_id(), self.sender, runtime.get_actor_address(), amount)
        runtime.emit_event("blackjack.fund_bankroll", {"amount": amount})
        return {"status": "funded", "amount": amount}

    @callable_by(OWNER, decode="cbor")
    def seed_next_shoe(self, payload):
        if not self._require_owner():
            return {"error": "OWNER_ONLY"}
        data = payload or {}
        cards = [_normalize_card(card) for card in data.get("cards", [])]
        if not cards:
            return {"error": "EMPTY_SHOE"}
        self.storage["shoe"] = cards
        runtime.emit_event("blackjack.seed_next_shoe", {"cards": cards[:16], "count": len(cards)})
        return {"status": "seeded", "count": len(cards)}

    @public(decode="cbor")
    def join_table(self, payload):
        if self.storage.get("current_round"):
            return {"error": "ROUND_IN_PROGRESS"}
        sender = self.sender.to_hex(checksum=False)
        players = self._joined_players()
        if sender not in players:
            if len(players) >= int(self.storage.get("max_players", 7)):
                return {"error": "TABLE_FULL"}
            players.append(sender)
        self.storage["players"] = players
        runtime.emit_event("blackjack.join_table", {"player": sender})
        return {"status": "joined", "player": sender, "players": players}

    @public(decode="cbor")
    def leave_table(self, payload):
        if self.storage.get("current_round"):
            return {"error": "ROUND_IN_PROGRESS"}
        sender = self.sender.to_hex(checksum=False)
        players = [player for player in self._joined_players() if player != sender]
        self.storage["players"] = players
        bets = self._bets()
        if sender in bets:
            del bets[sender]
            self.storage["bets"] = bets
        runtime.emit_event("blackjack.leave_table", {"player": sender})
        return {"status": "left", "player": sender, "players": players}

    @public(decode="cbor")
    def place_bet(self, payload):
        if self.storage.get("current_round"):
            return {"error": "ROUND_IN_PROGRESS"}
        data = payload or {}
        sender = self.sender.to_hex(checksum=False)
        amount = int(data.get("amount", 0))
        if sender not in self._joined_players():
            return {"error": "NOT_SEATED"}
        if amount < int(self.storage.get("min_bet", 0)) or amount > int(self.storage.get("max_bet", 0)):
            return {"error": "BET_OUT_OF_RANGE"}
        bets = self._bets()
        if sender in bets:
            return {"error": "BET_ALREADY_PLACED"}
        runtime.token_transfer_from(self._token_id(), self.sender, runtime.get_actor_address(), amount)
        bets[sender] = amount
        self.storage["bets"] = bets
        self.storage["phase"] = PHASE_BETTING
        runtime.emit_event("blackjack.place_bet", {"player": sender, "amount": amount})
        return {"status": "bet_placed", "player": sender, "amount": amount}

    @callable_by(OWNER, decode="cbor")
    def start_round(self, payload):
        if self.storage.get("current_round"):
            return {"error": "ROUND_IN_PROGRESS"}
        bets = self._bets()
        if not bets:
            return {"error": "NO_BETS"}
        order = [player for player in self._joined_players() if player in bets]
        if not order:
            return {"error": "NO_SEATED_BETTORS"}
        round_id = int(self.storage.get("round_id", 0)) + 1
        self.storage["round_id"] = round_id
        round_state = {
            "round_id": round_id,
            "phase": PHASE_BETTING,
            "player_order": order,
            "players": {},
            "dealer_cards": [],
            "active_player_index": 0,
            "active_hand_index": 0,
            "insurance_pending": [],
            "dealer_has_blackjack": False,
        }
        for player in order:
            round_state["players"][player] = {
                "base_bet": int(bets[player]),
                "insurance_bet": None,
                "hands": [{"cards": [], "wager": int(bets[player]), "status": "ACTIVE", "split": False, "split_aces": False, "doubled": False}],
            }

        for player in order:
            entry = self._player_entry(round_state, player)
            entry["hands"][0]["cards"].append(self._draw_card())
            self._save_player_entry(round_state, player, entry)
        round_state["dealer_cards"].append(self._draw_card())
        for player in order:
            entry = self._player_entry(round_state, player)
            entry["hands"][0]["cards"].append(self._draw_card())
            self._save_player_entry(round_state, player, entry)
        round_state["dealer_cards"].append(self._draw_card())

        upcard_rank = _rank(round_state["dealer_cards"][0])
        self._set_current_round(round_state)
        self.storage["phase"] = PHASE_BETTING

        if upcard_rank == "A":
            round_state["phase"] = PHASE_INSURANCE
            round_state["insurance_pending"] = order
            self._set_current_round(round_state)
            self.storage["phase"] = PHASE_INSURANCE
            runtime.emit_event("blackjack.insurance_open", {"round_id": round_id, "players": order})
            return {"status": "insurance_open", "round_id": round_id}
        if upcard_rank in TEN_VALUE_RANKS:
            if _is_blackjack(round_state["dealer_cards"], False):
                payouts, player_results = self._resolve_dealer_blackjack(round_state)
                self._payout_and_close(round_state, payouts, player_results)
                return {"status": "dealer_blackjack", "round_id": round_id}
        self._begin_player_turns(round_state)
        if round_state.get("phase") != PHASE_ROUND_COMPLETE:
            self._set_current_round(round_state)
            self.storage["phase"] = round_state["phase"]
        return {"status": "round_started", "round_id": round_id, "phase": self.storage.get("phase")}

    @public(decode="cbor")
    def buy_insurance(self, payload):
        round_state = self._current_round()
        if round_state.get("phase") != PHASE_INSURANCE:
            return {"error": "INSURANCE_CLOSED"}
        data = payload or {}
        sender = self.sender.to_hex(checksum=False)
        entry = self._player_entry(round_state, sender)
        if not entry:
            return {"error": "PLAYER_NOT_IN_ROUND"}
        if entry.get("insurance_bet") is not None:
            return {"error": "INSURANCE_ALREADY_SET"}
        amount = int(data.get("amount", 0))
        max_insurance = int(entry.get("base_bet", 0)) // 2
        if amount < 0 or amount > max_insurance:
            return {"error": "INVALID_INSURANCE_AMOUNT"}
        if amount > 0:
            runtime.token_transfer_from(self._token_id(), self.sender, runtime.get_actor_address(), amount)
        entry["insurance_bet"] = amount
        self._save_player_entry(round_state, sender, entry)
        pending = [player for player in self._round_players(round_state) if self._player_entry(round_state, player).get("insurance_bet") is None]
        round_state["insurance_pending"] = pending
        if pending:
            self._set_current_round(round_state)
            runtime.emit_event("blackjack.buy_insurance", {"player": sender, "amount": amount, "pending": pending})
            return {"status": "insurance_recorded", "pending": pending}
        if _is_blackjack(round_state["dealer_cards"], False):
            payouts, player_results = self._resolve_dealer_blackjack(round_state)
            self._payout_and_close(round_state, payouts, player_results)
            return {"status": "dealer_blackjack"}
        self._begin_player_turns(round_state)
        if round_state.get("phase") != PHASE_ROUND_COMPLETE:
            self._set_current_round(round_state)
            self.storage["phase"] = round_state["phase"]
        runtime.emit_event("blackjack.buy_insurance", {"player": sender, "amount": amount, "pending": []})
        return {"status": "insurance_closed", "phase": self.storage.get("phase")}

    @public(decode="cbor")
    def act(self, payload):
        round_state = self._current_round()
        if round_state.get("phase") != PHASE_PLAYER_TURN:
            return {"error": "NOT_PLAYER_TURN"}
        data = payload or {}
        sender = self.sender.to_hex(checksum=False)
        active_player = self._active_player(round_state)
        active_player_index, active_hand_index = self._active_cursor(round_state)
        if sender != active_player:
            return {"error": "OUT_OF_TURN"}
        if int(data.get("hand_index", active_hand_index)) != active_hand_index:
            return {"error": "INVALID_HAND_INDEX"}
        action = str(data.get("action", "")).strip().lower()
        entry = self._player_entry(round_state, sender)
        hands = list(entry.get("hands", []))
        if active_hand_index >= len(hands):
            return {"error": "HAND_NOT_FOUND"}
        hand = dict(hands[active_hand_index])
        if hand.get("status") != "ACTIVE":
            return {"error": "HAND_NOT_ACTIVE"}
        cards = list(hand.get("cards", []))
        token_id = self._token_id()

        if action == "hit":
            cards.append(self._draw_card())
            hand["cards"] = cards
            total = _hand_value(cards)["total"]
            if total > 21:
                hand["status"] = "BUST"
            hands[active_hand_index] = hand
            entry["hands"] = hands
            self._save_player_entry(round_state, sender, entry)
            if hand["status"] == "BUST":
                self._advance_turn(round_state)
        elif action == "stand":
            hand["status"] = "STAND"
            hands[active_hand_index] = hand
            entry["hands"] = hands
            self._save_player_entry(round_state, sender, entry)
            self._advance_turn(round_state)
        elif action == "double":
            if len(cards) != 2:
                return {"error": "DOUBLE_REQUIRES_TWO_CARDS"}
            extra = int(hand.get("wager", 0))
            runtime.token_transfer_from(token_id, self.sender, runtime.get_actor_address(), extra)
            hand["wager"] = extra * 2
            hand["doubled"] = True
            cards.append(self._draw_card())
            hand["cards"] = cards
            hand["status"] = "BUST" if _hand_value(cards)["total"] > 21 else "STAND"
            hands[active_hand_index] = hand
            entry["hands"] = hands
            self._save_player_entry(round_state, sender, entry)
            self._advance_turn(round_state)
        elif action == "split":
            if not _can_split(cards):
                return {"error": "HAND_NOT_SPLITTABLE"}
            if len(hands) >= 4:
                return {"error": "HAND_LIMIT_REACHED"}
            is_aces = _rank(cards[0]) == "A"
            if is_aces and hand.get("split_aces"):
                return {"error": "NO_RESPLIT_ACES"}
            extra = int(hand.get("wager", 0))
            runtime.token_transfer_from(token_id, self.sender, runtime.get_actor_address(), extra)
            first = {
                "cards": [cards[0], self._draw_card()],
                "wager": extra,
                "status": "ACTIVE",
                "split": True,
                "split_aces": is_aces,
                "doubled": False,
            }
            second = {
                "cards": [cards[1], self._draw_card()],
                "wager": extra,
                "status": "ACTIVE",
                "split": True,
                "split_aces": is_aces,
                "doubled": False,
            }
            if is_aces:
                first["status"] = "STAND"
                second["status"] = "STAND"
            hands = hands[:active_hand_index] + [first, second] + hands[active_hand_index + 1 :]
            entry["hands"] = hands
            self._save_player_entry(round_state, sender, entry)
            if is_aces:
                self._advance_turn(round_state)
            else:
                self._set_active_cursor(round_state, active_player_index, active_hand_index)
        else:
            return {"error": "UNKNOWN_ACTION"}
        if round_state.get("phase") != PHASE_ROUND_COMPLETE:
            self._set_current_round(round_state)
            self.storage["phase"] = round_state["phase"]
        runtime.emit_event(
            "blackjack.act",
            {
                "player": sender,
                "action": action,
                "hand_index": active_hand_index,
                "phase": self.storage.get("phase"),
            },
        )
        return {"status": "ok", "phase": self.storage.get("phase")}

    @pure
    @public(decode="cbor")
    def get_table_state(self, payload):
        round_state = self._current_round()
        active_player = self._active_player(round_state) if round_state else ""
        response = {
            "phase": str(self.storage.get("phase", PHASE_BETTING)),
            "token_id": self._token_id(),
            "players": self._joined_players(),
            "pending_bets": self._bets(),
            "round_id": int(self.storage.get("round_id", 0)),
            "current_round": {
                "round_id": int(round_state.get("round_id", 0)),
                "phase": round_state.get("phase", ""),
                "dealer_cards": list(round_state.get("dealer_cards", [])),
                "active_player": active_player,
                "active_hand_index": int(round_state.get("active_hand_index", 0)),
                "player_order": self._round_players(round_state),
            } if round_state else {},
        }
        runtime.emit_event("get_table_state", response)
        return response

    @pure
    @public(decode="cbor")
    def get_player_state(self, payload):
        data = payload or {}
        player = Address(data.get("player") or self.sender).to_hex(checksum=False)
        round_state = self._current_round()
        entry = self._player_entry(round_state, player) if round_state else {}
        response = {
            "player": player,
            "seated": player in self._joined_players(),
            "pending_bet": int(self._bets().get(player, 0)),
            "round": {
                "insurance_bet": int(entry.get("insurance_bet", 0) or 0),
                "hands": [_hand_snapshot(hand) for hand in entry.get("hands", [])],
            } if entry else {},
        }
        runtime.emit_event("get_player_state", response)
        return response

    @pure
    @public(decode="cbor")
    def get_last_round(self, payload):
        response = self.storage.get("last_round", {})
        runtime.emit_event("get_last_round", response)
        return response
def _get_actor():
    return BlackjackDealer()


def init(payload):
    return _get_actor().init(payload)


def fund_bankroll(payload):
    return _get_actor().fund_bankroll(payload)


def seed_next_shoe(payload):
    return _get_actor().seed_next_shoe(payload)


def join_table(payload):
    return _get_actor().join_table(payload)


def leave_table(payload):
    return _get_actor().leave_table(payload)


def place_bet(payload):
    return _get_actor().place_bet(payload)


def start_round(payload):
    return _get_actor().start_round(payload)


def buy_insurance(payload):
    return _get_actor().buy_insurance(payload)


def act(payload):
    return _get_actor().act(payload)


def get_table_state(payload):
    return _get_actor().get_table_state(payload)


def get_player_state(payload):
    return _get_actor().get_player_state(payload)


def get_last_round(payload):
    return _get_actor().get_last_round(payload)
