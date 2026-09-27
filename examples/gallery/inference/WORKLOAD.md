# Serving the off-chain handlers as a CIP-10 workload

The actor's `offchain/` handlers run in a container the Gateway reaches
through CIP-15 §8.15 workload routes. Nothing about the handlers is specific
to this shape; `offchain/host.py` is the process the container starts, and it
supplies what a request must never supply (actor identity, hire slots, CBFS
volume bindings, the ingress issuing key, the upstream credential) from one
operator-written config file. Chain admission per invocation, cbssd secret
release and metering settlement are the runner executor's job and are not
done here; a `/v1/chat/completions` call is one session.

Pieces, in the order they are used:

| Piece | What it does |
|---|---|
| `image/build-image.sh` | Builds the serving image (python, `offchain/`, the cbfs CLI) and exports it as a digest-addressed rootfs `<digest>.tar.gz`. |
| `image/image_digest.py` | The runner's `rootfs_digest` in Python: digest a tar where it is built, a rootfs where it is deployed. |
| `cowboy container register-image / register-class / register-workload` | The Container Registry (actor `0x13`) records: image digest, resource class, and the workload record that names the serving endpoint. System-deployer signed. |
| `image/deploy-workload.sh` | On the Linux box: provision the rootfs under the runner image root, verify its digest, run it with runc (host network, read-only rootfs), report status on chain every 20 s. |
| `install_routes.py --gateway-toml` | The Gateway operator's `[[workload_routes]]` entries for the actor, generated from `route-intent.json`. |

## 1. Image

```sh
image/build-image.sh --arch amd64            # cbfs from ~/dev/cowboy/cbfs (devnet), cross-compiled in a rust container
image/build-image.sh --arch amd64 --cbfs-binary /path/to/linux/cbfs
```

Output: `image/.build/<digest>.tar.gz` and `<digest>.json` (digest, size,
arch, cbfs commit, actor tree commit). The cbfs CLI's private git dependencies
come from the host's `~/.cargo/git`; nothing but the built binary enters the
image. The digest is `image_digest.py --tar`, which equals the runner's
`runner-node image-digest` of the rootfs a root `tar -xzp` produces.

## 2. Registry records

Signed by a genesis `system_deployers` key:

```sh
cowboy container register-class --name inference-small --cpu-millicores 1000 --memory-mib 1024 --max-duration-sec 86400 --private-key <deployer>
cowboy container register-image --name inference-host --digest <digest> --size-bytes <size> --private-key <deployer>
cowboy container register-workload --workload-id <64 hex> --owning-actor <actor> --image-digest <digest> \
    --resource-class inference-small --serving-endpoint http://<box>:8188 --reporter <reporter address> --private-key <deployer>
```

The workload id is the operator's choice (the harness uses
`sha256("cowboy/inference-workload:" + actor)`). The serving endpoint is
where the Gateway relays: an absolute `http(s)://host:port`, no path needed.
The relayed `X-Cowboy-Route-Context` is `base64url(CBOR)` and carries no
Gateway signature (gateway-types), so the host cannot tell a Gateway relay
from a direct caller on that port; reach the port only from the Gateway
(firewall or private network), because a direct caller can present any
route context.
The reporter is the key on the box that publishes `ReportWorkloadStatus`;
the Gateway serves the route only while the record is desired Running,
observed Running, and observed within the last 60 blocks, and the registry
removes a record left unreported past that lease plus a grace window.

## 3. The box (Linux, root, runc)

`/etc/cowboy/inference/` holds `host.json` (`listen` bound to the endpoint's
port; `cbfs.binary` is `/usr/local/bin/cbfs`; state dirs under
`/var/lib/cowboy/inference-host`), `owner.key`, the trusted checkpoint, and
`reporter.key`. Then:

```sh
image/deploy-workload.sh --image <digest>.tar.gz --workload-id <id> --generation 1 \
    --config-dir /etc/cowboy/inference --state-dir /var/lib/cowboy/inference-host \
    --reporter-key /etc/cowboy/inference/reporter.key --rpc-url http://<node>:4000 --port 8188
```

This extracts the rootfs to `/var/lib/cowboy/images/<digest>/rootfs`,
refuses to run unless it digests back to `<digest>`, writes an OCI bundle
(read-only rootfs, no capabilities, `/etc/cowboy` read-only and the state
dir read-write bind-mounted, host network so the container answers at the
serving endpoint), and installs two systemd units: the runc service and a
heartbeat that reports `running` while `/_cowboy/health` answers and `degraded`
otherwise. The generation is 1 after registration and advances with every
`update-workload`; pass the current one.

## 4. Gateway routes

```sh
python3 install_routes.py --gateway-toml --actor <actor> --workload-id <id> --out workload-routes.toml
```

Append the output to the Gateway's config. Each route names the workload;
the Gateway resolves the record with a proof-checked registry read and never
dials an endpoint written in its own config. The actor's manifest must not
declare overlapping routes (the Gateway refuses to start if it does); the
route table is entirely the operator's `[[workload_routes]]`.

## What the process sees

The Gateway relays the caller's request as sent: method, path, query, headers,
body, plus `x-cowboy-route-context` (base64url CBOR: actor, workload, verb,
path, auth, the account-proof fields, chain id, network). `host.py` verifies
the context, dispatches by path (`/manage*` → management, `/v1/models`,
`/v1/chat/completions`), and passes the account proof through unchanged, so a
signed management call is checked against the target the caller signed.
`/_cowboy/route` (the CIP-15 runner-route form) is also served; the CIP-10
path does not use it.

## Local proof

`cip9-grants-e2e.sh` with `ROUTE_HOST=workload` runs the whole chain locally:
registry records with the genesis deployer key, a reporter heartbeat, the
Gateway with generated workload routes, and the same signed client calls as
the runner-route form. On macOS the serving process runs directly at the
endpoint (no runc); `INFERENCE_IMAGE_JSON=image/.build/<digest>.json` registers
a real image digest, otherwise a stand-in rootfs is digested. Running the
container from the verified rootfs is `deploy-workload.sh` on a Linux box.
