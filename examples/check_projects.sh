#!/usr/bin/env bash
# Offline checks over every example that is a `cowboy init` project (a
# directory with cowboy.toml under the tracks listed in PROJECT_TRACKS).
#
#   ./check_projects.sh            # cowboy actor check <target> for every target
#   ./check_projects.sh --pytest   # additionally run each project's SimulatedChain tests
#
# `--pytest` needs cowboy_sdk importable or COWBOY_SDK_PATH set (see
# _sdk_path.py) and pytest in $PYTHON (default python3; e.g.
# PYTHON="uv run -q --no-project --with pytest python"); `actor check` needs
# only the cowboy CLI ($COWBOY_BIN, default cowboy).
set -euo pipefail

EXAMPLES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_TRACKS=(core gallery)
RUN_PYTEST=0
[[ "${1:-}" == "--pytest" ]] && RUN_PYTEST=1

COWBOY_BIN="${COWBOY_BIN:-cowboy}"
PYTHON="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
# An unmatched glob must expand to nothing, never to the literal pattern
# (`cowboy actor check '*'`).
shopt -s nullglob

failed=0
for track in "${PROJECT_TRACKS[@]}"; do
  for manifest in "$EXAMPLES_DIR/$track"/*/cowboy.toml; do
    [[ -f "$manifest" ]] || continue
    project="$(dirname "$manifest")"
    key="${project#"$EXAMPLES_DIR/"}"
    targets=("$project"/actors/*/main.py)
    # A project with no actor target is an off-chain function project (schema
    # v2 `[offchain]`, e.g. gallery/venue-signer). The pinned CLI parses schema
    # v1 only; those projects carry their own locked pytest workflow, and
    # test_examples_structure.py checks the manifest shape.
    if (( ${#targets[@]} == 0 )); then
      echo "skip  $key :: no actor targets (off-chain project)"
      continue
    fi
    for main in "${targets[@]}"; do
      target="$(basename "$(dirname "$main")")"
      if (cd "$project" && "$COWBOY_BIN" actor check "$target" >/dev/null 2>&1); then
        echo "ok    $key :: $target"
      else
        echo "FAIL  $key :: $target"
        (cd "$project" && "$COWBOY_BIN" actor check "$target" 2>&1 | sed 's/^/      /') || true
        failed=1
      fi
    done
    if [[ "$RUN_PYTEST" == "1" ]]; then
      if (cd "$project" && $PYTHON -m pytest -q -p no:cacheprovider >/dev/null 2>&1); then
        echo "ok    $key :: pytest"
      else
        echo "FAIL  $key :: pytest"
        (cd "$project" && $PYTHON -m pytest -q -p no:cacheprovider 2>&1 | tail -30 | sed 's/^/      /') || true
        failed=1
      fi
    fi
  done
done
exit "$failed"
