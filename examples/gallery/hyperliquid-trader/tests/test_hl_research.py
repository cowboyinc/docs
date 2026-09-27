"""Unit tests for the research actor's deterministic logic: RSS parse/dedup, the
retrieval-context seam, and parsing the LLM synthesis output into a thesis +
ideas. The judgment that runs on-chain, auditably.

cowboy_sdk targets the PVM's RustPython runtime, so under CPython we stub it in
sys.modules to let the module import; the functions under test touch none of it.
"""

import base64
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

_fake = types.ModuleType("cowboy_sdk")


def _identity(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def _wrap(fn):
        return fn

    return _wrap


class _Runner:
    def continuation(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn

    async def http(self, *a, **k):
        return {}

    async def llm(self, *a, **k):
        return {}


class _AddressError(Exception):
    pass


def _to_address_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        decoded = bytes(value)
    elif isinstance(value, str):
        raw = value[2:] if value.startswith(("0x", "0X")) else value
        if len(raw) != 40:
            raise _AddressError("address must have 40 hex characters")
        try:
            decoded = bytes.fromhex(raw)
        except ValueError as exc:
            raise _AddressError("address must be hex") from exc
    else:
        raise _AddressError("unsupported address")
    if len(decoded) != 20:
        raise _AddressError("address must be 20 bytes")
    return decoded


class _VB:
    def mode(self, *a, **k):
        return self

    def runners(self, *a, **k):
        return self

    def threshold(self, *a, **k):
        return self

    def check(self, *a, **k):
        return self

    def build(self, *a, **k):
        return {}


class _Verify:
    @staticmethod
    def builder():
        return _VB()

    @staticmethod
    def semantic_similarity(*a, **k):
        return {}


def _bounded_loop(*a, **k):
    def _wrap(fn):
        return fn

    return _wrap


_fake.Verify = _Verify
_fake.bounded_loop = _bounded_loop
_fake.BoundedRange = lambda x: range(x) if isinstance(x, int) else x
_fake.actor = _identity
_fake.public = _identity
_fake.pure = _identity
_fake.deferred = _identity
_fake.callable_by = _identity
_fake.OWNER = object()
_fake.SELF = object()
_fake.runner = _Runner()
_fake.capture = lambda: types.SimpleNamespace()
_fake.runtime = types.SimpleNamespace(
    get_block_height=lambda: 100,
    get_timestamp_ms=lambda: 1_700_000_000_000,
    schedule_timer_ex=lambda *args, **kwargs: None,
)
_fake.AddressError = _AddressError
_fake.to_address_bytes = _to_address_bytes
_fake.codec = types.SimpleNamespace(
    encode=lambda value: value, decode=lambda value: value
)
_fake.send = lambda *args, **kwargs: None
sys.modules["cowboy_sdk"] = _fake
_fake_routes = types.ModuleType("cowboy_sdk.routes")
_fake_routes.get = _identity
_fake_routes.post = _identity
sys.modules["cowboy_sdk.routes"] = _fake_routes

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXAMPLE_DIR))

import hl_research as r  # noqa: E402

SAMPLE_RSS = """<rss><channel>
  <item><title>AI chipmaker soars</title><link>https://ex.com/ai-1</link>
    <description><![CDATA[<p>Nvidia up 5% &amp; strong.</p>]]></description></item>
  <item><title>Bitcoin dips</title><link>https://ex.com/btc-1</link>
    <description>Crypto weakness.</description></item>
</channel></rss>"""

THESIS = {"long": ["AI"], "short": ["crypto"]}


# ---- ingest helpers -------------------------------------------------------- #


def test_strip_html():
    assert r._strip_html("<![CDATA[<p>hi &amp; bye</p>]]>") == "hi & bye"
    assert r._strip_html("<b>a</b>  b") == "a b"


def test_parse_rss():
    items = r._parse_rss(SAMPLE_RSS)
    assert len(items) == 2
    assert items[0]["title"] == "AI chipmaker soars"
    assert items[0]["url"] == "https://ex.com/ai-1"
    assert "Nvidia up 5% & strong" in items[0]["summary"]
    assert r._parse_rss("<html>nope</html>") == []


def test_parse_rss_without_regex_dependent_item_matching():
    atom = (
        "<feed><entry data-x='1'><title>Macro update</title>"
        "<link rel='alternate' href='https://ex.com/macro'/><summary>"
        "Rates <b>fell</b>.</summary></entry></feed>"
    )
    assert r._parse_rss(atom) == [
        {
            "title": "Macro update",
            "url": "https://ex.com/macro",
            "summary": "Rates fell .",
        }
    ]


def test_url_key():
    assert r._url_key("https://Ex.com/Path/") == "ex.com/path"
    assert r._url_key("http://ex.com/a#frag") == "ex.com/a"


def test_stage_items():
    cfg = {"max_items_per_feed": 15, "max_seen": 500}
    new_seen, cand, n = r.stage_items(SAMPLE_RSS, cfg, [], [], "feedA", 7)
    assert n == 2 and len(cand) == 2
    assert cand[0]["feed"] == "feedA" and cand[0]["block"] == 7
    # already-seen URL is skipped; fresh items append to the existing pile
    seen = [r._url_key("https://ex.com/ai-1")]
    _s, cand2, n2 = r.stage_items(SAMPLE_RSS, cfg, seen, cand, "feedB", 8)
    assert n2 == 1 and len(cand2) == 3  # only btc-1 was new


