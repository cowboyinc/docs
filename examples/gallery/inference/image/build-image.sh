#!/usr/bin/env bash
# Build the inference actor's serving image as a CIP-10 digest-addressed rootfs.
#
#   image/build-image.sh [--arch amd64|arm64] [--cbfs-src ~/dev/cowboy/cbfs | --cbfs-binary <linux cbfs>] [--out image/.build]
#
# 1. cbfs CLI for Linux, compiled in a rust container from the devnet checkout
#    (private git deps come from the host's ~/.cargo/git; no credentials enter
#    any image layer). Cross-compiles when --arch differs from the docker host.
# 2. docker build of image/Dockerfile (python + offchain/ + cbfs).
# 3. docker export → flat rootfs tar → digest (image_digest.py, the runner's
#    rootfs_digest) → <out>/<digest>.tar.gz + <digest>.json.
#
# Then: cowboy container register-image --name inference-host --digest <digest> --size-bytes <n>
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
INFERENCE_DIR=$(dirname "$HERE")
ARCH=amd64; CBFS_SRC=${CBFS_SRC:-$HOME/dev/cowboy/cbfs}; CBFS_BINARY=""; OUT=$HERE/.build; RUST_IMAGE=rust:1.93-bookworm
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH=$2; shift 2;;
    --cbfs-src) CBFS_SRC=$2; shift 2;;
    --cbfs-binary) CBFS_BINARY=$2; shift 2;;
    --out) OUT=$2; shift 2;;
    *) echo "unknown argument $1" >&2; exit 2;;
  esac
done
case "$ARCH" in amd64) TRIPLE=x86_64-unknown-linux-gnu; GCC=gcc-x86-64-linux-gnu; CROSS_CC=x86_64-linux-gnu-gcc;; arm64) TRIPLE=aarch64-unknown-linux-gnu; GCC=gcc-aarch64-linux-gnu; CROSS_CC=aarch64-linux-gnu-gcc;; *) echo "arch must be amd64 or arm64" >&2; exit 2;; esac
HOST_ARCH=$(docker info --format '{{.Architecture}}'); case "$HOST_ARCH" in x86_64) HOST_ARCH=amd64;; aarch64) HOST_ARCH=arm64;; esac
mkdir -p "$OUT"; CTX=$OUT/ctx; rm -rf "$CTX"; mkdir -p "$CTX"

if [ -z "$CBFS_BINARY" ]; then
  echo "== cbfs CLI ($TRIPLE) from $CBFS_SRC"
  CBFS_COMMIT=$(git -C "$CBFS_SRC" rev-parse --short HEAD)
  mkdir -p "$OUT/cbfs-target"
  CROSS_ENV=""
  if [ "$ARCH" != "$HOST_ARCH" ]; then
    CROSS_ENV="apt-get update -qq && apt-get install -y -qq $GCC >/dev/null && rustup target add $TRIPLE && export CC_${TRIPLE//-/_}=$CROSS_CC CARGO_TARGET_$(echo "$TRIPLE" | tr 'a-z-' 'A-Z_')_LINKER=$CROSS_CC &&"
  fi
  docker run --rm -v "$CBFS_SRC:/src:ro" -v "$HOME/.cargo/registry:/usr/local/cargo/registry" -v "$HOME/.cargo/git:/usr/local/cargo/git" -v "$OUT/cbfs-target:/target" -w /src "$RUST_IMAGE" \
    sh -c "$CROSS_ENV cargo build --release --locked -p cbfs-cli --target $TRIPLE --target-dir /target"
  CBFS_BINARY=$OUT/cbfs-target/$TRIPLE/release/cbfs
else
  CBFS_COMMIT=external
fi
cp "$CBFS_BINARY" "$CTX/cbfs"; chmod 0755 "$CTX/cbfs"
cp "$HERE/Dockerfile" "$CTX/Dockerfile"
mkdir -p "$CTX/offchain"; (cd "$INFERENCE_DIR/offchain" && tar --exclude='__pycache__' -cf - .) | tar -xf - -C "$CTX/offchain"

echo "== docker build (linux/$ARCH)"
TAG=cowboy-inference-host:build
docker build --platform "linux/$ARCH" -t "$TAG" "$CTX" >/dev/null
CID=$(docker create --platform "linux/$ARCH" "$TAG")
docker export "$CID" > "$OUT/rootfs.tar"; docker rm "$CID" >/dev/null

echo "== digest"
# image_digest.py needs blake3; PYTHON overrides the interpreter (default: uv with blake3).
DIGEST=$(${PYTHON:-uv run --with blake3 python} "$HERE/image_digest.py" --tar "$OUT/rootfs.tar")
gzip -nf "$OUT/rootfs.tar"; mv "$OUT/rootfs.tar.gz" "$OUT/$DIGEST.tar.gz"
SIZE=$(stat -f %z "$OUT/$DIGEST.tar.gz" 2>/dev/null || stat -c %s "$OUT/$DIGEST.tar.gz")
printf '{"digest":"%s","size_bytes":%s,"arch":"%s","cbfs_commit":"%s","actor_tree":"%s","built_at":"%s"}\n' \
  "$DIGEST" "$SIZE" "$ARCH" "$CBFS_COMMIT" "$(git -C "$INFERENCE_DIR" rev-parse --short HEAD)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$OUT/$DIGEST.json"
cat "$OUT/$DIGEST.json"
echo "register: cowboy container register-image --name inference-host --digest $DIGEST --size-bytes $SIZE --private-key <system deployer>"
