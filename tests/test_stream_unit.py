import pytest

from sipling import SiplingError, call


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"transport": "tls"},
        {"uri": "http://private"},
        {"uri": "sip:private@host\nq"},
        {"uri": "sip:private@host?Subject=private"},
        {"uri": "sip:private@host;transport=udp"},
    ],
)
def test_streaming_validation_precedes_child_start(kwargs):
    with pytest.raises(ValueError):
        call(**({"uri": "sip:private@example.test"} | kwargs))


def test_missing_helper_is_explicit_and_redacted(monkeypatch):
    monkeypatch.setenv("SIPLING_STREAM_EXECUTABLE", "/no/such/helper")
    with pytest.raises(SiplingError, match="not found") as error:
        call("sip:private:password@example.test")
    assert "private" not in str(error.value)
    assert "password" not in str(error.value)


def test_disconnect_waits_for_transaction_evidence_before_receive_ends(monkeypatch, tmp_path):
    import sys
    import threading
    import time

    helper = tmp_path / "helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import time, sys\n"
        "print('ANSWERED 200', flush=True)\n"
        "time.sleep(.04)\n"
        "print('ENDED 200', flush=True)\n"
        "time.sleep(.08)\n"
        "print('CLEAN 200', flush=True)\n"
        "for line in sys.stdin:\n"
        "    if line.strip() == 'q': break\n"
    )
    helper.chmod(0o700)
    monkeypatch.setenv("SIPLING_STREAM_EXECUTABLE", str(helper))
    with call("sip:private@example.test") as handle:
        events_at_receive_end = []

        def receive():
            list(handle.recv_audio())
            events_at_receive_end.extend(e.name for e in handle.ladder)

        thread = threading.Thread(target=receive)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert events_at_receive_end == ["dialed", "ringing", "answered", "hangup_clean"]
        assert handle.ladder[-1].at <= time.monotonic()


@pytest.mark.parametrize("stale_kind", ["receive", "send"])
def test_native_timestamps_reject_stale_ipc(monkeypatch, tmp_path, stale_kind):
    import sys
    import time

    helper = tmp_path / "timestamp-helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import socket, struct, sys, time\n"
        "sock = socket.socket(fileno=int(sys.argv[1]))\n"
        "print('ANSWERED 200', flush=True)\n"
        "old = time.clock_gettime_ns(time.CLOCK_MONOTONIC) - 200_000_000\n"
        + (
            "sock.send(b'P' + struct.pack('!Q', old) + bytes(320))\n"
            if stale_kind == "receive"
            else "packet = sock.recv(1024)\n"
            "sock.send(b'A' + packet[1:5] + struct.pack('!Q', old))\n"
        )
        + "for line in sys.stdin:\n"
        "    if line.strip() == 'q': break\n"
    )
    helper.chmod(0o700)
    monkeypatch.setenv("SIPLING_STREAM_EXECUTABLE", str(helper))
    with call("sip:private@example.test") as handle:
        if stale_kind == "receive":
            with pytest.raises(SiplingError, match="receive IPC fell behind"):
                list(handle.recv_audio())
        else:
            deadline = time.monotonic() + 1
            while not handle.answered:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with pytest.raises(SiplingError, match="send fell behind"):
                handle.send_audio(bytes(320))