# ---- scoring gate ---------------------------------------------------------- #


def test_parse_scores():
    assert r.parse_scores("8,3,9,5", 4) == [8, 3, 9, 5]
    assert r.parse_scores("8,3", 4) == [8, 3, 0, 0]  # pad short
    assert r.parse_scores("12,0", 2) == [10, 0]  # clamp to 0-10
    env = {"raw": base64.b64encode(b"9,2,7").decode()}  # runner base64 envelope
    assert r.parse_scores(env, 3) == [9, 2, 7]


def test_score_prompt():
    p = r.score_prompt(
        [{"title": "AI up", "summary": "nvidia"}], ["BTC", "NVDA"], "risk-on"
    )
    assert "BTC, NVDA" in p and "risk-on" in p and "1. AI up" in p


# ---- retrieval seam -------------------------------------------------------- #


def test_context_for_synthesis():
    items = [{"title": "AI up", "summary": "nvidia soars", "url": "x", "block": 1}]
    active = [{"statement": "long AI", "status": "active"}]
    ctx = json.loads(r._context_for_synthesis(items, ["BTC"], active))
    assert ctx["assets_of_interest"] == ["BTC"]
    assert ctx["existing_theses"] == ["long AI"]
    assert ctx["items"] == [
        {"title": "AI up", "summary": "nvidia soars"}
    ]  # url/block dropped


# ---- reality-check (hypothetical P&L) -------------------------------------- #


def test_resolve_prices():
    # multi-dex: core crypto book (dex="") has bare keys; builder dexs namespace as dex:SYMBOL
    core = {"data": {"BTC": "61000.0", "ETH": "3000.5", "DOGE": "0.1"}}
    xyz = {"xyz:NVDA": "210.1", "xyz:TSLA": "408.4"}
    prices = r.resolve_prices(
        ["BTC", "ETH", "NVDA", "TSLA"], [("", core), ("xyz", xyz)]
    )
    assert prices == {"BTC": 61000.0, "ETH": 3000.5, "NVDA": 210.1, "TSLA": 408.4}
    assert r.resolve_prices(["btc"], [("", '{"BTC":"5"}')]) == {
        "BTC": 5.0
    }  # lowercase + json str
    assert r.resolve_prices(["BTC"], [("", {})]) == {}  # missing → skipped


def test_universe_symbols():
    cfg = {"coins": ["BTC", "eth"], "stocks": ["NVDA"], "preipo": ["OPENAI", "BTC"]}
    assert r.universe_symbols(cfg) == [
        "BTC",
        "ETH",
        "NVDA",
        "OPENAI",
    ]  # upper, dedup, ordered


def test_mids_query():
    assert json.loads(r.mids_query("")) == {"type": "allMids"}
    assert json.loads(r.mids_query("xyz")) == {"type": "allMids", "dex": "xyz"}


def test_thesis_direction():
    t = {"long": ["AI", "NVDA"], "short": ["crypto"]}
    assert r._thesis_direction(t, "BTC") == -1  # short crypto → short BTC (theme)
    assert r._thesis_direction(t, "NVDA") == 1  # explicit long wins over theme
    t2 = {"long": ["BTC"], "short": ["ETH"]}
    assert r._thesis_direction(t2, "BTC") == 1
    assert r._thesis_direction(t2, "ETH") == -1
    assert r._thesis_direction(t2, "SOL") == 0


def test_thesis_return():
    baseline = {"BTC": 60000.0, "ETH": 3000.0}
    current = {"BTC": 63000.0, "ETH": 3000.0}  # BTC +5%, ETH flat
    # short crypto → BTC contributes -5%, ETH 0 → avg -2.5%
    assert (
        abs(
            r._thesis_return({"long": [], "short": ["crypto"]}, baseline, current)
            + 0.025
        )
        < 1e-9
    )
    # long crypto → +2.5%
    assert (
        abs(
            r._thesis_return({"long": ["crypto"], "short": []}, baseline, current)
            - 0.025
        )
        < 1e-9
    )
    # no priceable position → 0
    assert r._thesis_return({"long": ["AI"], "short": []}, baseline, current) == 0.0


# ---- synthesis parsing ----------------------------------------------------- #


def test_parse_synthesis():
    analysis = {
        "economic_context": "Risk-on tape, Fed on hold, AI capex strong",
        "thesis": {
            "statement": "long AI, short crypto",
            "long": ["ai"],
            "short": ["crypto"],
            "conviction": 72,
            "rationale": "compute demand",
        },
        "ideas": [
            {
                "asset": "nvda",
                "theme": "ai",
                "side": "LONG",
                "confidence": 80,
                "rationale": "x",
            },
            {
                "asset": "btc",
                "theme": "crypto",
                "side": "sideways",
            },  # invalid side → dropped
            "junk",
        ],
    }
    thesis, ideas = r._parse_synthesis(analysis)
    assert thesis["long"] == ["AI"] and thesis["short"] == ["CRYPTO"]
    assert thesis["conviction"] == 72
    assert (
        thesis["context"] == "Risk-on tape, Fed on hold, AI capex strong"
    )  # macro backdrop captured
    assert (
        len(ideas) == 1 and ideas[0]["asset"] == "NVDA" and ideas[0]["side"] == "long"
    )


