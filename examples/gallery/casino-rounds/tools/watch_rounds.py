#!/usr/bin/env python3

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_RPC_URL = "http://rpc-01.mesa.cowboylabs.net:4000"
DEFAULT_STATE_FILE = Path(
    os.environ.get(
        "STATE_FILE",
        str(Path(os.environ.get("TMPDIR", "/tmp")) / "cowboy-casino-rounds-deploy-state.env"),
    )
)
DEFAULT_REPO_CONFIG_FILE = Path(__file__).resolve().parents[4] / ".cowboy" / "config.json"
DEFAULT_EXAMPLES_CONFIG_FILE = Path(__file__).resolve().parents[2] / ".cowboy" / "config.json"
DEFAULT_LOCAL_COWBOY = Path(__file__).resolve().parents[4] / "node" / "target" / "release" / "cowboy"


def decode_hex_ascii(hex_text):
    return bytes.fromhex(hex_text).decode("utf-8")


class CborReader:
    def __init__(self, raw):
        self.raw = raw
        self.pos = 0

    def read(self):
        initial = self._read_u8()
        major = initial >> 5
        minor = initial & 0x1F
        if major == 0:
            return self._read_length(minor)
        if major == 1:
            return -1 - self._read_length(minor)
        if major == 3:
            length = self._read_length(minor)
            data = self._read_exact(length)
            return data.decode("utf-8")
        if major == 4:
            length = self._read_length(minor)
            return [self.read() for _ in range(length)]
        if major == 5:
            length = self._read_length(minor)
            out = {}
            for _ in range(length):
                key = self.read()
                out[key] = self.read()
            return out
        raise ValueError(f"unsupported cbor major type {major}")

    def _read_length(self, minor):
        if minor < 24:
            return minor
        if minor == 24:
            return self._read_u8()
        if minor == 25:
            return int.from_bytes(self._read_exact(2), "big")
        if minor == 26:
            return int.from_bytes(self._read_exact(4), "big")
        if minor == 27:
            return int.from_bytes(self._read_exact(8), "big")
        raise ValueError(f"unsupported cbor length {minor}")

    def _read_u8(self):
        if self.pos >= len(self.raw):
            raise ValueError("unexpected end of cbor")
        value = self.raw[self.pos]
        self.pos += 1
        return value

    def _read_exact(self, length):
        end = self.pos + length
        if end > len(self.raw):
            raise ValueError("unexpected end of cbor")
        data = self.raw[self.pos:end]
        self.pos = end
        return data


def decode_cbor_hex(hex_text):
    if not hex_text:
        return None
    return CborReader(bytes.fromhex(hex_text)).read()


def clean_hex_addr(addr):
    return addr[2:].lower() if addr.startswith("0x") else addr.lower()


