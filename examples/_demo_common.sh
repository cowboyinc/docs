#!/usr/bin/env bash

set -euo pipefail

cat >&2 <<'EOF'
examples/_demo_common.sh has been retired.

Use examples/_demo_lib.sh and write the lesson flow explicitly:

  source "$EXAMPLE_DIR/../../_demo_lib.sh"
  demo_begin "Title" "Intro"
  demo_step "Deploy actor" "Why this matters."
  actor="$(demo_deploy_actor "actor.py" --why "..." --expect "...")"
  demo_call "$actor" "handler" '{}' --why "..." --expect "..."
  demo_done

The old env-var workflow runner is intentionally not supported.
EOF

exit 1
