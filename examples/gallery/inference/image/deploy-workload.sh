#!/usr/bin/env bash
# Run the inference actor's serving image as a CIP-10 workload on a Linux box
# (root; runc). The image is provisioned digest-addressed under the runner's
# image root and verified before anything runs from it.
#
#   image/deploy-workload.sh --image <digest>.tar.gz --workload-id <64 hex> --generation <n> \
#       --config-dir /etc/cowboy/inference --state-dir /var/lib/cowboy/inference-host \
#       --reporter-key /etc/cowboy/inference/reporter.key --rpc-url http://<node>:4000 \
#       [--port 8188] [--image-root /var/lib/cowboy/images] [--uid 1000] [--cowboy-bin cowboy] [--runc runc] [--env KEY=VALUE]...
#
# --config-dir holds host.json (listen 0.0.0.0:<port>; cbfs.binary /usr/local/bin/cbfs;
# state dirs under /var/lib/cowboy/inference-host as seen inside the container),
# owner.key, and the trusted checkpoint host.json names. It is mounted read-only
# at /etc/cowboy; --state-dir is mounted read-write at /var/lib/cowboy/inference-host.
#
# Installs two systemd units: cowboy-workload-<id8>.service (runc, Restart=always)
# and cowboy-workload-<id8>-heartbeat.service (ReportWorkloadStatus every 20 s:
# running while /_cowboy/health answers, degraded otherwise). The record's reporter must
# be the --reporter-key address and it pays the report gas.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=8188; IMAGE_ROOT=${RUNNER_CONTAINER_IMAGE_ROOT:-/var/lib/cowboy/images}; UID_IN=1000; COWBOY=cowboy; RUNC=runc
IMAGE=""; WORKLOAD_ID=""; GENERATION=""; CONFIG_DIR=""; STATE_DIR=""; REPORTER_KEY=""; RPC_URL=""; EXTRA_ENV=()
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE=$2; shift 2;; --workload-id) WORKLOAD_ID=$2; shift 2;; --generation) GENERATION=$2; shift 2;;
    --config-dir) CONFIG_DIR=$2; shift 2;; --state-dir) STATE_DIR=$2; shift 2;; --reporter-key) REPORTER_KEY=$2; shift 2;;
    --rpc-url) RPC_URL=$2; shift 2;; --port) PORT=$2; shift 2;; --image-root) IMAGE_ROOT=$2; shift 2;; --uid) UID_IN=$2; shift 2;;
    --cowboy-bin) COWBOY=$2; shift 2;; --runc) RUNC=$2; shift 2;;
    --env) EXTRA_ENV+=("$2"); shift 2;;  # KEY=VALUE for the container process, repeatable
    *) echo "unknown argument $1" >&2; exit 2;;
  esac
done
for v in IMAGE WORKLOAD_ID GENERATION CONFIG_DIR STATE_DIR REPORTER_KEY RPC_URL; do [ -n "${!v}" ] || { echo "--$(echo $v | tr 'A-Z_' 'a-z-') is required" >&2; exit 2; }; done
[ "$(id -u)" = 0 ] || { echo "run as root (runc, image root)" >&2; exit 2; }
[ "$(uname -s)" = Linux ] || { echo "Linux only" >&2; exit 2; }
echo "$WORKLOAD_ID" | grep -Eq '^[0-9a-fA-F]{64}$' || { echo "--workload-id must be 32 bytes hex" >&2; exit 2; }
[ -f "$CONFIG_DIR/host.json" ] || { echo "$CONFIG_DIR/host.json missing" >&2; exit 2; }
command -v "$RUNC" >/dev/null || { echo "runc not found" >&2; exit 2; }

DIGEST=$(basename "$IMAGE" .tar.gz)
echo "$DIGEST" | grep -Eq '^[0-9a-f]{64}$' || { echo "image file must be named <digest>.tar.gz" >&2; exit 2; }
ROOTFS=$IMAGE_ROOT/$DIGEST/rootfs
if [ ! -d "$ROOTFS" ]; then
  echo "== provision $ROOTFS"
  mkdir -p "$ROOTFS.tmp"; tar -xzpf "$IMAGE" -C "$ROOTFS.tmp" --numeric-owner; mv "$ROOTFS.tmp" "$ROOTFS"
fi
echo "== verify digest"
if command -v runner-node >/dev/null; then ACTUAL=$(runner-node image-digest "$ROOTFS" | awk '{print $NF}' | sed 's/^0x//')
else
  python3 -c 'import blake3' 2>/dev/null || { echo "digest check needs runner-node on PATH or python3 with blake3 (pip install blake3)" >&2; exit 2; }
  ACTUAL=$(python3 "$HERE/image_digest.py" --rootfs "$ROOTFS")
fi
[ "$ACTUAL" = "$DIGEST" ] || { echo "rootfs digest $ACTUAL != $DIGEST; refusing" >&2; exit 1; }

