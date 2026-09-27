"""Actor-hosted Hyperliquid dashboard with bounded cross-actor aggregation.

The deployment build embeds the static files and serves them from actor GET
routes. The browser joins small research and trader API responses so no
``/actor/read`` request exceeds its 512-access ceiling.
"""

import json

from cowboy_sdk import OWNER, actor, call, callable_by, codec, public, pure, runtime
from cowboy_sdk.routes import get

ROUTES_KEY = "__cowboy/routes"
KEY_RESEARCH = "research_actor"
KEY_TRADER = "trader_actor"
KEY_NETWORK = "network_name"

JSON_HEADERS = [
    ("content-type", "application/json; charset=utf-8"),
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
]
# Active thesis rows carry price baselines for the full research universe.
# Serializing that bounded response exceeded the former 300k nested-call lane
# on Canyon even though the smaller stats/ideas reads succeeded.
CALL_CYCLES = 2_000_000
DASHBOARD_GAS = 250
AGGREGATION_GAS = 1_000
HEALTH_GAS = 100
CONFIGURATION_GAS = 1_000

# Replaced by tools/build_actor.py in dist/main.py for deployment.
INDEX_HTML = b""
STYLES_CSS = b""
APP_JS = b""


def _json_bytes(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _parse(payload):
    if isinstance(payload, (bytes, bytearray)) and payload:
        try:
            value = json.loads(bytes(payload).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _num(value):
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except Exception:
        return 0


def _side(value):
    return "SHORT" if str(value).lower() == "short" else "LONG"


def _response(status, headers, body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return codec.encode(
        {"status": int(status), "headers": headers, "body": body or b""}
    )


@actor
class HlUiHost:
    def init(self, payload):
        if self.storage.get("__initialized__"):
            return b"ok: already initialized"
        runtime.assume_ownership_if_unowned()
        config = _parse(payload)
        self.storage["__initialized__"] = b"1"
        self.storage["__name__"] = "hl-ui-host"
        self.storage["__description__"] = "Live Hyperliquid trader dashboard host"
        self.storage["__version__"] = "2.0.0"
        if config.get(KEY_RESEARCH):
            self.storage[KEY_RESEARCH] = str(config[KEY_RESEARCH])
        if config.get(KEY_TRADER):
            self.storage[KEY_TRADER] = str(config[KEY_TRADER])
        self.storage[KEY_NETWORK] = str(config.get(KEY_NETWORK, "Cowboy"))
        runtime.emit_event("hl_ui.init", {})
        return b"ok"

    def _read(self, target, handler, payload=None):
        # Source handlers declare decode="raw" and return JSON bytes. Keep the
        # call boundary raw and decode here; the caller codec must match the
        # callee declaration, and cowboy_sdk supports only raw and CBOR.
        value = _parse(
            call(target, handler, _json_bytes(payload or {}), CALL_CYCLES, codec="raw")
        )
        if not isinstance(value, dict):
            raise ValueError(handler + ": expected JSON object")
        return value

    @public(decode="raw")
    @get("/ui/")
    @get("/ui/index.html")
    @pure
    def index(self, payload):
        return _response(
            200,
            [
                ("content-type", "text/html; charset=utf-8"),
                ("cache-control", "public, max-age=300"),
                ("x-content-type-options", "nosniff"),
            ],
            INDEX_HTML,
        )

    @public(decode="raw")
    @get("/ui/styles.css")
    @pure
    def styles(self, payload):
        return _response(
            200,
            [
                ("content-type", "text/css; charset=utf-8"),
                ("cache-control", "public, max-age=300"),
                ("x-content-type-options", "nosniff"),
            ],
            STYLES_CSS,
        )

    @public(decode="raw")
    @get("/ui/app.js")
    @pure
    def app(self, payload):
        return _response(
            200,
            [
                ("content-type", "application/javascript; charset=utf-8"),
                ("cache-control", "public, max-age=300"),
                ("x-content-type-options", "nosniff"),
            ],
            APP_JS,
        )

    @public(decode="raw")
    @get("/ui/api/dashboard")
    @pure
    def dashboard(self, payload):
        runtime.charge_gas(DASHBOARD_GAS)
        research = self.storage.get(KEY_RESEARCH)
        trader = self.storage.get(KEY_TRADER)
        if not research or not trader:
            return _response(
                503,
                JSON_HEADERS,
                _json_bytes(
                    {
                        "error": "dashboard actors are not configured",
                        "researchActor": research or None,
                        "traderActor": trader or None,
                    }
                ),
            )
        return _response(
            200,
            JSON_HEADERS,
            _json_bytes(
                {
                    "mode": "live",
                    "network": str(self.storage.get(KEY_NETWORK) or "Cowboy"),
                    "updatedBlock": runtime.get_block_height(),
                    "researchActor": research,
                    "traderActor": trader,
                }
            ),
        )

    @public(decode="raw")
    @get("/ui/api/research")
    @pure
    def research_dashboard(self, payload):
        """Research summary with exactly three nested actor reads."""
        runtime.charge_gas(AGGREGATION_GAS)
        research = self.storage.get(KEY_RESEARCH)
        if not research:
            return _response(
                503, JSON_HEADERS, b'{"error":"research actor is not configured"}'
            )

        errors = []

        def read(handler, args=None):
            try:
                return self._read(research, handler, args)
            except Exception as exc:
                errors.append(handler + ": " + str(exc))
                return {}

        research_stats = read("get_stats")
        theses = read("get_theses", {"active_only": True})
        ideas = read("get_ideas", {"limit": 20})

        idea_rows = [
            {
                "asset": str(row.get("asset", "")),
                "theme": str(row.get("theme", "")),
                "side": _side(row.get("side")),
                "confidence": _num(row.get("confidence")),
                "rationale": str(row.get("rationale", "")),
            }
            for row in _list(ideas.get("ideas"))
            if isinstance(row, dict)
        ]

        thesis_rows = [
            {
                "statement": str(row.get("statement", "")),
                "conviction": _num(row.get("conviction")),
                "context": str(row.get("context", "")),
                "longs": [str(v) for v in _list(row.get("long"))],
                "shorts": [str(v) for v in _list(row.get("short"))],
            }
            for row in _list(theses.get("theses"))
            if isinstance(row, dict)
        ]

        data = {
            "stats": {
                "universe": _num(research_stats.get("universe")),
                "corpus": _num(research_stats.get("window")),
                "theses": _num(research_stats.get("active_theses", len(thesis_rows))),
                "ideas": _num(ideas.get("count", len(idea_rows))),
            },
            "runs": {
                "ingest": _num(research_stats.get("ingest_runs")),
                "synth": _num(research_stats.get("synth_runs")),
                "validate": _num(research_stats.get("validate_runs")),
                "distill": _num(research_stats.get("distill_runs")),
            },
            "theses": thesis_rows,
            "ideas": idea_rows,
        }
        if errors:
            data["connectionError"] = "; ".join(errors)
        return _response(200, JSON_HEADERS, _json_bytes(data))

    @public(decode="raw")
    @get("/ui/api/research/lessons")
    @pure
    def research_lessons(self, payload):
        """Optional wiki data isolated to one nested research read."""
        research = self.storage.get(KEY_RESEARCH)
        if not research:
            return _response(
                503, JSON_HEADERS, b'{"error":"research actor is not configured"}'
            )
        try:
            wiki = self._read(research, "get_wiki")
        except Exception:
            wiki = {}
        lessons = []
        for lesson in _list(wiki.get("lessons")):
            text = (
                lesson if isinstance(lesson, str) else _object(lesson).get("text", "")
            )
            if text:
                lessons.append(str(text))
        return _response(200, JSON_HEADERS, _json_bytes({"lessons": lessons}))

    @public(decode="raw")
    @get("/ui/api/trader")
    @pure
    def trader_dashboard(self, payload):
        """Trader summary with exactly three nested actor reads."""
        runtime.charge_gas(AGGREGATION_GAS)
        trader = self.storage.get(KEY_TRADER)
        if not trader:
            return _response(
                503, JSON_HEADERS, b'{"error":"trader actor is not configured"}'
            )

        errors = []

        def read(handler, optional=False):
            try:
                return self._read(trader, handler)
            except Exception as exc:
                if not optional:
                    errors.append(handler + ": " + str(exc))
                return {}

        portfolio = read("get_portfolio")
        pnl = read("get_pnl")
        scorecard = read("get_scorecard", optional=True)
        prices = _object(portfolio.get("last_prices"))
        account = _object(portfolio.get("account_state"))
        venue_counts = _object(account.get("positions_by_venue"))
        position_rows = []
        for row in _list(portfolio.get("positions")):
            if not isinstance(row, dict):
                continue
            asset = str(row.get("asset", ""))
            entry = _num(row.get("entry_px"))
            mark_value = row.get("mark_px")
            if mark_value is None:
                mark_value = prices.get(asset.upper())
            if mark_value is None:
                # Missing market data is unknown performance, not a zero-dollar
                # mark (which would fabricate -100% long / +100% short returns).
                mark_value = entry
            mark = _num(mark_value)
            direction = _side(row.get("side"))
            return_pct = ((mark - entry) / entry) * 100 if entry else 0
            if direction == "SHORT":
                return_pct *= -1
            position_rows.append(
                {
                    "asset": asset,
                    "side": direction,
                    "entry": entry,
                    "mark": mark,
                    "quantity": _num(row.get("qty")),
                    "notional": _num(row.get("notional")),
                    "returnPct": return_pct,
                    "venue": str(row.get("venue", "core")),
                }
            )

        data = {
            "stats": {
                "positions": _num(pnl.get("open_positions", len(position_rows))),
                "equity": _num(pnl.get("equity")),
                "realizedPnl": _num(pnl.get("realized_pnl")),
                "unrealizedPnl": _num(pnl.get("unrealized_pnl")),
            },
            "runs": {"rebalances": _num(pnl.get("rebalances"))},
            "positions": position_rows,
            "account": {
                "mode": str(account.get("mode", "unknown")),
                "network": str(account.get("network", "unknown")),
                "spotUsdc": _num(account.get("spot_usdc")),
                "hold": _num(account.get("hold")),
                "available": _num(account.get("available")),
                "reconciledBlock": _num(account.get("reconciled_block")),
                "corePositions": _num(venue_counts.get("core")),
                "xyzPositions": _num(venue_counts.get("xyz")),
                "healthy": _num(account.get("reconciled_block")) > 0
                and account.get("equity") is not None,
            },
            "unfillable": [
                {
                    "asset": str(row.get("asset", "")),
                    "attempts": _num(row.get("attempts")),
                    "reason": str(row.get("reason", "")),
                }
                for row in _list(scorecard.get("unfillable"))
                if isinstance(row, dict)
            ],
        }
        if errors:
            data["connectionError"] = "; ".join(errors)
        return _response(200, JSON_HEADERS, _json_bytes(data))

    @public(decode="raw")
    @get("/ui/api/health")
    @pure
    def health(self, payload):
        runtime.charge_gas(HEALTH_GAS)
        configured = bool(
            self.storage.get(KEY_RESEARCH) and self.storage.get(KEY_TRADER)
        )
        return _response(
            200 if configured else 503,
            JSON_HEADERS,
            _json_bytes(
                {
                    "ok": configured,
                    "researchActor": self.storage.get(KEY_RESEARCH) or None,
                    "traderActor": self.storage.get(KEY_TRADER) or None,
                }
            ),
        )

    @callable_by(OWNER, decode="raw")
    def set_actors(self, payload):
        runtime.charge_gas(CONFIGURATION_GAS)
        config = _parse(payload)
        research = str(config.get(KEY_RESEARCH, ""))
        trader = str(config.get(KEY_TRADER, ""))
        if not research or not trader:
            raise ValueError("set_actors: research_actor and trader_actor are required")
        self.storage[KEY_RESEARCH] = research
        self.storage[KEY_TRADER] = trader
        if config.get(KEY_NETWORK):
            self.storage[KEY_NETWORK] = str(config[KEY_NETWORK])
        runtime.emit_event(
            "hl_ui.actors_updated", {"research": research, "trader": trader}
        )
        return b"ok"

    @callable_by(OWNER, decode="raw")
    def set_routes(self, payload):
        runtime.charge_gas(CONFIGURATION_GAS)
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise ValueError("set_routes: empty payload")
        self.storage.set_raw(ROUTES_KEY, bytes(payload))
        runtime.emit_event("hl_ui.routes_installed", {"bytes": len(payload)})
        return b"ok"


def _get():
    return HlUiHost()


def init(payload):
    return _get().init(payload)


def dashboard(payload):
    return _get().dashboard(payload)


def research_dashboard(payload):
    return _get().research_dashboard(payload)


def research_lessons(payload):
    return _get().research_lessons(payload)


def trader_dashboard(payload):
    return _get().trader_dashboard(payload)


def health(payload):
    return _get().health(payload)


def index(payload):
    return _get().index(payload)


def styles(payload):
    return _get().styles(payload)


def app(payload):
    return _get().app(payload)


def set_actors(payload):
    return _get().set_actors(payload)


def set_routes(payload):
    return _get().set_routes(payload)
