import json


SUITS = ("S", "H", "D", "C")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K")
TEN_VALUE_RANKS = {"T", "J", "Q", "K"}


def _rank(card):
    return str(card or "").strip().upper()[:1]


def normalize_card(card):
    text = str(card or "").strip().upper()
    if len(text) != 2 or text[0] not in RANKS or text[1] not in SUITS:
        raise ValueError(f"invalid card: {card}")
    return text


def build_shoe(num_decks=6):
    shoe = []
    for _ in range(int(num_decks)):
        for rank in RANKS:
            for suit in SUITS:
                shoe.append(f"{rank}{suit}")
    return shoe


def hand_value(cards):
    total = 0
    aces = 0
    for card in cards or []:
        rank = _rank(card)
        if rank == "A":
            aces += 1
            total += 11
        elif rank in TEN_VALUE_RANKS:
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return {"total": total, "soft": aces > 0}


def is_blackjack(cards, from_split=False):
    value = hand_value(cards)
    return len(cards or []) == 2 and value["total"] == 21 and not bool(from_split)


def can_split(cards):
    if len(cards or []) != 2:
        return False
    return _rank(cards[0]) == _rank(cards[1])


def dealer_should_hit(cards):
    value = hand_value(cards)
    return value["total"] < 17


def settle_hand(wager, player_cards, dealer_cards, from_split=False):
    wager = int(wager)
    player_value = hand_value(player_cards)
    dealer_value = hand_value(dealer_cards)
    player_blackjack = is_blackjack(player_cards, from_split=from_split)
    dealer_blackjack = is_blackjack(dealer_cards, from_split=False)

    if dealer_blackjack:
        if player_blackjack:
            return {"outcome": "push", "payout": wager}
        return {"outcome": "lose", "payout": 0}
    if player_blackjack:
        return {"outcome": "blackjack", "payout": (wager * 5) // 2}
    if player_value["total"] > 21:
        return {"outcome": "bust", "payout": 0}
    if dealer_value["total"] > 21:
        return {"outcome": "win", "payout": wager * 2}
    if player_value["total"] > dealer_value["total"]:
        return {"outcome": "win", "payout": wager * 2}
    if player_value["total"] == dealer_value["total"]:
        return {"outcome": "push", "payout": wager}
    return {"outcome": "lose", "payout": 0}


def settle_insurance(insurance_amount, dealer_has_blackjack):
    insurance_amount = int(insurance_amount or 0)
    if insurance_amount <= 0:
        return 0
    if dealer_has_blackjack:
        return insurance_amount * 3
    return 0


def pretty_state(state):
    return json.dumps(state, sort_keys=True, indent=2)
