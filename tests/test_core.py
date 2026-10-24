import struct
import wave

import pytest

from sipling import SiplingError
from sipling import probe as call
from sipling.cli import main
from sipling.core import PjsuaBackend, Trace, has_energy, pcm_samples, redact


def feed(trace, first, method="INVITE", direction="RX", now=2, sdp=""):
    for line in [f"{direction} 100 bytes", first, f"CSeq: 1 {method}", "", sdp, "--end msg--"]:
        trace.feed(line, now)


def test_trace_timing_and_response_correlation():
    trace = Trace()
    feed(trace, "INVITE sip:secret@example.test SIP/2.0", direction="TX", now=1)
    feed(trace, "SIP/2.0 100 Trying", now=1.1)
    feed(trace, "SIP/2.0 180 Ringing", now=1.2)
    feed(trace, "SIP/2.0 200 OK", now=1.4, sdp="m=audio 10000 RTP/AVP 9\na=rtpmap:9 G722/8000")
    feed(trace, "SIP/2.0 200 OK", method="BYE", now=2)
    assert trace.final_code == 200
    assert trace.codec == "G722"
    assert trace.ring_time == pytest.approx(0.2)
    assert trace.answer_time == pytest.approx(0.4)
    assert trace.clean_bye
    assert "secret" not in str(trace.ladder)


def test_bye_does_not_answer_failed_call():
    trace = Trace()
    feed(trace, "SIP/2.0 486 Busy Here")
    feed(trace, "SIP/2.0 200 OK", method="BYE")
    assert trace.final_code == 486
    assert trace.answer_time is None


def test_energy_and_growing_wav(tmp_path):
    path = tmp_path / "input.wav"
    data = struct.pack("<640h", *([10000, -10000] * 320))
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(data)
    raw = bytearray(path.read_bytes())
    raw[40:44] = b"\x00" * 4
    path.write_bytes(raw)
    assert pcm_samples(path) == data
    assert has_energy(data, 0.01)
    assert not has_energy(b"\x00" * 640, 0.01)
    assert not has_energy(data[:639], 0.01)
    assert pcm_samples(tmp_path / "missing") == b""


@pytest.mark.parametrize(
    "uri", ["sip:alice:password@host", "sips:+1234@host;foo=bar", "SIP:alice%40secret@host"]
)
def test_redaction(uri):
    assert "alice" not in redact(uri)
    assert "password" not in redact(uri)
    assert "+1234" not in redact(uri)
    assert "[redacted]@host" in redact(uri)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"transport": "tls"},
        {"uri": "http://secret"},
        {"uri": "sip:a@b\n--log-file=leak"},
        {"uri": "sip:a@b;transport=udp"},
        {"uri": "sip:a@b?Subject=secret"},
    ],
)
def test_input_validation(kwargs):
    with pytest.raises(ValueError):
        call(**({"uri": "sip:secret@example.test"} | kwargs))


def test_missing_executable():
    with pytest.raises(SiplingError, match="not found"):
        call("sip:secret@example.test", backend=PjsuaBackend("sipling-missing-executable"))


def test_cli_invalid_private_uri(capsys):
    assert main(["call", "sip:secret@host\ninvalid", "--json"]) == 2
    output = capsys.readouterr()
    assert "secret" not in output.err + output.out


def test_child_exit_does_not_mask_error_or_leak_temporary_files(monkeypatch, tmp_path):
    import subprocess
    import sys
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class ExitingBackend:
        def start(self, uri, transport, recording):
            return subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(1)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )

    with pytest.raises(SiplingError, match="exited before"):
        call("sip:secret@example.test", backend=ExitingBackend())
    assert list(tmp_path.iterdir()) == []


def test_idle_log_lines_are_not_retained():
    trace = Trace()
    for _ in range(100):
        trace.feed("debug secret", 1)
    assert not trace._message


def test_direct_answer_sets_ring_time_and_static_codec():
    trace = Trace()
    feed(trace, "INVITE sip:user@host SIP/2.0", direction="TX", now=1)
    feed(trace, "SIP/2.0 200 OK", now=2, sdp="m=audio 4000 RTP/AVP 8")
    assert trace.ring_time == trace.answer_time == 1
    assert trace.codec == "PCMA"


def test_cli_argument_errors_redact_uri(capsys):
    with pytest.raises(SystemExit):
        main(["sip:secret:password@host"])
    output = capsys.readouterr().err
    assert "secret" not in output
    assert "password" not in output


def test_unknown_sdp_codec_cannot_leak_into_public_result():
    trace = Trace()
    feed(trace, "SIP/2.0 200 OK", sdp="m=audio 4000 RTP/AVP 100\na=rtpmap:100 secret/8000")
    assert trace.codec is None