ID8=${WORKLOAD_ID:0:8}; NAME=cowboy-workload-$ID8; BUNDLE=/var/lib/cowboy/workloads/$ID8
mkdir -p "$BUNDLE" "$STATE_DIR"; chown "$UID_IN:$UID_IN" "$STATE_DIR"
echo "== bundle $BUNDLE"
cat > "$BUNDLE/config.json" <<CFG
{
  "ociVersion": "1.0.2",
  "process": {
    "terminal": false,
    "user": {"uid": $UID_IN, "gid": $UID_IN},
    "args": ["python3", "-m", "offchain.host", "--config", "/etc/cowboy/host.json"],
    "env": ["PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1", "HOME=/var/lib/cowboy/inference-host",
            "CBFS_OWNER_KEY_FILE=/etc/cowboy/owner.key", "RUST_LOG=warn"$(for e in "${EXTRA_ENV[@]}"; do printf ', "%s"' "$e"; done)],
    "cwd": "/app",
    "noNewPrivileges": true,
    "capabilities": {"bounding": [], "effective": [], "permitted": [], "ambient": []},
    "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 65536, "soft": 65536}]
  },
  "root": {"path": "$ROOTFS", "readonly": true},
  "hostname": "$NAME",
  "mounts": [
    {"destination": "/proc", "type": "proc", "source": "proc"},
    {"destination": "/dev", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "strictatime", "mode=755", "size=65536k"]},
    {"destination": "/dev/pts", "type": "devpts", "source": "devpts", "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620"]},
    {"destination": "/dev/shm", "type": "tmpfs", "source": "shm", "options": ["nosuid", "noexec", "nodev", "mode=1777", "size=65536k"]},
    {"destination": "/sys", "type": "sysfs", "source": "sysfs", "options": ["nosuid", "noexec", "nodev", "ro"]},
    {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "nodev", "mode=1777", "size=262144k"]},
    {"destination": "/etc/resolv.conf", "type": "bind", "source": "/etc/resolv.conf", "options": ["rbind", "ro"]},
    {"destination": "/etc/hosts", "type": "bind", "source": "/etc/hosts", "options": ["rbind", "ro"]},
    {"destination": "/etc/cowboy", "type": "bind", "source": "$CONFIG_DIR", "options": ["rbind", "ro"]},
    {"destination": "/var/lib/cowboy/inference-host", "type": "bind", "source": "$STATE_DIR", "options": ["rbind", "rw"]}
  ],
  "linux": {
    "namespaces": [{"type": "pid"}, {"type": "ipc"}, {"type": "uts"}, {"type": "mount"}],
    "maskedPaths": ["/proc/kcore", "/proc/keys", "/proc/latency_stats", "/proc/timer_list", "/proc/timer_stats", "/proc/sched_debug", "/proc/scsi", "/sys/firmware"],
    "readonlyPaths": ["/proc/asound", "/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]
  }
}
CFG
# No network namespace: the workload listens on the host at the serving endpoint
# the registry record names (host.json "listen" must bind the same port).
grep -q "\"listen\": *\"[^\"]*:$PORT\"" "$CONFIG_DIR/host.json" || echo "warning: host.json listen does not name port $PORT" >&2

cat > "$BUNDLE/heartbeat.sh" <<HB
#!/bin/sh
# ReportWorkloadStatus for $WORKLOAD_ID (generation $GENERATION); the registry's
# lease is 60 blocks, so report well inside it.
while :; do
  if curl -fsS -m 3 "http://127.0.0.1:$PORT/_cowboy/health" >/dev/null 2>&1; then status=running; else status=degraded; fi
  "$COWBOY" container report-workload-status --workload-id "$WORKLOAD_ID" --generation "$GENERATION" --status "\$status" --private-key "$REPORTER_KEY" --rpc-url "$RPC_URL" >/dev/null 2>&1 || echo "report-workload-status failed (\$status)" >&2
  sleep 20
done
HB
chmod 0755 "$BUNDLE/heartbeat.sh"

cat > "/etc/systemd/system/$NAME.service" <<UNIT
[Unit]
Description=Cowboy CIP-10 workload $WORKLOAD_ID (inference actor serving image $DIGEST)
After=network-online.target
Wants=network-online.target
[Service]
ExecStartPre=-$RUNC delete -f $NAME
ExecStart=$RUNC run --bundle $BUNDLE $NAME
ExecStop=$RUNC kill $NAME TERM
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
cat > "/etc/systemd/system/$NAME-heartbeat.service" <<UNIT
[Unit]
Description=Cowboy workload $ID8 status reports
After=$NAME.service
BindsTo=$NAME.service
[Service]
ExecStart=$BUNDLE/heartbeat.sh
Restart=always
RestartSec=5
[Install]
WantedBy=$NAME.service
UNIT
systemctl daemon-reload
systemctl enable "$NAME.service" "$NAME-heartbeat.service"
systemctl restart "$NAME.service"
systemctl start "$NAME-heartbeat.service"
for _ in $(seq 1 30); do curl -fsS -m 2 "http://127.0.0.1:$PORT/_cowboy/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS -m 2 "http://127.0.0.1:$PORT/_cowboy/health" >/dev/null || { journalctl -u "$NAME.service" -n 30 --no-pager; echo "workload never answered /_cowboy/health" >&2; exit 1; }
echo "== $NAME up; rootfs $ROOTFS; status reports from $(cat "$REPORTER_KEY" >/dev/null && echo "$REPORTER_KEY")"
