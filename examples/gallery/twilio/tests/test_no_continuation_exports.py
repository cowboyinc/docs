"""Negative test: the sensitive initial-continuation handlers must NOT be
reachable as PVM entrypoints.

The PVM dispatches a handler by looking up a MODULE-LEVEL function of that name
(`run_simulation(... with_entrypoint(handler))`), and `@actor` skips permission
wrapping for `@runner.continuation` methods. So if the initial continuation
(`send_sms` / `verify_and_ack` / `tenant_answer`) were exported at module level,
anyone could dispatch it directly — bypassing the authenticated `post_answer` /
`relay_event` / `tenant_verified` gates and releasing the hirer's key for
attacker-chosen content.

This test parses the actor sources (no cowboy_sdk import needed) and asserts the
initial-continuation names are NOT module-level `def`s, while the gated launchers
and the required resume/on_runner_result plumbing ARE.

Run: python3 test_no_continuation_exports.py
"""

import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent


def module_level_defs(path):
    tree = ast.parse(path.read_text())
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


CASES = {
    "actors/twilio/main.py": {
        # Must NOT be dispatchable entrypoints (initial continuations):
        "forbidden": {"tenant_answer"},
        # Must remain exported (auth entry points / gated launchers / plumbing).
        # handle_timer is the node's DEFAULT_TIMER_HANDLER — the funded self-timer
        # tenant_verified schedules fires into it (replaces the old self-send
        # mailbox handle_message; D36 / COW-3894).
        "required": {
            "twilio_http", "tenant_verified", "tenant_rejected",
            "handle_timer", "run_agent",
            "tenant_answer__resume", "on_runner_result",
        },
    },
    "actors/relay/main.py": {
        "forbidden": {"send_sms", "verify_and_ack"},
        "required": {
            "relay_event", "post_answer", "handle_timer",
            "run_verify", "run_send",
            "send_sms__resume", "verify_and_ack__resume", "on_runner_result",
        },
    },
}

failures = []
for rel, spec in CASES.items():
    defs = module_level_defs(EXAMPLE_DIR / rel)
    leaked = spec["forbidden"] & defs
    if leaked:
        failures.append(f"{rel}: initial continuation(s) exported as entrypoints: {sorted(leaked)}")
    missing = spec["required"] - defs
    if missing:
        failures.append(f"{rel}: required entrypoint(s) missing: {sorted(missing)}")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("OK: no sensitive initial-continuation handler is a dispatchable entrypoint;")
print("OK: all authenticated entry points + gated launchers + resume plumbing present.")
