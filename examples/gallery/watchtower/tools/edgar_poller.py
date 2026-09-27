#!/usr/bin/env python3
"""Fixture-first SEC Form 4 normalizer for the Watchtower demo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "form4"
SEC_RECENT_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"


@dataclass(frozen=True)
class Form4Event:
    accession: str
    issuer_cik: str
    ticker: str
    issuer_name: str
    owner: str
    relationship: str
    transaction_code: str
    acquired_disposed: str
    shares: int
    price: float
    transaction_date: str
    filed_at: str

    @property
    def side(self) -> str:
        if self.transaction_code == "P" and self.acquired_disposed == "A":
            return "buy"
        if self.transaction_code == "S" and self.acquired_disposed == "D":
            return "sell"
        return "other"

    @property
    def notional_usd(self) -> float:
        return round(float(self.shares) * float(self.price), 2)

    def to_dict(self) -> dict:
        return {
            "schema": "cowboy.watchtower.form4.v1",
            "accession": self.accession,
            "issuer_cik": self.issuer_cik,
            "ticker": self.ticker,
            "issuer_name": self.issuer_name,
            "owner": self.owner,
            "relationship": self.relationship,
            "transaction_code": self.transaction_code,
            "acquired_disposed": self.acquired_disposed,
            "shares": self.shares,
            "price": self.price,
            "transaction_date": self.transaction_date,
            "filed_at": self.filed_at,
            "side": self.side,
            "notional_usd": self.notional_usd,
        }


def normalize_form4(raw: dict) -> Form4Event:
    required = [
        "accession",
        "issuer_cik",
        "ticker",
        "issuer_name",
        "owner",
        "relationship",
        "transaction_code",
        "acquired_disposed",
        "shares",
        "price",
        "transaction_date",
        "filed_at",
    ]
    missing = [key for key in required if raw.get(key) in (None, "")]
    if missing:
        raise ValueError(f"missing Form 4 fields: {', '.join(missing)}")
    return Form4Event(
        accession=str(raw["accession"]),
        issuer_cik=str(raw["issuer_cik"]).zfill(10),
        ticker=str(raw["ticker"]).upper(),
        issuer_name=str(raw["issuer_name"]),
        owner=str(raw["owner"]),
        relationship=str(raw["relationship"]),
        transaction_code=str(raw["transaction_code"]).upper(),
        acquired_disposed=str(raw["acquired_disposed"]).upper(),
        shares=int(raw["shares"]),
        price=float(raw["price"]),
        transaction_date=str(raw["transaction_date"]),
        filed_at=str(raw["filed_at"]),
    )


def load_fixture_events(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> list[dict]:
    events = []
    for path in sorted(fixture_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            events.append(normalize_form4(json.load(fh)).to_dict())
    return events


def fetch_live_recent(ciks: Iterable[str], user_agent: str, limit: int = 10) -> list[dict]:
    events: list[dict] = []
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", user_agent)]
    for cik in ciks:
        padded = "".join(ch for ch in cik if ch.isdigit()).zfill(10)
        with opener.open(SEC_RECENT_SUBMISSIONS.format(cik=padded), timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        issuer_name = data.get("name", padded)
        tickers = data.get("tickers") or [padded]
        ticker = str(tickers[0]).upper()
        for idx, form in enumerate(forms):
            if form != "4":
                continue
            accession = accessions[idx] if idx < len(accessions) else f"{padded}-{idx}"
            filed_at = filing_dates[idx] if idx < len(filing_dates) else ""
            events.append(
                normalize_form4(
                    {
                        "accession": accession,
                        "issuer_cik": padded,
                        "ticker": ticker,
                        "issuer_name": issuer_name,
                        "owner": "live-edgar",
                        "relationship": "unknown",
                        "transaction_code": "P",
                        "acquired_disposed": "A",
                        "shares": 1,
                        "price": 0.0,
                        "transaction_date": filed_at,
                        "filed_at": filed_at,
                    }
                ).to_dict()
            )
            if len(events) >= limit:
                return events
    return events


def cluster_buy_signals(events: Iterable[dict], min_buys: int = 2) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for event in events:
        if event.get("side") != "buy":
            continue
        grouped.setdefault(str(event.get("ticker", "")).upper(), []).append(event)
    signals = []
    for ticker, buys in sorted(grouped.items()):
        if len(buys) < min_buys:
            continue
        signals.append(
            {
                "schema": "cowboy.watchtower.cluster.v1",
                "action": "BUY",
                "ticker": ticker,
                "reason": f"{len(buys)} insider buys in fixture window",
                "event_count": len(buys),
                "accessions": [event["accession"] for event in buys],
                "notional_usd": round(sum(float(event["notional_usd"]) for event in buys), 2),
                "events": buys,
            }
        )
    return signals


def write_jsonl(rows: Iterable[dict], out) -> None:
    for row in rows:
        out.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--live-edgar", action="store_true")
    parser.add_argument("--cik", action="append", default=["0000320193"])
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT", "cowboy-watchtower-demo admin@example.com"))
    parser.add_argument("--clusters", action="store_true", help="emit clustered BUY signals instead of raw Form 4 rows")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    events = (
        fetch_live_recent(args.cik, args.user_agent)
        if args.live_edgar
        else load_fixture_events(args.fixtures)
    )
    rows = cluster_buy_signals(events) if args.clusters else events
    if args.output:
        with args.output.open("w", encoding="utf-8") as fh:
            write_jsonl(rows, fh)
    else:
        write_jsonl(rows, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
