"""Call lifecycle, trace parsing and decoded audio observation."""

from __future__ import annotations

import json
import math
import os
import re
import select
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol


class SiplingError(RuntimeError):
    """A local backend failure (child output is deliberately not exposed)."""


def redact(value: str) -> str:
    return re.sub(r"(?i)(sips?:)[^\s<>@]+@", r"\1[redacted]@", value)


class Backend(Protocol):
    def start(self, uri: str, transport: str, recording: Path) -> subprocess.Popen[str]: ...


class PjsuaBackend:
    """No linking or bundled native code; one isolated pjsua per call."""

    def __init__(self, executable: str = "pjsua") -> None:
        self.executable = executable

    def start(self, uri: str, transport: str, recording: Path) -> subprocess.Popen[str]:
        executable = shutil.which(self.executable)
        if not executable:
            raise SiplingError("pjsua not found; install pjproject (see README)")
        args = [
            executable,
            "--null-audio",
            "--clock-rate=16000",
            "--no-tones",
            "--no-vad",
            "--max-calls=1",
            "--local-port=0",
            "--bound-addr=0.0.0.0",
            "--rtp-port=0",
            "--no-udp" if transport == "tcp" else "--no-tcp",
            "--dis-codec=*",
            "--add-codec=PCMA",
            "--add-codec=PCMU",
            "--add-codec=G722",
            "--accept-redirect=0",
            "--use-compact-form",
            "--app-log-level=5",
            "--no-color",
            "--log-level=5",
            f"--rec-file={recording}",
            "--auto-rec",
            uri,
        ]
        if os.name != "posix":
            raise SiplingError("v0.1 requires macOS or Linux (PTY support)")
        import pty

        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=slave,
                stderr=slave,
                text=True,
            )
            process.stdout = os.fdopen(master, "r", errors="replace")
            return process
        except OSError:
            os.close(master)
            raise SiplingError("could not launch pjsua") from None
        finally:
            os.close(slave)


class Trace:
    """Parse only SIP wire messages, correlating responses by CSeq method."""

    def __init__(self) -> None:
        self.ladder: list[str] = []
        self.final_code: int | None = None
        self.codec: str | None = None
        self.started: float | None = None
        self.ring_time: float | None = None
        self.answer_time: float | None = None
        self.ended: float | None = None
        self.clean_bye = False
        self._message: list[str] = []
        self._direction = ""
        self._in_message = False

    def feed(self, line: str, now: float) -> None:
        if re.search(r"\b(TX|RX) \d+ bytes", line):
            self._direction = "TX" if "TX " in line else "RX"
            self._message = []
            self._in_message = True
        elif line.strip() == "--end msg--":
            self._parse(now)
            self._message = []
            self._in_message = False
        elif self._in_message:
            if len(self._message) < 2000:
                self._message.append(line.rstrip())

    def _parse(self, now: float) -> None:
        if not self._message:
            return
        first = self._message[0].strip()
        body = "\n".join(self._message)
        cseq = re.search(r"(?im)^CSeq:\s*\d+\s+(\w+)", body)
        if re.match(r"^SIP/2.0 [1-6][0-9]{2}\b", first) and cseq:
            code = int(first.split()[1])
            method = cseq[1]
            self.ladder.append(f"{code} ({method})")
            if method == "INVITE" and self._direction == "RX":
                elapsed = now - (self.started or now)
                if code in (180, 183) or 200 <= code < 300:
                    if self.ring_time is None:
                        self.ring_time = elapsed
                if code >= 200 and self.final_code is None:
                    if code < 300:
                        self.answer_time = elapsed
                        media = re.search(r"(?m)^m=audio\s+\d+\s+\S+\s+(\d+)", body)
                        if media:
                            pt = media[1]
                            mapping = re.search(rf"(?im)^a=rtpmap:{pt}\s+([^/\s]+)", body)
                            selected = (
                                mapping[1].upper()
                                if mapping
                                else {"0": "PCMU", "8": "PCMA", "9": "G722"}.get(pt)
                            )
                            self.codec = selected if selected in {"G722", "PCMU", "PCMA"} else None
                    self.final_code = code
            if method == "BYE" and 200 <= code < 300:
                self.clean_bye = True
                self.ended = now
        elif re.match(r"^(INVITE|ACK|BYE|CANCEL|OPTIONS)\s", first):
            method = first.split()[0]
            self.ladder.append(method)
            if method == "INVITE" and self._direction == "TX" and self.started is None:
                self.started = now
            if method == "BYE":
                self.ended = now


def pcm_samples(path: Path, offset: int = 0) -> bytes:
    """Read a growing PCM WAV without trusting its not-yet-finalized data size."""
    try:
        with path.open("rb") as stream:
            if stream.read(12)[:4] != b"RIFF":
                return b""
            while header := stream.read(8):
                if len(header) != 8:
                    return b""
                tag, size = struct.unpack("<4sI", header)
                if tag == b"data":
                    stream.seek(offset, 1)
                    return stream.read()
                stream.seek(size + size % 2, 1)
    except FileNotFoundError:
        pass
    return b""


def has_energy(data: bytes, floor: float) -> bool:
    # 20 ms at the fixed 16 kHz recorder rate; avoid diluting speech with silence.
    for offset in range(0, len(data) - 639, 640):
        samples = struct.unpack("<320h", data[offset : offset + 640])
        if math.sqrt(sum(s * s for s in samples) / 320) / 32768 > floor:
            return True
    return False