def test_wiki_cases():
    archive = [
        {
            "long": ["NVDA"],
            "short": ["ETH"],
            "final_return": -0.02,
            "context": "risk-off, rates rising",
        },
        {
            "long": ["BTC"],
            "short": [],
            "final_return": 0.03,
            "context": "",
        },  # no context → skipped
    ]
    cases = r._wiki_cases(archive)
    assert len(cases) == 1  # empty-context one dropped
    assert cases[0]["context"] == "risk-off, rates rising"
    assert cases[0]["return_pct"] == -2.0 and cases[0]["long"] == ["NVDA"]
    # rides into synthesis as past_cases
    ctx = json.loads(
        r._context_for_synthesis([], ["BTC"], [], "", None, None, None, cases)
    )
    assert ctx["past_cases"] == cases


def test_parse_lessons():
    assert r.parse_lessons({"lessons": ["a", "b", "c"]}, 2) == [
        "a",
        "b",
    ]  # capped at limit
    assert r.parse_lessons('{"lessons":["x","y"]}', 5) == ["x", "y"]  # from json string
    assert r.parse_lessons({"nope": 1}, 5) == []  # no lessons key
    assert r.parse_lessons("garbage", 5) == []
    env = {"raw": base64.b64encode(b'{"lessons":["z"]}').decode()}  # runner envelope
    assert r.parse_lessons(env, 5) == ["z"]


def test_distill_prompt():
    archive = [
        {
            "context": "risk-off, rates rising",
            "long": ["NVDA"],
            "short": ["ETH"],
            "final_return": -0.02,
        }
    ]
    p = r.distill_prompt(archive, ["an old lesson"], 15)
    assert "risk-off, rates rising" in p and "an old lesson" in p and '{"lessons"' in p
    # lessons ride into synthesis
    ctx = json.loads(
        r._context_for_synthesis(
            [], ["BTC"], [], "", None, None, None, None, ["lesson one"]
        )
    )
    assert ctx["lessons"] == ["lesson one"]


def test_parse_synthesis_garbage():
    assert r._parse_synthesis("not json") == (None, [])
    assert r._parse_synthesis({"nope": 1}) == (None, [])


def test_thesis_fit():
    assert (
        r._thesis_fit({"asset": "BTC", "theme": "crypto", "side": "short"}, THESIS)
        == "aligns"
    )
    assert (
        r._thesis_fit({"asset": "BTC", "theme": "crypto", "side": "long"}, THESIS)
        == "contradicts"
    )
    assert (
        r._thesis_fit({"asset": "NVDA", "theme": "AI", "side": "long"}, THESIS)
        == "aligns"
    )
    assert (
        r._thesis_fit({"asset": "CL", "theme": "energy", "side": "long"}, THESIS)
        == "neutral"
    )


def test_normalize_result():
    import base64

    assert r._normalize_result('{"a": 1}') == {"a": 1}  # plain JSON string
    assert r._normalize_result({"raw": base64.b64encode(b'{"b": 2}').decode()}) == {
        "b": 2
    }  # runner {"raw":<base64>}
    assert r._normalize_result({"data": '{"d": 4}'}) == {
        "d": 4
    }  # data-wrapped (price path)
    assert r._normalize_result({"content": [{"text": '{"c": 3}'}]}) == {
        "c": 3
    }  # LLM content-list
    assert r._normalize_result({"BTC": "64000"}) == {
        "BTC": "64000"
    }  # already-parsed dict -> as-is
    assert r._normalize_result("plain") == {"raw_text": "plain"}  # non-JSON -> raw_text


def test_thesis_outcomes_feedback():
    theses = [
        {
            "statement": "long AI, short crypto",
            "status": "active",
            "last_return": 0.031,
        },
        {"statement": "long crypto", "status": "retired", "final_return": -0.022},
        {"statement": "long SOL", "status": "active"},  # ungraded → dropped
    ]
    oc = r._thesis_outcomes(theses)
    assert [o["statement"] for o in oc] == ["long AI, short crypto", "long crypto"]
    assert oc[0]["return_pct"] == 3.1 and oc[0]["status"] == "active"
    assert oc[1]["return_pct"] == -2.2 and oc[1]["status"] == "retired"
    # the feedback rides into the synthesis context as track_record
    ctx = json.loads(r._context_for_synthesis([], ["BTC"], [], "", oc))
    assert ctx["track_record"] == oc
    assert "track_record" not in json.loads(
        r._context_for_synthesis([], ["BTC"], [])
    )  # absent by default


