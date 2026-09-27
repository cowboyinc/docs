# Actor-hosted Hyperliquid trader dashboard

This replaces the local Next.js server with a self-contained Cowboy web actor.

- /ui/, /ui/styles.css, and /ui/app.js are served by read-only actor routes.
- /ui/api/dashboard returns only actor configuration; the browser joins it with
  bounded /ui/api/research, /ui/api/research/lessons, and /ui/api/trader reads.
- Each aggregation request performs at most three synchronous actor reads, so it
  stays below the 512-access `/actor/read` ceiling.
- /ui/api/health reports whether both source actors are configured.

There is no fabricated fallback. Failed actor reads produce explicit errors
and zero/empty fields. The browser is read-only and cannot submit orders.

## Build and test

Run:

    python3 tools/build_actor.py
    python3 tools/lower_routes.py
    pytest tests/test_ui_host.py -q
    node --check hl-ui/app.js

The build embeds the three files from hl-ui/ into dist/main.py. The generated
routes.cbor installs the eighteen GET/HEAD routes.

## Deploy

Deploy dist/main.py with atomic init containing research_actor, trader_actor,
and network_name. Then call set_routes with the generated routes.cbor. The UI
actor and source actors must be on the same chain because dashboard aggregation
uses bounded synchronous actor calls.

On Canyon, install the route blob and use the native actor gateway; no
QuickTunnel is required:

    export COWBOY_RPC_URL=https://rpc.canyon.cowboylabs.net
    cowboy actor deploy --code dist/main.py --manifest-json manifest.json \
      --salt 0x<hex> --private-key ../tools/deploykey \
      --init-payload '{"research_actor":"0x...","trader_actor":"0x...","network_name":"Canyon"}'
    cowboy actor execute --actor 0x<ui-actor> --handler set_routes \
      --payload @routes.cbor --private-key ../tools/deploykey

The public URL is `https://0x<ui-actor>.canyon.cowboylabs.net/ui/`.
