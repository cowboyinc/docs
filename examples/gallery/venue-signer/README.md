# Venue Signer

The proposed CIP-10 signing bundle for live trading: an off-chain function
image that holds no key of its own, receives a venue key through
`RuntimeConfig.secret_env`, signs an order **offline**, and returns the exact
HTTP request to send. The actor sends it with `runner.http`. The runner never
learns a venue's signing scheme, and the box needs no network.

> **Integration status:** staged, not runnable on current `main`.
> cowboy-protocol#127 adds the `secret_env` wire field, cowboy#390 specifies
> it and the `container.run` entitlement, node#1626 exposes both to actors,
> and runner#240 performs the scoped environment injection. CIP-24 §3.9
> already defines environment delivery as a compatibility exception for
> spawned subprocesses; cowboy#390 extends that model specifically to a
> CIP-10 container sandbox. Land and deploy that support train before using
> this example. The committed `schema_version = 2` / `[offchain]` manifest is
> likewise staged on the node off-chain build work (node#1599/#1610); the CLI
> on current `main` supports schema version 1 and will intentionally reject it.

| Function | Venue | Key (env) | Returns |
|---|---|---|---|
| `sign_hyperliquid_order(order)` | Hyperliquid (EIP-712 phantom agent, `hyperliquid-python-sdk`) | `HYPERLIQUID_PRIVATE_KEY` | `SignedRequest{url, method, headers, body, nonce, price, size, asset, is_buy, reduce_only, tif, expires_after}` |
| `sign_gemini_order(order)` | Gemini (HMAC-SHA384 over base64 payload + nonce) | `GEMINI_API_KEY`, `GEMINI_API_SECRET` | next |

Layout follows the staged `cowboy init --offchain` work (node
`examples/offchain_score_report`, not current `main`):
`offchain/main.py` is the interface, `offchain/tests` are plain pytest and
never enter the image, `pyproject.toml`'s `offchain` group is the only thing
installed into the delta, and `uv.lock` is what the build installs from.

## How the trading actor uses it

```python
# the actor already fetched mids + meta from /info with runner.http (no key)
ctx.signed = await sign_hyperliquid_order(HyperliquidOrder(
    asset=0, is_buy=True, size=0.0123, limit_px=78_600.0,
    sz_decimals=5, is_spot=False, tif="Ioc", mainnet=False,
    # Both fields are required safety boundaries: never infer network or market
    # kind from a truthy value or from the numeric asset range.
))
# ctx.signed.price / ctx.signed.size are what was actually signed
ctx.venue = await runner.http(ctx.signed.url, method="POST",
                              headers=ctx.signed.headers, body=ctx.signed.body)
```

After the support train above is deployed, `sign_hyperliquid_order` is the
generated proxy (`actors/offchain.py` from `cowboy offchain build`), which
dispatches a container job on this image. Two things ride that job that the
proxy generator does not yet emit:
`secrets=["HYPERLIQUID_PRIVATE_KEY"]` (the CIP-24 authorization; the actor's
manifest then needs `secrets.read` for the key and `container.run`, the
entitlement node#1626 registers and cowboy#390 documents) and
`secret_env=["HYPERLIQUID_PRIVATE_KEY"]` (the delivery, cowboy-protocol#127).
Until `[offchain]` grows a per-function `secrets` field, the harness's trading
template calls `runner.container` with the pinned digest and both kwargs by
hand.

`mainnet` has no default. The demo desk is testnet-only, and the signed
request's host and the phantom-agent `source` the EIP-712 signature commits
to (`a` mainnet, `b` testnet; the domain chainId is 1337 on both) follow that
flag, so a caller that omits it fails to construct the order rather than
signing for mainnet.

What gets signed is what the venue accepts, snapped in the direction that
cannot make the order more aggressive than asked: a buy's price rounds down,
a sell's up, size always down (a reduce-only close never exceeds the
position). If snapping moves price or size by more than `max_rounding_bps` (an
integer from 0 through 500; default 50) the order is refused rather than
signed; `SignedRequest.price` and `.size` report the values committed to, and
`.asset`, `.is_buy`, `.reduce_only` and `.tif` restate the order so the actor
can audit the request without re-parsing the body. The cap is deliberate: at
a 100% tolerance the size guard is inert (a floored size is always less than
one requested size away), so a reduce-only close could sign for a fraction of
the position while the desk believes it flattened.
Non-finite or non-positive size and price, non-boolean control fields, an
`asset` that is not a small non-negative index, and `sz_decimals` outside the
explicit market kind's range (0–6 perps, 0–8 spot) are refused
before anything is signed. `asset` is the only market identifier the venue
signs over; it, `sz_decimals`, and `is_spot` come from `/info` metadata. The
explicit market kind matters because builder-deployed perps use high asset
indexes that overlap the naive `asset >= 10000` spot heuristic.

The nonce is what Hyperliquid de-duplicates per key: unique among the key's
recent orders and within about a day of venue time, in milliseconds. By
default it is the box's clock, which is enough for one sequential signer per
key — the demo desk's `trade` path. Two signers sharing a key must pass
distinct `nonce` values themselves.

Signing and sending happen in different processes, with the signed blob held
in actor state in between, and Hyperliquid accepts a nonce for about a day.
`expires_after` (venue time in milliseconds, later than the nonce) is signed
into the order so a delayed send, a crash retry, or a replayed container
result cannot fire at a stale price past the deadline; `None` leaves the
order valid for as long as the nonce is.

## Tests (no chain, no network)

This is an off-chain function project, not a runnable actor: there is no
`demo.sh`; the interface is `offchain/main.py` and the tests are the
entrypoint. CI runs them on every change to this directory.

```bash
uv run --locked --group offchain --group test pytest
```

The signature test recovers the signer address from the produced
`{action, nonce, signature}` with the SDK's own
`recover_agent_or_user_from_l1_action`, for both phantom-agent sources
(mainnet `a`, testnet `b`), and asserts the key never appears in the request.
A frozen `r/s/v` vector per network pins the exact bytes under the locked
SDK, so a bump that changes the EIP-712 domain, the phantom-agent source
letters or the action hash fails the suite instead of passing a round trip.
`uv run` creates `.venv/` and Python caches here; they are gitignored. The
examples structure test inspects tracked paths, so ignored developer state is
not mistaken for a committed artifact.

## Build and publish

```bash
cowboy offchain build            # node `cowboy offchain build` (node#1610)
```

Prints the composed digest and writes the delta under `.cowboy/offchain/`.
Publishing the delta to a public CBFS volume and registering the digest is
the permissionless image path (CIP-10 §5.5–5.6; runner#239, node#1609). The image
is public by construction; it carries this source and the SDK wheels, never a
key.

## Going live from the paper desk

The Hyperliquid demo desk's trading actor (`cowboy-harness` templates.rs) is
a paper book. Its `_on_trade` is the one step to replace: instead of
recording a fill at the mid, await `sign_hyperliquid_order`, POST the
returned request with `runner.http`, and record the fill from the venue's
`response.data.statuses[0].filled`. Everything around it — handlers, storage
keys, the interface actor — stays.
