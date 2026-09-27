#!/usr/bin/env bash
# load-knowledge.sh — populate the cowboyinc-3 volume with cowbot-readable
# source for cowboy, node, runner, gateway, cbfs, cbss.
#
# Filters out build artifacts (target/, node_modules/, etc.), lock files,
# and anything >256KB. Walks each repo from $REPO_BASE and uploads via
# `cbfs put-many` in one open/commit cycle.

set -euo pipefail

REPO_BASE="${REPO_BASE:-/Users/chadd/dev/cowboy}"
VOLUME="${COWBOT_KNOWLEDGE_VOLUME:-cowboyinc-3}"
RPC_URL="${COWBOY_RPC_URL:-http://127.0.0.1:4002}"
CBFS="${CBFS_BIN:-/Users/chadd/dev/cowboy/cbfs/target/release/cbfs}"
STATE_DIR="${CBFS_STATE_DIR:-$HOME/.cbfs}"
REPOS=(cowboy node runner gateway cbfs cbss)
MANIFEST="$(mktemp -t cowbot-load.XXXXXX.json)"
MAX_BYTES="${MAX_BYTES:-262144}"  # 256 KiB
# Chain caps shard_refs at 4096/commit. With K+M=3 that's ~1300 files;
# leave headroom in case the volume bumps to K+M=5.
BATCH_SIZE="${BATCH_SIZE:-700}"

trap 'rm -f "$MANIFEST" "$MANIFEST".part.* 2>/dev/null' EXIT

python3 - "$REPO_BASE" "$MAX_BYTES" "$MANIFEST" "${REPOS[@]}" <<'PY'
import os, sys, json
base, max_bytes, out_path, *repos = sys.argv[1:]
max_bytes = int(max_bytes)

KEEP_EXT = {
    ".rs", ".py", ".md", ".mdx", ".toml", ".yaml", ".yml",
    ".ts", ".tsx", ".js", ".jsx", ".json", ".sh", ".txt",
    ".sql", ".html", ".css", ".proto",
}
EXCLUDE_DIRS = {
    "target", "node_modules", ".git", ".venv", "venv",
    "dist", "build", "__pycache__", ".cargo", ".next",
    ".turbo", "out", "coverage", ".pytest_cache",
}
EXCLUDE_FILES = {
    "Cargo.lock", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "uv.lock", "poetry.lock",
}
# Vendored / generated subtrees we don't want polluting the bot's grep.
# Paths are relative to $REPO_BASE so they match repo-prefixed walks.
EXCLUDE_PREFIXES = (
    "node/pvm/Lib/",         # vendored CPython stdlib
    "node/pvm/extra_tests/", # massive test fixtures
    "node/pvm/wasm/",        # generated wasm
)

entries = []
for repo in repos:
    root = os.path.join(base, repo)
    if not os.path.isdir(root):
        print(f"WARN: {root} missing, skipping", file=sys.stderr)
        continue
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in filenames:
            if name in EXCLUDE_FILES or name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in KEEP_EXT:
                continue
            local = os.path.join(dirpath, name)
            try:
                if os.path.getsize(local) > max_bytes:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(local, base)  # repo/<...>
            if any(rel.startswith(p) for p in EXCLUDE_PREFIXES):
                continue
            entries.append({"local": local, "remote": rel})

with open(out_path, "w") as f:
    json.dump(entries, f)

bytes_total = sum(os.path.getsize(e["local"]) for e in entries)
print(f"manifest: {len(entries)} files, {bytes_total/1024/1024:.1f} MiB", file=sys.stderr)
PY

echo "uploading to $VOLUME via $RPC_URL in batches of $BATCH_SIZE ..."
python3 - "$MANIFEST" "$BATCH_SIZE" <<'PY'
import json, sys, os
manifest, batch = sys.argv[1], int(sys.argv[2])
with open(manifest) as f: entries = json.load(f)
total = (len(entries) + batch - 1) // batch
for i in range(0, len(entries), batch):
    n = i // batch + 1
    part = f"{manifest}.part.{n:02d}"
    with open(part, "w") as f: json.dump(entries[i:i+batch], f)
    print(f"BATCH {n}/{total} -> {part} ({len(entries[i:i+batch])} files)")
PY

n=0
for part in "$MANIFEST".part.*; do
    n=$((n+1))
    echo ">>> batch $n: $part"
    "$CBFS" put-many "$VOLUME" --from-json "$part" --state-dir "$STATE_DIR" --rpc-url "$RPC_URL" || {
        echo "FAILED on batch $n — manifest left at $part for retry"
        exit 1
    }
done
echo "done"