def test_wiki_aggregate():
    hist = [
        {
            "long": ["BTC"],
            "short": ["ETH"],
            "status": "retired",
            "final_return": 0.02,
        },  # BTC long +2, ETH short +2
        {
            "long": ["BTC"],
            "short": [],
            "status": "retired",
            "final_return": -0.04,
        },  # BTC long -4
        {
            "long": ["BTC"],
            "short": [],
            "status": "active",
            "last_return": 0.01,
        },  # BTC long +1
        {"long": ["SOL"], "short": [], "status": "active"},  # ungraded → skipped
    ]
    agg = {(a["asset"], a["side"]): a for a in r._wiki_aggregate(hist)}
    btc = agg[("BTC", "long")]
    assert btc["n"] == 3
    assert (
        abs(btc["avg_return_pct"] - round((2 - 4 + 1) / 3, 2)) < 1e-9
    )  # (+2 -4 +1)/3 %
    assert btc["win_rate"] == round(2 / 3, 2)  # 2 of 3 positive
    assert (
        agg[("ETH", "short")]["n"] == 1
        and agg[("ETH", "short")]["avg_return_pct"] == 2.0
    )
    assert ("SOL", "long") not in agg  # ungraded excluded
    # rides into synthesis as long_term_record
    ctx = json.loads(
        r._context_for_synthesis(
            [], ["BTC"], [], "", None, None, r._wiki_aggregate(hist)
        )
    )
    assert "long_term_record" in ctx


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")


def _fake_meta_ctxs(rows):
    # rows: [(name, vol)] — the runner delivers JSON responses PARSED, so
    # metaAndAssetCtxs arrives as a naked [meta, ctxs] list.
    return [
        {"universe": [{"name": "xyz:" + n} for n, _ in rows]},
        [{"dayNtlVlm": str(v)} for _, v in rows],
    ]


def test_discover_stocks_screens_by_volume_and_caps():
    res = _fake_meta_ctxs([("MU", 420), ("SNDK", 239), ("INTC", 0), ("META", 337)])
    merged, added, kept = r.discover_stocks(res, ["MSFT"], [], 50, 2)
    assert merged == ["MSFT", "MU", "META"]  # pinned first, top-2 by vol
    assert added == 2 and kept == 0


def test_discover_stocks_never_drops_held_and_survives_garbage():
    res = _fake_meta_ctxs([("MU", 420)])
    merged, added, kept = r.discover_stocks(res, ["MSFT"], ["AMZN"], 50, 5)
    assert "AMZN" in merged and kept == 1  # held name re-added
    m2, a2, k2 = r.discover_stocks({"value": "junk"}, ["MSFT"], [], 50, 5)
    assert m2 == ["MSFT"] and a2 == 0  # bad payload -> pinned only


def test_notify_tg_encode():
    import hl_notify as n

    assert n.tg_encode("BTC +1.5% & up") == "BTC%20%2B1.5%25%20%26%20up"


def test_notify_address_normalizer_rejects_malformed_widths():
    import hl_notify as n

    assert n._normalized_address("0x" + "  " + "ab" * 19) == ""
    assert n._normalized_address(b"abcde") == ""
    assert n._normalized_address("  0X" + "AB" * 20 + "  ") == "0x" + "ab" * 20


def test_compose_brief_reports_only_stored_state():
    theses = [
        {"status": "active", "statement": "Risk-on tilt", "context": "Fed easing"}
    ]
    ideas = [{"asset": "BTC", "side": "long", "confidence": 70}]
    pnl = {"open_positions": 1, "equity": 999.76, "realized_pnl": 0.0}
    b = r.compose_brief(None, theses, ideas, pnl, ["BTC", "NVDA"])
    assert (
        "Risk-on tilt" in b and "BTC long(70)" in b and "$999.76" in b and "NVDA" in b
    )


def test_guidance_lands_in_synthesis_context():
    import json as _json

    ctx = _json.loads(
        r._context_for_synthesis(
            [], ["BTC"], [], guidance=[{"text": "go easy on crypto"}]
        )
    )
    assert ctx["operator_guidance"] == ["go easy on crypto"]
    assert "operator_guidance" not in _json.loads(
        r._context_for_synthesis([], ["BTC"], [])
    )


def test_notify_brief_delivery_rotates_guidance():
    import hl_notify as n

    n.runtime.emit_event = lambda *a, **k: None
    n.runtime.get_block_height = lambda: 100
    a = n.HlNotify()
    a.storage = {"guidance": [{"text": "old", "block": 1}]}
    a._after_notify(brief=True)  # brief delivered -> window closes
    assert a.storage["guidance"] == []
    a.storage["guidance"] = [{"text": "kept", "block": 2}]
    a._after_notify(brief=False)  # plain reply keeps guidance
    assert a.storage["guidance"] == [{"text": "kept", "block": 2}]


def test_brief_slot_partitions_the_local_day():
    DAY = 86400 * 1000
    base = 20000 * DAY  # an arbitrary day, ts in UTC ms
    off = -4  # EDT

    def at(hour_local):  # ts for a local wall-clock hour
        return base + (hour_local + 4) * 3600 * 1000

    assert r.brief_slot(at(8.5), off, 9, 17) == (None, None)  # pre-morning
    k1, m1 = r.brief_slot(at(9.0), off, 9, 17)
    assert m1 == "morning"
    k2, m2 = r.brief_slot(at(16.9), off, 9, 17)
    assert (k2, m2) == (k1, "morning")
    k3, m3 = r.brief_slot(at(17.0), off, 9, 17)
    assert m3 == "evening" and k3 != k1
    k4, _ = r.brief_slot(at(9.0) + DAY, off, 9, 17)
    assert k4 != k1  # next day: new key


