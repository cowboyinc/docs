# /// script
# requires-python = ">=3.13"
# dependencies = ["cbor2==6.1.4"]
# ///
"""Explicit paid comparison through signed, request-specific Canyon receipts."""
import argparse
from pathlib import Path
import sys

# Standalone runs start with only tests/ on sys.path; add tools/ for the client.
_TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from browserbase_client import add_connection_arguments, invoke
from test_browserbase_comparison import run_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--with-agent", action="store_true", help="also start and complete one agent run")
    args = parser.parse_args()
    key = args.key_file.read_text().strip()
    if not key:
        raise ValueError("vendor key file is empty")
    run_comparison(key, lambda handler, payload: invoke(args, handler, payload), args.with_agent)


if __name__ == "__main__":
    main()
