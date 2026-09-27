from cowboy_sdk import codec, mock_host
from cowboy_sdk.testing import SimulatedChain

from actors.ticker.main import TimerCounter

ACTOR_ADDR = b"\x22" * 20  # the mock host's own-actor address; a timer fire carries it as sender


def test_init_stores_interval_once() -> None:
    actor = TimerCounter()
    chain = SimulatedChain(actor)
    first = codec.decode(chain.call(actor.init, b'{"interval_blocks":3,"max_ticks":2}'))
    assert first == {"status": "initialized", "interval_blocks": 3}
    again = codec.decode(chain.call(actor.init, b'{"interval_blocks":9}'))
    assert again == {"status": "already_initialized", "interval_blocks": 3}


def test_start_arms_one_timer() -> None:
    actor = TimerCounter()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"interval_blocks":3,"max_ticks":2}')
    result = codec.decode(chain.call(actor.start, codec.encode({})))
    assert result["status"] == "started"
    assert result["next_timer"]["handler"] == "on_timer"
    assert result["next_timer"]["delay_blocks"] == 3
    timers = mock_host.get_timers()
    assert len(timers) == 1
    assert list(timers.values())[0]["height"] == chain.block_height + 3
    assert chain.events()[-1][0] == "timer_counter.started"


def test_timer_fires_ticks_and_rearms_until_max() -> None:
    actor = TimerCounter()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"interval_blocks":2,"max_ticks":2}')
    chain.call(actor.start, codec.encode({}))
    chain.set_sender(ACTOR_ADDR)

    assert chain.advance_block(1) == []
    fired = chain.advance_block(1)
    assert [h for h, _ in fired] == ["on_timer"]
    first = codec.decode(fired[0][1])
    assert first["status"] == "ticked" and first["count"] == 1 and first["tag"] == "tick"
    assert len(mock_host.get_timers()) == 1  # re-armed

    fired = chain.advance_block(2)
    second = codec.decode(fired[0][1])
    assert second["status"] == "completed" and second["count"] == 2
    assert second["next_timer"] == {}
    assert mock_host.get_timers() == {}
    assert [name for name, _ in chain.events() if name == "timer_counter.tick"] == ["timer_counter.tick"] * 2


def test_get_state_reflects_count() -> None:
    actor = TimerCounter()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"interval_blocks":1,"max_ticks":1}')
    chain.call(actor.start, codec.encode({}))
    chain.set_sender(ACTOR_ADDR)
    chain.advance_block(1)
    state = codec.decode(chain.call(actor.get_state, codec.encode({})))
    assert state == {
        "started": False,
        "count": 1,
        "max_ticks": 1,
        "interval_blocks": 1,
        "next_timer": {},
    }