class CallResult:
    def __init__(self, uri: str, transport: str, backend: Backend, no_redact: bool) -> None:
        self.uri = uri if no_redact else redact(uri)
        self.transport = transport
        self._temp = tempfile.TemporaryDirectory(prefix="sipling-")
        self._recording = Path(self._temp.name) / "audio.wav"
        self._trace = Trace()
        self._stop = threading.Event()
        self._closed = False
        self.timed_out = False
        self.audio_detected = False
        try:
            self._process = backend.start(uri, transport, self._recording)
        except BaseException:
            self._temp.cleanup()
            raise
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self._process.stdout
        if stream is None:
            return
        pending = b""
        while not self._stop.is_set():
            if not select.select([stream], [], [], 0.02)[0]:
                continue
            try:
                chunk = os.read(stream.fileno(), 65536)
            except OSError:  # PTY returns EIO at child exit on Linux.
                break
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._trace.feed(line.decode(errors="replace").rstrip("\r"), time.monotonic())

    @property
    def answered(self) -> bool:
        return self.final_code is not None and 200 <= self.final_code < 300

    @property
    def final_code(self) -> int | None:
        return self._trace.final_code

    @property
    def ring_time(self) -> float | None:
        return self._trace.ring_time

    @property
    def answer_time(self) -> float | None:
        return self._trace.answer_time

    @property
    def codec(self) -> str | None:
        return self._trace.codec

    @property
    def ladder(self) -> list[str]:
        return list(self._trace.ladder)

    @property
    def clean_bye(self) -> bool:
        return self._trace.clean_bye

    @property
    def duration(self) -> float:
        if self._trace.answer_time is None or self._trace.started is None:
            return 0.0
        return max(
            0,
            (self._trace.ended or time.monotonic()) - self._trace.started - self._trace.answer_time,
        )

    def heard_audio(self, within: float = 5, *, floor: float = 0.01) -> bool:
        """Observe new decoded inbound PCM; normalized RMS floor, not speech recognition."""
        if not math.isfinite(within) or within < 0 or not 0 <= floor < 1:
            raise ValueError("within must be finite and >= 0; floor must be in [0, 1)")
        if self._closed or not self.answered:
            return False
        offset = len(pcm_samples(self._recording))
        deadline = time.monotonic() + within
        while True:
            new = pcm_samples(self._recording, offset)
            if has_energy(new, floor):
                self.audio_detected = True
                return True
            offset += len(new) // 640 * 640
            if time.monotonic() >= deadline or self._process.poll() is not None:
                return False
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))

    def _send(self, command: str) -> None:
        try:
            if self._process.stdin:
                self._process.stdin.write(command + "\n")
                self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def hangup(self) -> None:
        """Idempotently attempt BYE/CANCEL, then reap child and delete private captures."""
        if self._closed:
            return
        self._send("h")
        deadline = time.monotonic() + 2
        while self._process.poll() is None and time.monotonic() < deadline:
            if self.clean_bye or not self.answered:
                break
            time.sleep(0.02)
        self._send("q")
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        time.sleep(0.03)  # allow the tail reader to consume shutdown's final transaction
        self._stop.set()
        self._reader.join()
        if self._process.stdin:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        if self._process.stdout:
            self._process.stdout.close()
        if self._trace.ended is None:
            self._trace.ended = time.monotonic()
        self._closed = True
        self._temp.cleanup()

    def to_dict(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "transport": self.transport,
            "answered": self.answered,
            "final_code": self.final_code,
            "ring_time": self.ring_time,
            "answer_time": self.answer_time,
            "codec": self.codec,
            "duration": self.duration,
            "clean_bye": self.clean_bye,
            "timed_out": self.timed_out,
            "audio_detected": self.audio_detected,
            "ladder": self.ladder,
        }

    def save_ladder(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    def __enter__(self) -> CallResult:
        return self

    def __exit__(self, *_: object) -> None:
        self.hangup()


def call(
    uri: str,
    *,
    transport: str = "tcp",
    timeout: float = 15,
    backend: Backend | None = None,
    no_redact: bool = False,
) -> CallResult:
    """Dial and wait for a final INVITE response. Use as a context manager."""
    if transport not in ("tcp", "udp"):
        raise ValueError("transport must be tcp or udp")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not re.fullmatch(r"sip:[^\s<>]+", uri, flags=re.IGNORECASE):
        raise ValueError("expected a sip: URI without whitespace or angle brackets")
    if "?" in uri or '"' in uri or "\\" in uri:
        raise ValueError("URI headers and quoted URI components are not supported")
    existing = re.search(r";transport=([^;]+)", uri, re.IGNORECASE)
    if existing and existing[1].lower() != transport:
        raise ValueError("URI transport conflicts with selected transport")
    target = uri if existing else uri + f";transport={transport}"
    result = CallResult(target, transport, backend or PjsuaBackend(), no_redact)
    deadline = time.monotonic() + timeout
    try:
        while result.final_code is None:
            if result._process.poll() is not None:
                time.sleep(0.03)
                if result.final_code is None:
                    raise SiplingError(
                        "pjsua exited before a final response; check installation/network"
                    )
                break
            if time.monotonic() >= deadline:
                result.timed_out = True
                break
            time.sleep(0.01)
        if not result.answered:
            result.hangup()
        return result
    except BaseException:
        result.hangup()
        raise