def test_eastern_offset_tracks_both_dst_transition_boundaries():
    def utc_ms(year, month, day, hour, minute=0):
        return int(
            datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
            * 1000
        )

    assert r.eastern_utc_offset_hours(utc_ms(2026, 3, 8, 6, 59)) == -5
    assert r.eastern_utc_offset_hours(utc_ms(2026, 3, 8, 7, 0)) == -4
    assert r.eastern_utc_offset_hours(utc_ms(2026, 11, 1, 5, 59)) == -4
    assert r.eastern_utc_offset_hours(utc_ms(2026, 11, 1, 6, 0)) == -5

    summer = utc_ms(2026, 7, 15, 13)
    winter = utc_ms(2026, 1, 15, 14)
    assert (
        r.brief_slot(summer, r.eastern_utc_offset_hours(summer), 9, 17)[1] == "morning"
    )
    assert (
        r.brief_slot(winter, r.eastern_utc_offset_hours(winter), 9, 17)[1] == "morning"
    )


def test_brief_dst_default_preserves_explicit_fixed_offset():
    assert r.research_cfg({})["brief_auto_dst"] is True
    assert r.research_cfg({"brief_utc_offset_hours": -7})["brief_auto_dst"] is False
    assert (
        r.research_cfg(
            {
                "brief_utc_offset_hours": -7,
                "brief_auto_dst": True,
            }
        )["brief_auto_dst"]
        is True
    )


def test_notify_status_distinguishes_ideas_from_research_window():
    import hl_notify as n

    a = n.HlNotify()
    a.storage = {
        "cached_research_stats": {"total_theses": 12, "window": 9},
        "cached_ideas": [{"asset": str(i)} for i in range(6)],
        "cached_pnl": {"open_positions": 3, "equity": 1000, "realized_pnl": 4},
    }
    status = a._answer("/status")
    assert "ideas 6" in status
    assert "research window 9" in status


def test_notify_receiver_authenticates_snapshots_and_notifications():
    import hl_notify as n

    research = b"\x11" * 20
    trader = b"\x22" * 20
    n.runtime.get_sender = lambda: trader
    a = n.HlNotify()
    a.storage = {
        "cfg": {"max_outbox": 20, "max_guidance": 12},
        "research_addr": "0x" + research.hex(),
        "trader_addr": "0x" + trader.hex(),
        "outbox": [],
    }
    snapshot = json.dumps(
        {
            "type": "command_snapshot",
            "research_stats": {"window": 4},
            "ideas": [{"asset": "BTC"}],
            "pnl": {"equity": 1000},
            "portfolio": {"positions": []},
        }
    ).encode()
    assert a.handle_message(snapshot) == b"stored"
    assert a.storage["cached_ideas"] == [{"asset": "BTC"}]

    n.runtime.get_sender = lambda: b"\x33" * 20
    assert a.handle_message(snapshot) == b"ignored: unknown trader"
    assert a.storage["cached_research_stats"] == {"window": 4}

    n.runtime.get_sender = lambda: research
    notification = json.dumps(
        {
            "type": "notification",
            "text": "morning",
            "kind": "brief",
        }
    ).encode()
    assert a.handle_message(notification) == b"queued"
    assert a.storage["outbox"] == [{"text": "morning", "kind": "brief"}]

    n.runtime.get_sender = lambda: b"\x33" * 20
    assert a.handle_message(notification) == b"ignored: unknown research"
    assert len(a.storage["outbox"]) == 1


def test_message_command_is_queued_only_for_a_legacy_self_message(monkeypatch):
    import hl_notify as n

    a = n.HlNotify()
    a.storage = {
        "cfg": {
            "max_outbox": 20,
            "max_guidance": 12,
            "secret_account": "0x" + "33" * 20,
        },
        "outbox": [],
        "cached_portfolio": {"positions": []},
    }
    a._notify_one = lambda _payload: pytest.fail("spawned a child from handle_message")
    own = b"\x11" * 20
    monkeypatch.setattr(n.runtime, "get_actor_address", lambda: own, raising=False)
    monkeypatch.setattr(n.runtime, "get_sender", lambda: b"\x22" * 20, raising=False)
    assert a.handle_message(b'{"text":"/book"}') == b"ignored: unknown text sender"
    assert a.storage["outbox"] == []
    monkeypatch.setattr(n.runtime, "get_sender", lambda: own)
    assert a.handle_message(b'{"text":"/book"}') == b"queued"
    assert a.storage["outbox"] == [{"text": "book: empty", "kind": "reply"}]


