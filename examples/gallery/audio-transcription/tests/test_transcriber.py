import json
from types import SimpleNamespace

import pytest
from cowboy_sdk import codec, mock_host, runner
from cowboy_sdk.testing import SimulatedChain

from actors.transcriber.main import SYSTEM_PROMPT, AudioTranscriber


def test_init_stores_default_language() -> None:
    actor = AudioTranscriber()
    chain = SimulatedChain(actor)
    assert chain.call(actor.init, b'{"default_language":"en"}') == b"ok: initialized"
    name, data = chain.events()[0]
    assert (name, codec.decode(data)) == ("transcriber.init", {"default_language": "en"})
    stats = codec.decode(chain.call(actor.get_stats, codec.encode({})))
    assert stats == {"default_language": "en", "job_count": 0}
    assert codec.decode(chain.call(actor.get_last_transcript, codec.encode({}))) == {"status": "idle"}


def test_transcribe_audio_records_job_and_dispatches_llm_job() -> None:
    actor = AudioTranscriber()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b'{"default_language":"en"}')
    chain.call(
        actor.transcribe_audio,
        codec.encode({"audio_url": "https://example.com/demo.wav", "hints": "earnings call"}),
    )

    assert chain.state("job_count") == codec.encode(1)
    sent = mock_host.get_send_log()
    assert len(sent) == 1
    assert sent[0]["target"] == runner.RUNNER_ADDRESS
    job = codec.decode(sent[0]["payload"])
    assert job["job_type"] == "llm"
    prompt = json.loads(job["payload"]["args"][0])
    assert prompt["audio_url"] == "https://example.com/demo.wav"
    assert prompt["language"] == "en"  # default_language fills the omitted field
    assert prompt["hints"] == "earnings call"
    assert job["payload"]["kwargs"] == {"system_prompt": SYSTEM_PROMPT, "max_tokens": 220}
    assert job["reply_handler"] == "_transcribe_audio_job__resume"


def test_finalize_transcript_parses_fenced_json() -> None:
    actor = AudioTranscriber()
    chain = SimulatedChain(actor)
    chain.call(actor.init, b"")
    request = {"job_id": 1, "audio_url": "https://example.com/demo.wav", "language": "en"}
    fenced = SimpleNamespace(
        text='```json\n{"transcript": "hello world", "summary": "greeting", "confidence": 90}\n```'
    )
    report = codec.decode(actor._finalize_transcript(request, fenced))
    assert report == {
        "job_id": 1,
        "audio_url": "https://example.com/demo.wav",
        "language": "en",
        "transcript": "hello world",
        "summary": "greeting",
        "confidence": 90,
    }
    assert codec.decode(chain.state("last_transcript")) == report
    assert chain.events()[-1][0] == "transcriber.result"


def test_finalize_transcript_rejects_prose() -> None:
    actor = AudioTranscriber()
    SimulatedChain(actor).call(actor.init, b"")
    with pytest.raises(ValueError, match="did not return JSON"):
        actor._finalize_transcript({"job_id": 1}, SimpleNamespace(text="Sure! Here is the transcript."))
