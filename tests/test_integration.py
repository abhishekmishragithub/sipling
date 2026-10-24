import shutil
import socket

import pytest

from sipling import probe as call
from sipling.cli import main

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not shutil.which("pjsua"), reason="pjsua is not installed"),
]


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_answer_energy_and_bye(agent_factory, transport, tmp_path):
    agent = agent_factory(transport)
    with call(agent.uri, transport=transport, timeout=4) as result:
        assert result.answered, result.to_dict()
        assert result.final_code == 200
        assert result.codec == "PCMU"
        assert 0 <= result.ring_time <= result.answer_time < 2
        assert result.heard_audio(within=3)
        assert agent.outbound_rtp.is_set()
        assert "secret" not in result.uri
    assert result.clean_bye, result.ladder
    assert "ACK" in agent.methods
    assert "BYE" in agent.methods
    assert result.duration > 0
    result.hangup()
    result.save_ladder(tmp_path / "ladder.json")
    assert "secret" not in (tmp_path / "ladder.json").read_text()
    assert not result._recording.exists()
    assert result._process.poll() is not None


def test_silence(agent_factory):
    agent = agent_factory(audio=False)
    with call(agent.uri, timeout=4) as result:
        assert result.answered
        assert not result.heard_audio(within=0.4)


def test_rejection(agent_factory):
    agent = agent_factory(code=486)
    result = call(agent.uri, timeout=4)
    assert not result.answered
    assert result.final_code == 486
    assert result.answer_time is None
    assert not result.clean_bye
    assert result._closed


def test_no_response_timeout():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
        sink.bind(("127.0.0.1", 0))
        result = call(f"sip:hidden@127.0.0.1:{sink.getsockname()[1]}", transport="udp", timeout=0.3)
    assert result.timed_out
    assert result.final_code is None
    assert result._closed
    assert not result.heard_audio()


def test_cli_json(agent_factory, capsys, tmp_path):
    import json

    agent = agent_factory()
    capture = tmp_path / "ladder.json"
    assert (
        main(
            [
                "call",
                agent.uri,
                "--expect-answer",
                "--expect-audio",
                "3",
                "--timeout",
                "4",
                "--json",
                "--ladder-file",
                str(capture),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["answered"] and data["clean_bye"] and data["audio_detected"]
    assert "secret" not in output
    assert json.loads(capture.read_text()) == data


def test_cli_rejection(agent_factory, capsys):
    agent = agent_factory(code=486)
    assert main(["call", agent.uri, "--expect-answer", "--json"]) == 1
    assert "486" in capsys.readouterr().out


def test_remote_bye(agent_factory):
    agent = agent_factory(remote_bye=True)
    with call(agent.uri, timeout=4) as result:
        import time

        deadline = time.monotonic() + 2
        while not result.clean_bye and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result.clean_bye, result.ladder
    assert result.final_code == 200


@pytest.mark.parametrize("codec", ["G722", "PCMA"])
def test_codec_negotiation(agent_factory, codec):
    agent = agent_factory(codec=codec)
    with call(agent.uri, timeout=4) as result:
        assert result.answered
        assert result.codec == codec
        assert "RTP/AVP 9 0 8" in agent.invite
    assert result.clean_bye


def test_fixture_and_explicit_unredacted_output(agent_factory, sip_caller):
    agent = agent_factory()
    result = sip_caller(agent.uri, no_redact=True)
    assert "secret@" in result.uri
    assert result.answered
    result.hangup()


def test_exception_in_context_cleans_up(agent_factory):
    agent = agent_factory()
    with pytest.raises(RuntimeError, match="assertion failed"):
        with call(agent.uri, timeout=4) as result:
            raise RuntimeError("assertion failed")
    assert result._closed
    assert result.clean_bye


def test_missing_bye_response_is_not_reported_clean(agent_factory):
    agent = agent_factory(drop_bye=True)
    result = call(agent.uri, timeout=4)
    result.hangup()
    assert "BYE" in agent.methods
    assert not result.clean_bye
    assert result._closed
    assert result._process.poll() is not None