def test_research_receiver_authenticates_trader_and_guidance_snapshots():
    r.runtime.emit_event = lambda *a, **k: None
    trader = b"\x11" * 20
    notifier = b"\x22" * 20
    a = r.HyperliquidResearch()
    a.storage = {
        "exec_addr": "0x" + trader.hex(),
        "notify_addr": "0x" + notifier.hex(),
    }
    r.runtime.get_sender = lambda: trader
    trader_snapshot = json.dumps(
        {
            "type": "trader_snapshot",
            "scorecard": [{"asset": "BTC"}],
            "unfillable": [{"asset": "XYZ"}],
            "pnl": {"equity": 1001},
        }
    ).encode()
    assert a.handle_message(trader_snapshot) == b"stored"
    assert a.storage["last_scorecard"] == [{"asset": "BTC"}]
    assert a.storage["last_pnl"] == {"equity": 1001}

    r.runtime.get_sender = lambda: notifier
    guidance = json.dumps(
        {
            "type": "guidance_snapshot",
            "guidance": [{"text": "reduce beta", "block": 9}],
        }
    ).encode()
    assert a.handle_message(guidance) == b"stored"
    assert a.storage["last_guidance"][0]["text"] == "reduce beta"

    r.runtime.get_sender = lambda: b"\x33" * 20
    assert a.handle_message(trader_snapshot) == b"ignored: unknown trader"
    assert a.handle_message(guidance) == b"ignored: unknown notifier"
    assert a.storage["last_scorecard"] == [{"asset": "BTC"}]


def test_research_sender_matcher_strictly_normalizes_expected_address(monkeypatch):
    sender = b"\x11" * 20
    monkeypatch.setattr(r.runtime, "get_sender", lambda: sender, raising=False)

    assert r._sender_matches("  0X" + sender.hex().upper() + "  ")
    assert not r._sender_matches("0x" + "  " + "11" * 19)
    assert not r._sender_matches("0x1234")
    assert not r._sender_matches("")


@pytest.mark.parametrize(
    ("elapsed_ms", "allowed"),
    [
        (60_000, False),
        (1_800_001, True),
    ],
)
def test_research_tick_guard_waits_then_releases_lost_work(
    monkeypatch, elapsed_ms, allowed
):
    now_ms = 1_700_000_000_000
    events = []
    timers = []
    monkeypatch.setattr(r.runtime, "get_block_height", lambda: 100, raising=False)
    monkeypatch.setattr(r.runtime, "get_timestamp_ms", lambda: now_ms, raising=False)
    monkeypatch.setattr(r.runtime, "get_sender", lambda: b"\x11" * 20, raising=False)
    monkeypatch.setattr(
        r.runtime,
        "emit_event",
        lambda name, body: events.append((name, body)),
        raising=False,
    )
    monkeypatch.setattr(
        r.runtime,
        "schedule_timer_ex",
        lambda *args, **kwargs: timers.append((args, kwargs)),
        raising=False,
    )
    actor = r.HyperliquidResearch()
    actor.storage = {
        "running": True,
        "cfg": {
            "ingest_interval_blocks": 149,
            "max_poll_wait_blocks": 300,
            "max_poll_wait_ms": 1_800_000,
        },
        "ingest_pending": True,
        "ingest_started_block": 99,
        "ingest_started_ms": now_ms - elapsed_ms,
    }

    assert actor._tick_ok("ingest", "ingest_interval_blocks") is allowed
    assert len(timers) == 1
    if allowed:
        assert actor.storage["ingest_started_block"] == 100
        assert actor.storage["ingest_started_ms"] == now_ms
        lost = [body for name, body in events if name == "research.continuation_lost"]
        assert lost[-1]["released_by"] == "clock"
    else:
        assert any(name == "research.skipped" for name, _body in events)


def test_webhook_requires_a_configured_secret_and_reports_missing_config_once(
    monkeypatch,
):
    import hl_notify as n

    a = n.HlNotify()
    a.storage = {"cfg": {"max_outbox": 20, "max_guidance": 12}, "chat_id": "42"}
    events = []
    monkeypatch.setattr(
        n.runtime, "emit_event", lambda *args: events.append(args), raising=False
    )
    request = {
        "headers": [],
        "body": json.dumps(
            {
                "update_id": 1,
                "message": {"chat": {"id": 42}, "text": "/book"},
            }
        ).encode(),
    }
    for _ in range(2):
        assert a.handle_webhook(request)["status"] == 401
    assert events == [("notify.webhook_unconfigured", {})]
    assert not a.storage.get("outbox") and not a.storage.get("guidance")
    assert not a.storage.get(n.KEY_SEEN_UPDATES)

    with pytest.raises(ValueError):
        a.set_webhook_secret(b'{"secret_token":"secret"}')
    secret_hash = n.hashlib.sha256(b"secret").hexdigest()
    assert (
        a.set_webhook_secret(json.dumps({"secret_hash": secret_hash}).encode()) == b"ok"
    )
    for headers in ([], [("x-telegram-bot-api-secret-token", "wrong")]):
        request["headers"] = headers
        assert a.handle_webhook(request)["status"] == 401
    assert not a.storage.get("outbox") and not a.storage.get(n.KEY_SEEN_UPDATES)
    request["headers"] = [("x-telegram-bot-api-secret-token", "secret")]
    response = a.handle_webhook(request)
    assert response["status"] == 200
    assert json.loads(response["body"]) == {
        "chat_id": "42",
        "method": "sendMessage",
        "text": "book: empty",
    }
    assert not a.storage.get("outbox")
    assert len(events) == 1


