import json
from types import SimpleNamespace

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.scheduler.main import SYSTEM_PROMPT, AlertScheduler

ACTOR_ADDR = b"\x22" * 20  # the mock host's own-actor address; a timer fire carries it as sender
INIT = b'{"check_name":"treasury-balance","threshold":42,"interval_blocks":25}'


def test_init_stores_check_config() -> None:
    actor = AlertScheduler()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, INIT) == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("scheduler.init", {"check_name": "treasury-balance"})
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {
        "check_name": "treasury-balance",
        "threshold": 42,
        "interval_blocks": 25,
        "max_auto_checks": 0,
        "check_count": 0,
        "alert_count": 0,
        "next_timer_id": "",
    }


def test_schedule_next_check_arms_timer_that_fires_run_check() -> None:
    actor = AlertScheduler()
    chain = SimulatedChain(actor)
    chain.call(actor.init, INIT)
    result = codec.decode(
        chain.call(actor.schedule_next_check, codec.encode({"delay_blocks": 3, "observed_value": 38}))
    )
    assert result["scheduled"] is True and result["delay_blocks"] == 3
    timers = mock_host.get_timers()
    assert len(timers) == 1
    assert list(timers.values())[0]["height"] == chain.block_height + 3
    assert chain.state("next_timer_id") == codec.encode(result["timer_id"])

    chain.set_sender(ACTOR_ADDR)
    assert chain.advance_block(2) == []
    fired = chain.advance_block(1)
    assert [h for h, _ in fired] == ["run_check"]
    assert chain.state("check_count") == codec.encode(1)
    job = codec.decode(mock_host.get_send_log()[-1]["payload"])
    assert json.loads(job["payload"]["args"][0])["observed_value"] == 38


def test_run_check_records_request_and_dispatches_llm_job() -> None:
    actor = AlertScheduler()
    chain = SimulatedChain(actor)
    chain.call(actor.init, INIT)
    chain.call(actor.run_check, b'{"observed_value":38}')

    assert chain.state("check_count") == codec.encode(1)
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    assert json.loads(job["payload"]["args"][0]) == {
        "check_name": "treasury-balance",
        "observed_value": 38,
        "threshold": 42,
        "instruction": "Return JSON with normalized_value(int), alert(boolean), summary(string).",
    }
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 140}
    assert job["reply_handler"] == "_run_check_job__resume"
    # The runner never answers in the simulator, so the report is still idle.
    assert codec.decode(chain.call(actor.get_last_check, codec.encode({}))) == {"status": "idle"}


def test_finalize_run_check_parses_fenced_json_and_fails_soft() -> None:
    actor = AlertScheduler()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"threshold":42,"interval_blocks":5,"max_auto_checks":1}')
    request = {"request_id": 1, "observed_value": 38, "threshold": 42}

    fenced = SimpleNamespace(text='```json\n{"normalized_value": 40, "alert": true, "summary": "low"}\n```')
    report = codec.decode(actor._finalize_run_check(request, fenced))
    assert report["normalized_value"] == 40 and report["alert"] is True and report["summary"] == "low"
    assert report["next_timer_id"] != ""  # max_auto_checks 1 re-arms once
    assert len(mock_host.get_timers()) == 1
    assert chain.state("alert_count") == codec.encode(1)
    assert chain.state("max_auto_checks") == codec.encode(0)

    prose = SimpleNamespace(text="The reading looks fine.")
    report = codec.decode(actor._finalize_run_check(request, prose))
    assert report["normalized_value"] == 38 and report["alert"] is True and report["summary"] == ""
    assert report["next_timer_id"] == ""
    assert [n for n, _ in chain.events() if n == "scheduler.result_unparsed"] == ["scheduler.result_unparsed"]
    assert chain.state("alert_count") == codec.encode(2)
