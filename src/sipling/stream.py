"""Clocked PCM through a separately built pjproject process, never WAV tailing."""

from __future__ import annotations

import math
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

from .core import SiplingError, Trace, redact


@dataclass(frozen=True)
class Event:
    name: str
    at: float


class StreamingCall:
    """One receive consumer and serialized sends; 8 kHz mono PCM16LE, 20 ms.

    Receive timestamps reflect the conference clock plus IPC/scheduler delay.
    A slow consumer fails after five queued frames rather than replaying stale
    audio with fabricated pacing. Read continuously (as earshot does).
    """

    sample_rate = 8000
    frame_bytes = 320

    def __init__(self, uri: str, transport: str, timeout: float, no_redact: bool) -> None:
        executable = shutil.which(os.environ.get("SIPLING_STREAM_EXECUTABLE", "sipling-stream"))
        if executable is None:
            raise SiplingError("sipling-stream not found; build helpers/Makefile (see README)")
        self.uri = uri if no_redact else redact(uri)
        self.transport = transport
        self._trace = Trace()
        self._events: list[Event] = [Event("dialed", time.monotonic())]
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._hangup_lock = threading.Lock()
        self._closed = threading.Event()
        self._ack = threading.Event()
        self._dtmf_ack = threading.Event()
        self._dtmf_status: int | None = None
        self._sequence = 0
        self._consumed_at = 0.0
        self._receive_lock = threading.Lock()
        self._frames: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=5)
        self._error: str | None = None
        self._local_hangup = False
        self._ended_at: float | None = None
        self.final_code: int | None = None
        self.timed_out = False
        self._socket, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.settimeout(0.02)
        try:
            self._process = subprocess.Popen(
                [executable, str(child.fileno()), transport, uri],
                pass_fds=(child.fileno(),),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError:
            self._socket.close()
            raise SiplingError("could not launch streaming helper") from None
        finally:
            child.close()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._media = threading.Thread(target=self._receive, daemon=True)
        self._reader.start()
        self._media.start()
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    @property
    def ladder(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def answered(self) -> bool:
        return any(e.name == "answered" for e in self.ladder)

    def _finish(self, clean: bool, error: str | None = None) -> None:
        with self._lock:
            if not any(e.name.startswith("hangup_") for e in self._events):
                self._error = error
                self._events.append(
                    Event("hangup_clean" if clean else "hangup_abnormal", time.monotonic())
                )
                self._closed.set()
                self._ack.set()
                self._dtmf_ack.set()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._trace.feed(line.rstrip(), time.monotonic())
            if self._trace.clean_bye:
                self._finish(True)
            parts = line.strip().split()
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            name, code = parts[0], int(parts[1])
            with self._lock:
                if name == "RINGING" and not self._closed.is_set():
                    if not any(e.name == "ringing" for e in self._events):
                        self._events.append(Event("ringing", time.monotonic()))
                elif name == "ANSWERED" and not self._closed.is_set():
                    self.final_code = code
                    now = time.monotonic()
                    if not any(e.name == "ringing" for e in self._events):
                        self._events.append(Event("ringing", now))
                    if not self.answered:
                        self._events.append(Event("answered", now))
                elif name == "ENDED":
                    if self.final_code is None:
                        self.final_code = code
                    self._ended_at = time.monotonic()
                    if not self.answered:
                        self._finish(False)
                elif name == "CLEAN":
                    self._finish(True)
                elif name == "DTMF_RESULT":
                    self._dtmf_status = code
                    self._dtmf_ack.set()
        self._finish(False, "streaming helper exited unexpectedly")

    def _receive(self) -> None:
        while not self._closed.is_set():
            # A disconnected callback can precede the BYE transaction response.
            # Publish termination before ending receive so earshot sees the event.
            if self._ended_at is not None and time.monotonic() - self._ended_at > 4:
                self._finish(False)
                break
            try:
                packet = self._socket.recv(1024)
            except TimeoutError:
                continue
            except OSError:
                break
            if len(packet) == 329 and packet[:1] == b"P":
                captured = struct.unpack("!Q", packet[1:9])[0] / 1e9
                if time.clock_gettime(time.CLOCK_MONOTONIC) - captured > 0.06:
                    self._finish(False, "receive IPC fell behind the media clock")
                    continue
                try:
                    self._frames.put_nowait((captured, packet[9:]))
                except queue.Full:
                    self._finish(False, "receive consumer fell behind the media clock")
            elif len(packet) == 13 and packet[:1] == b"A":
                if struct.unpack("!I", packet[1:5])[0] == self._sequence:
                    self._consumed_at = struct.unpack("!Q", packet[5:])[0] / 1e9
                    self._ack.set()
            else:
                self._finish(False, "invalid helper media frame")

    def _expire(self) -> None:
        with self._lock:
            if self.answered or self._closed.is_set():
                return
            self.timed_out = True
            self._finish(False)
        self.hangup()

    def recv_audio(self) -> Iterator[bytes]:
        if not self._receive_lock.acquire(blocking=False):
            raise SiplingError("only one receive consumer is supported")
        try:
            while not self._closed.is_set():
                try:
                    received, frame = self._frames.get(timeout=0.02)
                except queue.Empty:
                    continue
                if time.clock_gettime(time.CLOCK_MONOTONIC) - received > 0.06:
                    self._finish(False, "receive consumer fell behind the media clock")
                if not self._closed.is_set():
                    yield frame
            if self._error:
                raise SiplingError(self._error)
        finally:
            self._receive_lock.release()

    def _active(self) -> None:
        if self._closed.is_set() or self._ended_at is not None or not self.answered:
            raise SiplingError("call is not active")

    def send_audio(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError("PCM16LE must have an even byte length")
        with self._send_lock:
            self._active()
            began = time.clock_gettime(time.CLOCK_MONOTONIC)
            for offset in range(0, len(pcm), self.frame_bytes):
                self._active()
                self._sequence = (self._sequence + 1) % 2**32
                self._ack.clear()
                chunk = pcm[offset : offset + self.frame_bytes]
                self._socket.send(
                    b"S" + struct.pack("!I", self._sequence) + chunk.ljust(320, b"\0")
                )
                if not self._ack.wait(1):
                    self._finish(False, "media clock stopped during playback")
                    raise SiplingError("media clock stopped during playback")
                self._active()
                expected = began + offset / (2 * self.sample_rate)
                now = time.clock_gettime(time.CLOCK_MONOTONIC)
                if self._consumed_at - expected > 0.06 or now - self._consumed_at > 0.06:
                    self._finish(False, "send fell behind the media clock")
                    raise SiplingError("send fell behind the media clock")
            # ACK means the final frame entered the conference; wait for its end.
            if pcm and self._closed.wait(0.02):
                raise SiplingError("call ended during playback")

    def _command(self, value: str) -> None:
        with self._command_lock:
            try:
                assert self._process.stdin is not None
                self._process.stdin.write(value + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                raise SiplingError("streaming helper unavailable") from None

    def send_dtmf(self, digits: str) -> None:
        if not digits or len(digits) > 16 or any(d not in "0123456789*#ABCD" for d in digits):
            raise ValueError("expected 1 to 16 RFC4733 digits")
        with self._send_lock:
            self._active()
            self._dtmf_ack.clear()
            self._dtmf_status = None
            self._command("d" + digits)
            if not self._dtmf_ack.wait(1) or self._dtmf_status != 0:
                raise SiplingError("RFC4733 DTMF unavailable on this call")

    def hangup(self) -> None:
        # Unblock media immediately, even when the peer never acknowledges BYE.
        with self._hangup_lock:
            if self._local_hangup:
                return
            self._local_hangup = True
            self._closed.set()
            self._ack.set()
            self._dtmf_ack.set()
            self._timer.cancel()
            try:
                self._command("h")
                self._command("q")
            except SiplingError:
                pass
            try:
                self._process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._reader.join(timeout=1)
            self._media.join(timeout=1)
            self._socket.close()
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass
            if self._process.stdout:
                self._process.stdout.close()
            with self._lock:
                if not any(e.name.startswith("hangup_") for e in self._events):
                    self._events.append(Event("hangup_abnormal", time.monotonic()))

    def __enter__(self) -> StreamingCall:
        return self

    def __exit__(self, *_: object) -> None:
        self.hangup()


def call(
    uri: str, *, transport: str = "tcp", timeout: float = 15, no_redact: bool = False
) -> StreamingCall:
    """Return promptly; setup/answer events and PCM arrive asynchronously.

    Requires the separately compiled sipling-stream helper. Use probe() for the
    v0.1 blocking pjsua CLI/recording API. No silent non-streaming fallback.
    """
    if transport not in {"tcp", "udp"}:
        raise ValueError("transport must be tcp or udp")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not re.fullmatch(r"sip:[^\s<>]+", uri, re.IGNORECASE):
        raise ValueError("expected a sip: URI without whitespace or angle brackets")
    if any(c in uri for c in '?"\\'):
        raise ValueError("URI headers and quoted components are unsupported")
    existing = re.search(r";transport=([^;]+)", uri, re.IGNORECASE)
    if existing and existing[1].lower() != transport:
        raise ValueError("URI transport conflicts with selected transport")
    target = uri if existing else uri + f";transport={transport}"
    return StreamingCall(target, transport, timeout, no_redact)