def test_webhook_only_mode_drains_burst_and_publishes_guidance(monkeypatch):
    import asyncio

    import hl_notify as n

    a = n.HlNotify()
    a.storage = {
        "cfg": {
            "max_outbox": 20,
            "max_guidance": 12,
            "secret_account": "0x" + "33" * 20,
        },
        "outbox": [],
        "chat_id": "42",
        "research_addr": "0x" + "22" * 20,
        n.KEY_WEBHOOK_SECRET_HASH: n.hashlib.sha256(b"secret").hexdigest(),
    }
    receiver = r.HyperliquidResearch()
    receiver.storage = {"cfg": {}, "notify_addr": "0x" + "11" * 20}
    height, timers, delivered, children = [100], [], [], []
    monkeypatch.setattr(n.runtime, "get_block_height", lambda: height[0], raising=False)
    monkeypatch.setattr(r.runtime, "get_block_height", lambda: height[0], raising=False)
    monkeypatch.setattr(r.runtime, "get_sender", lambda: b"\x11" * 20, raising=False)
    monkeypatch.setattr(n.runtime, "emit_event", lambda *args: None, raising=False)
    monkeypatch.setattr(
        n.runtime,
        "schedule_timer_ex",
        lambda block, payload, **kw: timers.append((block, json.loads(payload), kw)),
        raising=False,
    )

    async def http(url, **kwargs):
        assert "/sendMessage?" in url  # webhook mode never calls getUpdates
        assert kwargs["secrets"] == [["0x" + "33" * 20, n.TG_SECRET_KEY]]
        delivered.append(url)
        children.append(height[0])
        return {"ok": True}

    def send(target, payload):
        assert target == "0x" + "22" * 20
        children.append(height[0])
        assert receiver.handle_message(payload) == b"stored"

    monkeypatch.setattr(n.runner, "http", http)
    monkeypatch.setattr(n.runtime, "send_message", send, raising=False)
    responses = []
    for update_id, text in enumerate(["/book", "/ideas", "reduce beta"]):
        req = {
            "headers": [("x-telegram-bot-api-secret-token", "secret")],
            "body": json.dumps(
                {"update_id": update_id, "message": {"chat": {"id": 42}, "text": text}}
            ).encode(),
        }
        response = a.handle_webhook(req)
        assert response["status"] == 200
        responses.append(json.loads(response["body"]))
    assert responses[0]["method"] == "sendMessage"
    assert responses[0]["text"] == "book: empty"
    assert responses[1]["method"] == "sendMessage"
    assert responses[2] == {"ok": True}
    assert len(timers) == 1  # only guidance needs the deferred drain
    assert delivered == [] and receiver._cached_guidance() == []
    due, envelope, funding = timers.pop(0)
    assert due == 101 and funding.get("fee_payer") is None
    assert funding["access_limit"] == 322
    height[0] = 101
    result = getattr(a, envelope["_handler"])(b"{}")
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    assert children.count(101) == 1
    assert delivered == []
    assert receiver._cached_guidance() == [{"text": "reduce beta", "block": 100}]
    assert timers == [] and a.storage["webhook_drain_pending"] is False
    a.clear_guidance(b"{}")
    height[0] = 104
    assert len(timers) == 1
    timers.pop(0)
    a.webhook_drain_tick(b"{}")
    assert receiver._cached_guidance() == []
    assert timers == []
    # Autonomous notifications still use the CBSS-backed runner path. A
    # delivered brief also publishes the cleared guidance window.
    a.storage["guidance"] = [{"text": "old advice", "block": 104}]
    assert a._enqueue(b'{"text":"morning","kind":"brief"}') == b"queued"
    assert len(timers) == 1
    height[0] = 105
    due, envelope, _funding = timers.pop(0)
    assert due == 105
    result = getattr(a, envelope["_handler"])(b"{}")
    assert asyncio.iscoroutine(result)
    asyncio.run(result)
    assert len(delivered) == 1 and "morning" in delivered[0]
    assert len(timers) == 1
    height[0] = 106
    timers.pop(0)
    a.webhook_drain_tick(b"{}")
    assert receiver._cached_guidance() == [] and timers == []


def test_webhook_drain_retains_unconfigured_outbox_and_wakes_after_wiring(monkeypatch):
    import hl_notify as n

    a = n.HlNotify()
    a.storage = {
        "webhook_drain_pending": True,
        "outbox": [{"text": "book: empty", "kind": "reply"}],
    }
    timers = []
    monkeypatch.setattr(n.runtime, "emit_event", lambda *args: None, raising=False)
    monkeypatch.setattr(
        n.runtime,
        "schedule_timer_ex",
        lambda *args, **kw: timers.append((args, kw)),
        raising=False,
    )
    assert a.webhook_drain_tick(b"{}") == b"unconfigured"
    assert len(a.storage["outbox"]) == 1 and timers == []
    a.set_telegram(b'{"chat_id":"42"}')
    assert len(timers) == 1 and a.storage["webhook_drain_pending"] is True


