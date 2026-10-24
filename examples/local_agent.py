"""Local pjsua UAS fixture; no external signaling or real numbers."""

from __future__ import annotations

import math
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path


def tone(seconds: float) -> bytes:
    return b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / 8000)))
        for i in range(int(seconds * 8000))
    )


class LocalAgent:
    """Repeated greeting player and CLI-controlled conference loopback.

    Slot 1 is the WAV player and slot 2 the single call (--no-tones).
    The player contains 200 ms energy followed by 600 ms silence.
    """

    def __init__(self, transport: str = "udp", greeting_seconds: float = 0.2) -> None:
        import pty

        executable = shutil.which("pjsua")
        if executable is None:
            raise RuntimeError("pjsua not found")
        self._temp = tempfile.TemporaryDirectory(prefix="sipling-agent-")
        path = Path(self._temp.name) / "greeting.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "none"))
            wav.writeframes(tone(greeting_seconds) + bytes(9600))
        # Reserve a loopback signaling port until immediately before launch.
        with socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM
        ) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.uri = f"sip:local-agent@127.0.0.1:{port}"
        master, slave = pty.openpty()
        self._master = master
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._dtmf: list[str] = []
        self._lock = threading.Lock()
        try:
            self.process = subprocess.Popen(
                [
                    executable,
                    "--null-audio",
                    "--clock-rate=8000",
                    "--no-tones",
                    "--no-vad",
                    "--no-color",
                    "--max-calls=1",
                    "--auto-answer=200",
                    "--auto-play",
                    "--bound-addr=127.0.0.1",
                    f"--local-port={port}",
                    "--rtp-port=0",
                    "--no-udp" if transport == "tcp" else "--no-tcp",
                    "--dis-codec=*",
                    "--add-codec=PCMU",
                    f"--play-file={path}",
                ],
                stdin=subprocess.PIPE,
                stdout=slave,
                stderr=slave,
                text=True,
            )
        finally:
            os.close(slave)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if not self._ready.wait(5):
            self.close()
            raise RuntimeError("local pjsua fixture did not become ready")

    @property
    def dtmf(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._dtmf)

    def _read(self) -> None:
        pending = b""
        while not self._stop.is_set():
            if not select.select([self._master], [], [], 0.02)[0]:
                continue
            try:
                chunk = os.read(self._master, 65536)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if b"state changed: STARTING --> RUNNING" in line:
                    self._ready.set()
                # pjsua distinguishes RFC2833 from SIP INFO in this callback.
                match = re.search(rb"Incoming DTMF on call \d+: ([0-9*#ABCD]).*using RFC2833", line)
                if match:
                    with self._lock:
                        self._dtmf.append(match[1].decode())

    def command(self, command: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def greeting(self, enabled: bool) -> None:
        self.command(("cc" if enabled else "cd") + " 1 2")

    def echo(self, enabled: bool) -> None:
        self.command(("cc" if enabled else "cd") + " 2 2")

    def close(self) -> None:
        if self.process.poll() is None:
            self.command("q")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._stop.set()
        self._reader.join(timeout=1)
        os.close(self._master)
        if self.process.stdin:
            self.process.stdin.close()
        self._temp.cleanup()

    def __enter__(self) -> LocalAgent:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
