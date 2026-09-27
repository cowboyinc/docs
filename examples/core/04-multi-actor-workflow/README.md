# Multi-Actor Workflow Example

This multi-actor example shows the core Cowboy model with three actors:

- `board` (`actors/board/main.py`) publishes jobs and accepts submissions.
- `validator` (`actors/validator/main.py`) validates submitted work.
- `ledger` (`actors/ledger/main.py`) records the final settlement state.

## Message Flow

1. A creator calls `create_job` on the job board.
2. A worker calls `submit_work` on the job board.
3. The job board forwards the submission to the `validator` actor.
4. The validator checks the required keyword and forwards the verdict to the `ledger` actor.
5. The settlement ledger records the terminal state and notifies the job board.

## Why This Example Exists

- Teaches multi-actor composition without runner complexity.
- Shows actor-to-actor message flow and terminal state recording.
- Demonstrates a clean split between request, validation, and settlement.

## Files

- `cowboy.toml` - project manifest; targets are discovered from `actors/<name>/main.py`
- `actors/board/main.py` - `JobBoard`
- `actors/validator/main.py` - `JobValidator`
- `actors/ledger/main.py` - `SettlementLedger`
- `tests/test_board.py`, `tests/test_validator.py`, `tests/test_ledger.py` - `SimulatedChain` tests, run with `uv run pytest`
- `demo.sh` - the network walkthrough

## Core Handlers

- `JobBoard.create_job`
- `JobBoard.submit_work`
- `JobValidator.validate_submission`
- `SettlementLedger.settle_submission`
- `SettlementLedger.get_settlement`

## Demo Shape

The demo script deploys all three targets, wires them together with `configure`, creates one job, submits one response, and reads back the settlement.

## Simulate Locally

The tests drive each actor through `cowboy_sdk.testing.SimulatedChain`, an
in-memory host: state, events, sender and block height, no node. Actor-to-actor
messages land in the mock host's send log, so each test asserts on what the
actor forwarded and to whom.

```bash
uv run pytest
```

`cowboy test` compiles a target the way a deploy does and runs one handler on
the simulated PVM. Payloads and expected outputs are the handler's wire bytes,
hex-encoded; this call is `create_job` on a fresh `board` actor and expects the
CBOR-encoded `job_created` reply for job 1:

```bash
cowboy test --target board --handler create_job \
  --payload 0xa3657469746c657753756d6d6172697a652076616c696461746f72206c6f676c7265776172645f756e697473027072657175697265645f6b6579776f72646473686970 \
  --expect 0xa3657469746c657753756d6d6172697a652076616c696461746f72206c6f67666a6f625f696401667374617475736b6a6f625f63726561746564
```

`cowboy actor check <target>` validates a target offline. `cowboy test` and
`cowboy dev` need `cowboy-dev-runner` on `PATH` or at `COWBOY_DEV_RUNNER`
(`cargo build --release -p cowboy-dev-runner` in a node checkout).

## Run On A Network

```bash
./demo.sh
```

`demo.sh` deploys the targets with `cowboy actor deploy board`, `validator` and
`ledger`. A project deploy checks the node's actor runtime profile, so use the
CLI release that matches the network you target.

## Caveats

- Validation is intentionally simple: the required keyword must appear in the submitted text.
- The settlement ledger is the final source of truth for the settled state.
- This is a teaching example, not a production marketplace.
