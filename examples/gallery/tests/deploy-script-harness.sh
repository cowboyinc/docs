#!/usr/bin/env bash
# deploy-script-harness.sh — executable contract test for the gallery deploy
# scripts, no network or chain required.
#
# Drives ranchhand/deploy.sh (twilio's is structurally identical) and
# cowbot/deploy.sh with PATH stubs for cowboy/cbfs/curl that record every
# invocation and replay canned chain responses. Asserts the exact contracts
# the real CLIs enforce:
#   - `actor address --creator` receives a 20-byte ADDRESS, never a pubkey
#   - `actor address` output is parsed from the CLI's real shape — bare
#     lowercase hex, no 0x — by both the regex and cowbot's awk parser
#   - the declaration salt is the full right-padded 32-byte value
#   - `cbfs put` is positional: VOLUME LOCAL REMOTE
#   - `cbfs volume create` carries --owner-key and a nonzero --initial-escrow
#   - the cw() retry wrapper survives a transient nonzero exit under
#     `set -euo pipefail` (a plain $? capture aborts the whole script)
#   - a transient indexer 502 during the post-put poll is retried, not
#     misreported as duplicate chain volumes
#   - a loopback relayer URL does not demand a relayer API key
#   - declare pins --claimed-root to the post-put per-id manifest root
#
# The full-stack local e2e (real relayer + CBSS committee + storage nodes)
# remains separate; this harness pins the script-side command contracts.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GALLERY="$(dirname "$HERE")"
SANDBOX="$(mktemp -d)"
# KEEP_SANDBOX=1 preserves the sandbox (stub call log at state/calls.log) for
# debugging a failed assertion.
trap '[ -n "${KEEP_SANDBOX:-}" ] && echo "sandbox kept: $SANDBOX" || rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
assert() { # assert <desc> <cmd...>
    local desc="$1"; shift
    if "$@"; then echo "  ok: $desc"; PASS=$((PASS+1)); else echo "  FAIL: $desc" >&2; FAIL=$((FAIL+1)); fi
}

# ── sandbox: script + minimal companions ─────────────────────────────────────
mkdir -p "$SANDBOX/actor/tools" "$SANDBOX/bin" "$SANDBOX/state"
cp "$GALLERY/ranchhand/deploy.sh" "$SANDBOX/actor/deploy.sh"
printf '# stub actor\n' > "$SANDBOX/actor/ranchhand.py"
printf 'prompt\n' > "$SANDBOX/actor/system_prompt.txt"
printf '{}\n' > "$SANDBOX/actor/capability-schema.json"
printf '{}\n' > "$SANDBOX/actor/manifest.json"
printf 'open("%s","wb").write(b"")\n' "$SANDBOX/actor/tools/routes.cbor" > "$SANDBOX/actor/tools/emit_routes.py"
printf 'aa\n' > "$SANDBOX/key.hex"
mkdir -p "$SANDBOX/cowbot"
cp "$GALLERY/cowbot/deploy.sh" "$SANDBOX/cowbot/deploy.sh"
printf '# stub actor\n' > "$SANDBOX/cowbot/cowbot.py"
printf '{"owner":"OWNER_EOA"}\n' > "$SANDBOX/cowbot/manifest.json"

LOG="$SANDBOX/state/calls.log"
: > "$LOG"
CREATOR=0x89d9e2a79cb8d5a3a82ecc9e5bd32e849195956b
ACTOR=0x949fc46419d93f1003b660f4a7e43f905c036d53
VOLID_HEX=a7dd03c5418d3cd74a56799bce94a4c0d0dc3db0d6b141f812a342ed5f6c1533
ROOT_HEX=f4019731b20996379969db14b11988ac55d7ced4e6bf66bca8fa45749db9964d

# ── stub: cowboy ─────────────────────────────────────────────────────────────
cat > "$SANDBOX/bin/cowboy" << STUB
#!/usr/bin/env bash
echo "cowboy \$*" >> "\${HARNESS_LOG:-$LOG}"
case "\$*" in
    *"wallet address"*) echo "$CREATOR" ;;
    *"actor address"*)
        # Real CLI prints Creator BEFORE Actor address — a lazy grep grabs the
        # wrong line — and both as BARE lowercase hex (commonware hex, no 0x);
        # parsers must accept the unprefixed form.
        echo "  Creator: ${CREATOR#0x}"
        echo "  Actor address: ${ACTOR#0x}"
        ;;
    *"actor deploy"*)
        # First attempt fails transiently: cw() must retry, not abort. Only
        # for ranchhand's salt — cowbot deploys bare, with no retry wrapper.
        if [[ "\$*" == *"0x4a11"* ]] && [ ! -f "$SANDBOX/state/deploy-tried" ]; then
            touch "$SANDBOX/state/deploy-tried"
            echo "Service Unavailable"; exit 1
        fi
        echo "submitted"
        ;;
    *"get-declaration"*) echo "Declaration not found" >&2; exit 1 ;;
    *"declaration-id"*) echo "Declaration ID: 0x1111111111111111111111111111111111111111111111111111111111111111" ;;
    *) echo "ok" ;;
esac
STUB

# ── stub: cbfs ───────────────────────────────────────────────────────────────
cat > "$SANDBOX/bin/cbfs" << STUB
#!/usr/bin/env bash
echo "cbfs \$*" >> "\${HARNESS_LOG:-$LOG}"
case "\$*" in
    *"put "*) touch "$SANDBOX/state/put-done" ;;
esac
exit 0
STUB

