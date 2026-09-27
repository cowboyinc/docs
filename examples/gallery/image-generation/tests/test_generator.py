import json

from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.runner import RunnerResult
from cowboy_sdk.testing import SimulatedChain

from actors.generator.main import SYSTEM_PROMPT, ImageGenerator


def test_init_stores_default_style() -> None:
    actor = ImageGenerator()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"default_style":"editorial"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("image.init", {"default_style": "editorial"})
    assert codec.decode(chain.call(actor.get_stats, codec.encode({}))) == {"default_style": "editorial", "job_count": 0}
    assert codec.decode(chain.call(actor.get_last_image, codec.encode({}))) == {"status": "idle"}


def test_generate_image_counts_and_dispatches_llm_job() -> None:
    actor = ImageGenerator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"default_style":"editorial"}')
    chain.call(actor.generate_image, codec.encode({"prompt": "A validator node on a mesa"}))

    assert codec.decode(chain.call(actor.get_stats, codec.encode({})))["job_count"] == 1
    (sent,) = mock_host.get_send_log()
    assert sent["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent["payload"])
    assert job["job_type"] == "llm"
    assert job["reply_handler"] == "_generate_image_job__resume"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 220}
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["prompt"] == "A validator node on a mesa"
    assert prompt["style"] == "editorial"


def test_finalize_image_falls_back_to_demo_asset_uri() -> None:
    actor = ImageGenerator()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    analysis = RunnerResult('{"revised_prompt": "A cinematic validator node", "palette": ["#c60", "#123"], "caption": "Mesa at dusk"}')
    image = codec.decode(actor._finalize_image({"job_id": 3, "prompt": "A validator node", "style": "cinematic"}, analysis))
    assert image == {
        "job_id": 3,
        "prompt": "A validator node",
        "style": "cinematic",
        "revised_prompt": "A cinematic validator node",
        "asset_uri": "demo://generated/3",
        "palette": ["#c60", "#123"],
        "caption": "Mesa at dusk",
    }
    name, data = chain.events()[-1]
    assert (name, codec.decode(data)) == ("image.result", image)
    assert codec.decode(chain.call(actor.get_last_image, codec.encode({}))) == image