def load_state_file(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def resolve_config_rpc_url(path):
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    active = data.get("active")
    envs = data.get("environments", {})
    if not active or not isinstance(envs.get(active), dict):
        return ""
    return str(envs[active].get("rpc_url") or "")


def find_cowboy_binary():
    override = os.getenv("COWBOY", "")
    if override and shutil.which(override):
        return override
    cowboy_on_path = shutil.which("cowboy")
    if cowboy_on_path:
        return cowboy_on_path
    if DEFAULT_LOCAL_COWBOY.exists():
        return str(DEFAULT_LOCAL_COWBOY)
    raise RuntimeError("cowboy CLI not found; set COWBOY or add cowboy to PATH")


def resolve_rpc_url(cli_rpc_url, state, config_paths):
    if cli_rpc_url:
        return cli_rpc_url
    if state.get("RPC_URL"):
        return state["RPC_URL"]
    for path in config_paths:
        rpc_url = resolve_config_rpc_url(path)
        if rpc_url:
            return rpc_url
    return DEFAULT_RPC_URL


def run_cowboy(rpc_url, actor):
    cowboy_bin = find_cowboy_binary()
    cmd = [
        cowboy_bin,
        "--indexer-url",
        rpc_url,
        "actor",
        "get",
        "--address",
        actor,
        "--raw",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "cowboy actor get failed")
    text = proc.stdout
    start = text.find("{")
    if start < 0:
        raise RuntimeError("failed to parse actor json")
    return json.loads(text[start:])


def parse_storage(storage):
    decoded = {}
    for key_hex, value_hex in storage.items():
        try:
            key = decode_hex_ascii(key_hex)
        except Exception:
            continue

        if key in {
            "active_round_id",
            "round_counter",
            "round_duration_blocks",
            "vault_balance",
            "auto_enabled",
            "auto_restart_delay_blocks",
            "fee_bps",
            "max_recent_rounds",
            "max_detailed_rounds",
            "pruned_before_round_id",
        }:
            decoded[key] = int(decode_cbor_hex(value_hex) or 0)
            continue

        if key in {"feed_actor", "self_actor", "token_id"}:
            decoded[key] = str(decode_cbor_hex(value_hex) or "")
            continue

        if not key.startswith("round:"):
            continue

        try:
            _, round_part, suffix = key.split(":", 2)
            round_id = int(round_part)
        except ValueError:
            continue

        round_entry = decoded.setdefault("rounds", {}).setdefault(round_id, {})
        if suffix in {"state", "detail_level"}:
            round_entry[suffix] = str(decode_cbor_hex(value_hex) or "")
        elif suffix in {
            "open_height",
            "close_height",
            "resolved_height",
            "resolved_price",
            "total_pot",
            "bet_count",
            "winner_count",
            "winning_delta",
        }:
            round_entry[suffix] = int(decode_cbor_hex(value_hex) or 0)
        elif suffix == "bettors":
            round_entry[suffix] = decode_cbor_hex(value_hex)
        elif suffix == "payouts":
            payouts = {}
            for item in decode_cbor_hex(value_hex) or []:
                if isinstance(item, dict) and item.get("bettor"):
                    payouts[str(item["bettor"])] = int(item.get("amount", 0))
            round_entry["payouts"] = payouts
        elif suffix.startswith("bet:"):
            bettor = suffix.split(":", 1)[1]
            round_entry.setdefault("bets", {})[bettor] = decode_cbor_hex(value_hex)
        elif suffix.startswith("payout:"):
            bettor = suffix.split(":", 1)[1]
            round_entry.setdefault("payouts", {})[bettor] = int(decode_cbor_hex(value_hex) or 0)
    return decoded


def decode_event_data(hex_text):
    if not hex_text:
        return {}
    raw = bytes.fromhex(hex_text)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"raw_hex": hex_text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def normalize_event_topic(topic):
    topic = str(topic or "")
    aliases = {
        "casino.round_opened": "round_opened",
        "casino.bet_placed": "bet_placed",
        "casino.round_resolved": "round_settled",
    }
    return aliases.get(topic, topic)


def parse_actor_logs(raw):
    parsed = []
    for event in raw.get("events", []):
        parsed.append(
            {
                "block_height": int(event.get("block_height", 0) or 0),
                "tx_hash": str(event.get("tx_hash", "")),
                "topic": normalize_event_topic(event.get("topic", "")),
                "payload": decode_event_data(event.get("data", "")),
            }
        )
    return parsed


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_actor_logs(rpc_url, actor, page_size=1000):
    offset = 0
    events = []
    total = None
    while total is None or offset < total:
        url = f"{rpc_url}/actor/0x{actor}/logs?offset={offset}&limit={page_size}"
        payload = fetch_json(url)
        batch = payload.get("events", [])
        events.extend(batch)
        total = int(payload.get("total", len(events)))
        if not batch:
            break
        offset += len(batch)
    return {"events": events}


def ensure_round(rounds, round_id):
    return rounds.setdefault(round_id, {"detail_level": "event"})


def merge_round_history(state, log_events):
    merged = copy.deepcopy(state)
    rounds = merged.setdefault("rounds", {})

    for event in log_events:
        payload = event.get("payload", {})
        round_id = int(payload.get("round_id", 0) or 0)
        if round_id <= 0:
            continue
        round_entry = ensure_round(rounds, round_id)
        round_entry.setdefault("detail_level", "event")
        topic = event.get("topic", "")

        if topic == "round_opened":
            round_entry["state"] = "open"
            round_entry["open_height"] = int(payload.get("open_height", event.get("block_height", 0)) or 0)
            round_entry["close_height"] = int(payload.get("close_height", 0) or 0)
            continue

        if topic == "bet_placed":
            bettor = str(payload.get("bettor", ""))
            if bettor:
                round_entry.setdefault("bets", {})[bettor] = {
                    "guess": int(payload.get("guess", 0) or 0),
                    "stake": int(payload.get("stake", 0) or 0),
                    "net_stake": int(payload.get("net_stake", 0) or 0),
                }
            round_entry["bet_count"] = max(
                int(round_entry.get("bet_count", 0) or 0),
                int(payload.get("bet_count_after", round_entry.get("bet_count", 0)) or 0),
            )
            round_entry["total_pot"] = max(
                int(round_entry.get("total_pot", 0) or 0),
                int(payload.get("pot_after", round_entry.get("total_pot", 0)) or 0),
            )
            continue

        if topic == "round_settled":
            round_entry["state"] = "resolved"
            round_entry["resolved_height"] = int(payload.get("resolved_height", event.get("block_height", 0)) or 0)
            round_entry["resolved_price"] = int(payload.get("resolved_price", payload.get("price", -1)) or 0)
            round_entry["total_pot"] = int(payload.get("total_pot", round_entry.get("total_pot", 0)) or 0)
            round_entry["winner_count"] = int(payload.get("winner_count", round_entry.get("winner_count", 0)) or 0)
            round_entry["winning_delta"] = int(payload.get("winning_delta", round_entry.get("winning_delta", -1)) or -1)
            continue

        if topic == "payout_issued":
            winner = str(payload.get("winner", payload.get("bettor", "")))
            if winner:
                round_entry.setdefault("payouts", {})[winner] = int(payload.get("payout", payload.get("amount", 0)) or 0)
            continue

        if topic == "round_pruned":
            if payload.get("pruned_detail"):
                round_entry["detail_level"] = "summary"
                round_entry.pop("bets", None)
            if payload.get("pruned_summary"):
                round_entry["detail_level"] = "pruned"
                round_entry["state"] = "pruned"

    return merged


def summarize_round(round_id, data):
    price = data.get("resolved_price")
    winners = data.get("payouts", {})
    if data.get("state") == "pruned":
        return f"round {round_id} pruned"
    if data.get("state") == "resolved":
        winner_summary = ", ".join(
            f"{short_addr(addr)}={amount}" for addr, amount in sorted(winners.items())
        ) or "none"
        return (
            f"round {round_id} resolved"
            f" price={price}"
            f" pot={data.get('total_pot', 0)}"
            f" winners={winner_summary}"
        )
    return (
        f"round {round_id} {data.get('state', 'unknown')}"
        f" open={data.get('open_height', '?')}"
        f" close={data.get('close_height', '?')}"
    )


def short_addr(addr):
    return f"{addr[:6]}..{addr[-4:]}"


class Ansi:
    RESET = "\033[0m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"


def use_color(enabled):
    return enabled and sys.stdout.isatty()


def colorize(enabled, color, text):
    if not enabled:
        return text
    return f"{color}{text}{Ansi.RESET}"


def label(enabled, name):
    palette = {
        "tick": Ansi.DIM,
        "baseline": Ansi.DIM,
        "new": Ansi.BLUE,
        "state": Ansi.CYAN,
        "bet": Ansi.YELLOW,
        "win": Ansi.GREEN,
        "balances": Ansi.MAGENTA,
        "prune": Ansi.RED,
        "error": Ansi.RED,
    }
    return colorize(enabled, palette.get(name, Ansi.BOLD), f"[{name}]")


def clear_status_line(enabled):
    if enabled:
        print("\r\033[2K", end="", flush=True)


def write_status_line(enabled, text):
    if enabled:
        print(f"\r\033[2K{text}", end="", flush=True)
    else:
        print(text)


def status_summary(current):
    active_round = current.get("active_round_id", 0)
    round_counter = current.get("round_counter", 0)
    vault_balance = current.get("vault_balance", 0)
    auto_enabled = bool(current.get("auto_enabled", 0))
    round_state = current.get("rounds", {}).get(active_round, {}) if active_round else {}
    close_height = round_state.get("close_height", "-")
    bet_count = round_state.get("bet_count", 0) if round_state else 0
    total_pot = round_state.get("total_pot", 0) if round_state else 0
    return (
        f"active_round={active_round} "
        f"state={round_state.get('state', 'idle') if round_state else 'idle'} "
        f"close={close_height} "
        f"bets={bet_count} "
        f"pot={total_pot} "
        f"vault={vault_balance} "
        f"rounds={round_counter} "
        f"auto={auto_enabled}"
    )


def print_round_diff(previous, current, colors_enabled, status_line_enabled):
    previous_rounds = previous.get("rounds", {})
    current_rounds = current.get("rounds", {})

    for round_id in sorted(current_rounds):
        prev_round = previous_rounds.get(round_id, {})
        curr_round = current_rounds[round_id]

        if not prev_round:
            clear_status_line(status_line_enabled)
            print(f"{label(colors_enabled, 'new')} {summarize_round(round_id, curr_round)}")

        prev_bets = prev_round.get("bets", {})
        curr_bets = curr_round.get("bets", {})
        for bettor, bet in sorted(curr_bets.items()):
            if bettor in prev_bets:
                continue
            clear_status_line(status_line_enabled)
            print(
                f"{label(colors_enabled, 'bet')}"
                f" round={round_id}"
                f" bettor={short_addr(bettor)}"
                f" guess={bet.get('guess')}"
                f" stake={bet.get('stake')}"
                f" net={bet.get('net_stake')}"
            )

        if curr_round.get("state") != prev_round.get("state"):
            clear_status_line(status_line_enabled)
            print(f"{label(colors_enabled, 'state')} {summarize_round(round_id, curr_round)}")

        prev_payouts = prev_round.get("payouts", {})
        curr_payouts = curr_round.get("payouts", {})
        for bettor, amount in sorted(curr_payouts.items()):
            if prev_payouts.get(bettor) == amount:
                continue
            clear_status_line(status_line_enabled)
            print(
                f"{label(colors_enabled, 'win')}"
                f" round={round_id}"
                f" winner={short_addr(bettor)}"
                f" payout={amount}"
                f" price={curr_round.get('resolved_price')}"
                f" delta={curr_round.get('winning_delta')}"
            )


def print_log_diff(previous_count, current_events, current_state, colors_enabled, status_line_enabled):
    for event in current_events[previous_count:]:
        topic = event.get("topic", "")
        payload = event.get("payload", {})
        round_id = int(payload.get("round_id", 0) or 0)
        round_data = current_state.get("rounds", {}).get(round_id, {})

        if topic == "round_opened":
            clear_status_line(status_line_enabled)
            print(f"{label(colors_enabled, 'new')} {summarize_round(round_id, round_data)}")
            continue

        if topic == "bet_placed":
            clear_status_line(status_line_enabled)
            print(
                f"{label(colors_enabled, 'bet')}"
                f" round={round_id}"
                f" bettor={short_addr(str(payload.get('bettor', '')))}"
                f" guess={payload.get('guess')}"
                f" stake={payload.get('stake')}"
                f" net={payload.get('net_stake')}"
            )
            continue

        if topic == "round_settled":
            clear_status_line(status_line_enabled)
            print(f"{label(colors_enabled, 'state')} {summarize_round(round_id, round_data)}")
            continue

        if topic == "payout_issued":
            clear_status_line(status_line_enabled)
            winner = str(payload.get("winner", payload.get("bettor", "")))
            print(
                f"{label(colors_enabled, 'win')}"
                f" round={round_id}"
                f" winner={short_addr(winner)}"
                f" payout={payload.get('payout', payload.get('amount'))}"
                f" price={round_data.get('resolved_price')}"
                f" delta={round_data.get('winning_delta')}"
            )
            continue

        if topic == "round_pruned":
            clear_status_line(status_line_enabled)
            print(
                f"{label(colors_enabled, 'prune')}"
                f" round={round_id}"
                f" pruned_detail={bool(payload.get('pruned_detail'))}"
                f" pruned_summary={bool(payload.get('pruned_summary'))}"
            )


def print_balances(rpc_url, token_id, actors, colors_enabled, status_line_enabled):
    items = []
    for actor_label, address in actors:
        url = f"{rpc_url}/token/0x{token_id}/balance/0x{address}"
        proc = subprocess.run(["curl", "-sf", url], capture_output=True, text=True)
        if proc.returncode != 0:
            items.append(f"{actor_label}=ERR")
            continue
        balance = json.loads(proc.stdout)["balance"]
        items.append(f"{actor_label}={balance}")
    clear_status_line(status_line_enabled)
    print(f"{label(colors_enabled, 'balances')} " + " ".join(items))


def build_actor_list(state):
    actors = [("casino", clean_hex_addr(state["CASINO_ADDR"]))]
    for idx in range(1, 32):
        key = f"BETTOR_ADDR_{idx}"
        if key not in state:
            break
        actors.append((f"bettor{idx}", clean_hex_addr(state[key])))
    return actors


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Watch casino rounds, bets, and wins.")
    parser.add_argument("--rpc-url")
    parser.add_argument("--casino", help="Casino actor address")
    parser.add_argument("--token-id", help="Token id for balance reporting")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--config-file", action="append", default=None)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--show-balances", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--no-status-line", action="store_true")
    args = parser.parse_args()

    state = load_state_file(Path(args.state_file))
    config_paths = [Path(value) for value in (args.config_file or [])]
    if not config_paths:
        config_paths = [DEFAULT_REPO_CONFIG_FILE, DEFAULT_EXAMPLES_CONFIG_FILE]
    rpc_url = resolve_rpc_url(args.rpc_url, state, config_paths)
    casino = clean_hex_addr(args.casino or state.get("CASINO_ADDR", ""))
    token_id = clean_hex_addr(args.token_id or state.get("TOKEN_ID", ""))
    if not casino:
        print("missing casino address; pass --casino or provide --state-file", file=sys.stderr)
        raise SystemExit(1)

    actors = build_actor_list(state)
    previous = {}
    previous_log_count = 0
    logs_available = None
    colors_enabled = use_color(not args.no_color and os.getenv("NO_COLOR") is None)
    print(f"watching casino=0x{casino} rpc={rpc_url} interval={args.interval}s")
    if token_id:
        print(f"token=0x{token_id}")
    status_line_enabled = sys.stdout.isatty() and not args.no_status_line

    while True:
        try:
            raw = run_cowboy(rpc_url, casino)
            storage_state = parse_storage(raw.get("storage", {}))
            log_events = None
            if logs_available is not False:
                try:
                    log_events = parse_actor_logs(fetch_actor_logs(rpc_url, casino))
                    logs_available = True
                except Exception:
                    if logs_available is None:
                        logs_available = False
            current = merge_round_history(storage_state, log_events or []) if log_events is not None else storage_state
            header = f"{label(colors_enabled, 'tick')} {status_summary(current)}"
            write_status_line(status_line_enabled, header)
            if previous:
                if log_events is not None:
                    print_log_diff(previous_log_count, log_events, current, colors_enabled, status_line_enabled)
                else:
                    print_round_diff(previous, current, colors_enabled, status_line_enabled)
            else:
                clear_status_line(status_line_enabled)
                print(f"{label(colors_enabled, 'baseline')} captured current casino state; streaming new changes from here")
            if args.show_balances and token_id:
                print_balances(rpc_url, token_id, actors, colors_enabled, status_line_enabled)
            previous = current
            if log_events is not None:
                previous_log_count = len(log_events)
        except KeyboardInterrupt:
            clear_status_line(status_line_enabled)
            print("stopped")
            raise SystemExit(0)
        except Exception as exc:
            clear_status_line(status_line_enabled)
            print(f"{label(colors_enabled, 'error')} {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