# ── stub: curl ───────────────────────────────────────────────────────────────
cat > "$SANDBOX/bin/curl" << STUB
#!/usr/bin/env bash
url="\${@: -1}"
echo "curl \$url" >> "\${HARNESS_LOG:-$LOG}"
case "\$url" in
    */cbss/account-release-key/*) echo '{"mpk_g2":"0x01"}' ;;
    */ras/chain/volumes/$VOLID_HEX)
        echo '{"volume_id":[1],"owner":"$CREATOR","volume_name":"ranchhand-brain","manifest_root":[$(python3 -c "print(','.join(str(b) for b in bytes.fromhex('$ROOT_HEX')))")],"used_bytes":123}'
        ;;
    */ras/chain/volumes)
        # First post-put poll gets a 502 HTML body: volume_row must treat it
        # as transient and retry, not abort as "duplicate chain volumes".
        if [ -f "$SANDBOX/state/put-done" ] && [ ! -f "$SANDBOX/state/poll-502-served" ]; then
            touch "$SANDBOX/state/poll-502-served"
            echo '<html>502 Bad Gateway</html>'
        elif [ -f "$SANDBOX/state/put-done" ] || [ -f "$SANDBOX/state/created" ]; then
            echo '[{"volume_id":[$(python3 -c "print(','.join(str(b) for b in bytes.fromhex('$VOLID_HEX')))")],"owner":"$CREATOR","volume_name":"ranchhand-brain","manifest_root":[$(python3 -c "print(','.join(str(b) for b in bytes.fromhex('$ROOT_HEX')))")],"used_bytes":123}]'
        else
            touch "$SANDBOX/state/created"
            echo '[]'
        fi
        ;;
    */actor/0x*) exit 22 ;;  # -f style failure: actor not yet deployed
    *) echo '{}' ;;
esac
STUB
chmod +x "$SANDBOX/bin/cowboy" "$SANDBOX/bin/cbfs" "$SANDBOX/bin/curl"

# ── run the real script against the stubs ────────────────────────────────────
echo "── running deploy.sh under harness"
set +e
(
    cd "$SANDBOX/actor" &&
    PATH="$SANDBOX/bin:$PATH" \
    RANCHHAND_PRIVATE_KEY="$SANDBOX/key.hex" \
    COWBOY_RAS_WRITE_RELAYER_URL="http://127.0.0.1:9387" \
    CBFS_STATE="$SANDBOX/state/cbfs" \
    ./deploy.sh "http://stub:4000"
) > "$SANDBOX/state/run.log" 2>&1
RC=$?
set -e
[ "$RC" -eq 0 ] || { echo "deploy.sh exited $RC — run log:"; cat "$SANDBOX/state/run.log"; exit 1; }

# cowbot's deploy.sh is NOT structurally identical: it parses the actor
# address with awk instead of the regex, so it gets its own run and log.
echo "── running cowbot/deploy.sh under harness"
COWBOT_LOG="$SANDBOX/state/cowbot-calls.log"
: > "$COWBOT_LOG"
set +e
(
    cd "$SANDBOX/cowbot" &&
    PATH="$SANDBOX/bin:$PATH" \
    HARNESS_LOG="$COWBOT_LOG" \
    COWBOT_PRIVATE_KEY="$SANDBOX/key.hex" \
    ./deploy.sh "http://stub:4000"
) > "$SANDBOX/state/cowbot-run.log" 2>&1
COWBOT_RC=$?
set -e
[ "$COWBOT_RC" -eq 0 ] || { echo "cowbot/deploy.sh exited $COWBOT_RC — run log:"; cat "$SANDBOX/state/cowbot-run.log"; exit 1; }

# ── assertions ───────────────────────────────────────────────────────────────
echo "── assertions"
assert "actor address --creator gets a 20-byte address" \
    grep -qE -- "actor address --code [^ ]*ranchhand.py --creator $CREATOR " "$LOG"
assert "declaration salt is right-padded 32 bytes" \
    grep -q -- "--salt 0x4a12000000000000000000000000000000000000000000000000000000000000" "$LOG"
assert "cbfs put is positional VOLUME LOCAL REMOTE" \
    grep -q "cbfs .*put ranchhand-brain $SANDBOX/actor/system_prompt.txt ranchhand/system_prompt.txt" "$LOG"
assert "volume create carries --owner-key and nonzero escrow" \
    grep -q "volume create --name ranchhand-brain --owner-key --initial-escrow 1" "$LOG"
assert "cw() retried the transient actor deploy failure" \
    test "$(grep -c "actor deploy" "$LOG")" -ge 2
assert "loopback relayer needs no API key (script ran without one)" \
    test "$RC" -eq 0
assert "declare pins --claimed-root to the per-id manifest root" \
    grep -q -- "declare .*--claimed-root 0x$ROOT_HEX" "$LOG"
assert "declare uses private visibility with the escrow envelope" \
    grep -q -- "--code-visibility private" "$LOG"
assert "cbfs auth setup ran (no stale-status trust)" \
    grep -q "cbfs --state-dir .* auth setup" "$LOG"
assert "transient 502 mid-poll was served and retried, not fatal" \
    test -f "$SANDBOX/state/poll-502-served"
assert "cowbot awk parser extracts the bare-hex Actor address line" \
    grep -q "actor address = ${ACTOR#0x}" "$SANDBOX/state/cowbot-run.log"
assert "cowbot derives the address with --creator <deployer address>" \
    grep -qE -- "actor address --code [^ ]*cowbot.py --creator $CREATOR --salt 0xc0" "$COWBOT_LOG"

echo
if [ "$FAIL" -gt 0 ]; then echo "FAILED: $FAIL of $((PASS+FAIL)) assertions" >&2; exit 1; fi
echo "PASSED: all $PASS assertions"
