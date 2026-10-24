"""Real pjsua PCM and RFC4733 tests; no mock transport."""

import math
import shutil
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from sipling import SiplingError, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from local_agent import LocalAgent, tone  # noqa: E402

pytestmark = pytest.mark.skipif(
    not shutil.which("sipling-stream") or not shutil.which("pjsua"),
    reason="separately built sipling-stream and pjsua required",
)


def energy(pcm):
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def capture(handle):
    frames, errors = [], []

    def receive():
        try:
            for pcm in handle.recv_audio():
                frames.append((time.monotonic(), pcm))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=receive)
    thread.start()
    return frames, errors, thread


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_stream_clock_echo_dtmf_and_shutdown(transport):
    with LocalAgent(transport) as agent:
        before = time.monotonic()
        with call(agent.uri, transport=transport) as handle:
            assert time.monotonic() - before < 0.15
            frames, errors, receiver = capture(handle)
            wait_until(lambda: handle.answered)
            wait_until(lambda: any(energy(p) > 0.01 for _, p in frames))
            agent.greeting(False)
            time.sleep(0.25)
            assert len(frames) >= 10
            assert all(len(p) == 320 for _, p in frames)
            # Sustained quiet is delivered as frames, not absence or replay.
            silence_start = len(frames)
            time.sleep(0.2)
            quiet = frames[silence_start:]
            assert 7 <= len(quiet) <= 13
            assert all(energy(p) < 0.01 for _, p in quiet)
            intervals = [b[0] - a[0] for a, b in zip(quiet, quiet[1:], strict=False)]
            assert 0.01 < sum(intervals) / len(intervals) < 0.03
            agent.echo(True)
            time.sleep(0.08)
            start_index = len(frames)
            started = time.monotonic()
            handle.send_audio(tone(0.3))
            elapsed = time.monotonic() - started
            assert 0.29 <= elapsed < 0.40
            time.sleep(0.3)
            echoed = [(at, p) for at, p in frames[start_index:] if energy(p) > 0.01]
            assert len(echoed) >= 12
            assert 0 <= echoed[0][0] - started < 0.3
            assert 0.22 < echoed[-1][0] - echoed[0][0] < 0.4
            handle.send_dtmf("12#A")
            wait_until(lambda: len(agent.dtmf) == 4)
            assert agent.dtmf == ("1", "2", "#", "A")
            with pytest.raises(ValueError):
                handle.send_dtmf("1\nq")
            with pytest.raises(ValueError):
                handle.send_audio(b"x")
            hangup_started = time.monotonic()
            handle.hangup()
            receiver.join(timeout=0.2)
            assert not receiver.is_alive()
            assert not errors
            assert time.monotonic() - hangup_started < 4
        assert handle._process.poll() is not None
        assert [e.name for e in handle.ladder][-1] == "hangup_clean"
        assert all(before <= e.at <= time.monotonic() for e in handle.ladder)
        assert "local-agent" not in str(handle.ladder) + handle.uri


def test_no_answer_is_async_and_hangup_unblocks():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
        sink.bind(("127.0.0.1", 0))
        before = time.monotonic()
        with call(
            f"sip:private@127.0.0.1:{sink.getsockname()[1]}", transport="udp", timeout=0.25
        ) as handle:
            assert time.monotonic() - before < 0.15
            frames, errors, receiver = capture(handle)
            wait_until(lambda: handle.timed_out)
            receiver.join(timeout=0.1)
            assert not receiver.is_alive()
            assert not errors
            assert all(energy(p) == 0 for _, p in frames)
            assert not handle.answered


def test_slow_consumer_fails_instead_of_fabricating_pacing():
    with LocalAgent() as agent, call(agent.uri, transport="udp") as handle:
        wait_until(lambda: handle.answered)
        time.sleep(0.3)
        with pytest.raises(SiplingError, match="fell behind"):
            list(handle.recv_audio())


def test_remote_hangup_and_dead_helper():
    with LocalAgent() as agent, call(agent.uri, transport="udp") as handle:
        frames, errors, receiver = capture(handle)
        wait_until(lambda: handle.answered)
        agent.command("h")
        receiver.join(timeout=2)
        assert not receiver.is_alive() and not errors
        wait_until(lambda: any(e.name == "hangup_clean" for e in handle.ladder))
    with LocalAgent() as agent, call(agent.uri, transport="udp") as handle:
        frames, errors, receiver = capture(handle)
        wait_until(lambda: handle.answered)
        handle._process.kill()
        receiver.join(timeout=2)
        assert not receiver.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], SiplingError)
        assert handle.ladder[-1].name == "hangup_abnormal"


def test_hangup_interrupts_playback():
    with LocalAgent() as agent, call(agent.uri, transport="udp") as handle:
        _, errors, receiver = capture(handle)
        wait_until(lambda: handle.answered)
        failures = []

        def play():
            try:
                handle.send_audio(tone(5))
            except SiplingError as exc:
                failures.append(exc)

        sender = threading.Thread(target=play)
        sender.start()
        time.sleep(0.1)
        handle.hangup()
        sender.join(timeout=0.1)
        receiver.join(timeout=0.1)
        assert not sender.is_alive() and not receiver.is_alive()
        assert len(failures) == 1 and not errors


def test_rejection_and_unnegotiated_dtmf(agent_factory):
    agent = agent_factory(code=486)
    with call(agent.uri) as handle:
        _, errors, receiver = capture(handle)
        receiver.join(timeout=3)
        assert not receiver.is_alive() and not errors
        assert not handle.answered and handle.final_code == 486
        assert handle.ladder[-1].name == "hangup_abnormal"
    agent = agent_factory()
    with call(agent.uri) as handle:
        _, errors, receiver = capture(handle)
        wait_until(lambda: handle.answered)
        with pytest.raises(SiplingError, match="RFC4733"):
            handle.send_dtmf("1")
        handle.hangup()
        receiver.join(timeout=0.1)
        assert not errors


def test_missing_bye_ack_unblocks_before_cleanup_and_is_abnormal(agent_factory):
    agent = agent_factory(drop_bye=True)
    with call(agent.uri) as handle:
        _, errors, receiver = capture(handle)
        wait_until(lambda: handle.answered)
        closer = threading.Thread(target=handle.hangup)
        closer.start()
        receiver.join(timeout=0.1)
        assert not receiver.is_alive() and not errors
        closer.join(timeout=5)
        assert not closer.is_alive()
        assert handle.ladder[-1].name == "hangup_abnormal"
        assert "BYE" in agent.methods