def test_webhook_replies_inline_after_auth_and_dedup(monkeypatch):
    import hl_notify as n

    a = n.HlNotify()
    a.storage = {
        "cfg": {"max_outbox": 20, "max_guidance": 12},
        "outbox": [],
        "chat_id": "42",
        "cached_portfolio": {"positions": []},
        n.KEY_WEBHOOK_SECRET_HASH: n.hashlib.sha256(b"secret").hexdigest(),
    }
    a._notify_one = lambda _payload: pytest.fail("webhook spawned a runner job")
    monkeypatch.setattr(
        n.runtime,
        "send_message",
        lambda *a: pytest.fail("webhook sent a child"),
        raising=False,
    )

    def request(token="secret", chat="42", update_id=1):
        return {
            "headers": [("x-telegram-bot-api-secret-token", token)],
            "body": json.dumps(
                {
                    "update_id": update_id,
                    "message": {"chat": {"id": chat}, "text": "/book"},
                }
            ).encode(),
        }

    assert a.handle_webhook(request("wrong"))["status"] == 401
    assert a.storage["outbox"] == []
    response = a.handle_webhook(request())
    assert response["status"] == 200
    assert json.loads(response["body"]) == {
        "chat_id": "42",
        "method": "sendMessage",
        "text": "book: empty",
    }
    assert a.storage["outbox"] == []
    assert b'"dedup":true' in a.handle_webhook(request())["body"]
    assert a.handle_webhook(request(chat="foreign", update_id=2))["status"] == 200
    assert a.storage["outbox"] == []


def test_research_init_is_idempotent_and_does_not_erase_theses():
    r.runtime.assume_ownership_if_unowned = lambda: None
    r.runtime.emit_event = lambda *a, **k: None
    a = r.HyperliquidResearch()
    a.storage = {}
    assert a.init(b"{}") == b"ok: initialized"
    a.storage["theses"] = [{"statement": "keep me"}]
    assert a.init(b"{}") == b"ok: already initialized"
    assert a.storage["theses"] == [{"statement": "keep me"}]


def test_research_init_rejects_running_as_a_deploy_time_control():
    r.runtime.assume_ownership_if_unowned = lambda: None
    a = r.HyperliquidResearch()
    a.storage = {}
    with pytest.raises(ValueError, match="controlled by the start handler"):
        a.init(b'{"running":true}')


def test_research_rejects_malformed_actor_addresses():
    r.runtime.emit_event = lambda *a, **k: None
    a = r.HyperliquidResearch()
    a.storage = {"cfg": {}}
    with pytest.raises(ValueError, match="address"):
        a.set_exec(b'{"exec_addr":"not-an-address"}')


def test_notify_lifecycle_is_idempotent():
    import hl_notify as n

    n.runtime.assume_ownership_if_unowned = lambda: None
    n.runtime.emit_event = lambda *a, **k: None
    a = n.HlNotify()
    a.storage = {}
    payload = json.dumps({"secret_account": "0x" + "33" * 20}).encode()
    assert a.init(payload) == b"ok"
    a.storage["guidance"] = [{"text": "keep me"}]
    assert a.init(payload) == b"ok: already initialized"
    assert a.storage["guidance"] == [{"text": "keep me"}]


def test_notify_requires_cross_account_secret_owner():
    import hl_notify as n

    n.runtime.assume_ownership_if_unowned = lambda: None
    a = n.HlNotify()
    a.storage = {}
    with pytest.raises(ValueError, match="secret_account"):
        a.init(b"{}")
    account = "0x" + "AB" * 20
    assert n._secret_refs({"secret_account": account}) == [
        [account.lower(), n.TG_SECRET_KEY]
    ]


# ---- synthesis normalization (the overnight garbage, as fixtures) ------------ #


def _syn(thesis=None, ideas=None):
    return json.dumps(
        {"thesis": thesis or {}, "ideas": ideas or [], "economic_context": "ctx"}
    )


def test_parse_synthesis_dedupes_and_drops_contradictions():
    t = {
        "statement": "risk-on",
        "conviction": 100,
        "long": ["BTC", "ETH", "BTC", ""],
        "short": ["ETH", "AAPL", "AAPL"],
    }
    thesis, _ = r._parse_synthesis(_syn(t))
    assert thesis["long"] == ["BTC"]  # ETH contradicted -> dropped BOTH sides
    assert thesis["short"] == ["AAPL"]  # deduped, de-blanked
    assert thesis["conviction"] == 100


def test_parse_synthesis_rejects_zero_conviction_noise():
    t = {"statement": "meh", "conviction": 0, "long": ["BTC"], "short": [""]}
    thesis, _ = r._parse_synthesis(_syn(t))
    assert thesis is None


def test_parse_synthesis_filters_ideas_to_universe():
    ideas = [
        {"asset": "XRP", "side": "short", "confidence": 80},
        {"asset": "ETH", "side": "long", "confidence": 85},
        {"asset": "ETH", "side": "long", "confidence": 70},  # dupe, lower conf
        {"asset": "BTC", "side": "long", "confidence": 95},
    ]
    _, out = r._parse_synthesis(_syn(None, ideas), universe=["BTC", "ETH", "SOL"])
    assets = sorted((i["asset"], i["confidence"]) for i in out)
    assert assets == [("BTC", 95), ("ETH", 85)]  # XRP gone, ETH deduped high


def test_parse_synthesis_without_universe_keeps_all_assets():
    ideas = [{"asset": "XRP", "side": "short", "confidence": 80}]
    _, out = r._parse_synthesis(_syn(None, ideas))
    assert [i["asset"] for i in out] == ["XRP"]
