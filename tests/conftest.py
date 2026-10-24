from __future__ import annotations

import contextlib
import re
import socket
import struct
import threading
import time
from collections.abc import Iterator

import pytest


class MockAgent:
    """A loopback UAS, not a fake pjsua process; frames TCP using Content-Length."""

    def __init__(
        self,
        transport: str,
        code: int = 200,
        audio: bool = True,
        remote_bye: bool = False,
        codec: str = "PCMU",
        drop_bye: bool = False,
    ) -> None:
        self.transport, self.code, self.audio = transport, code, audio
        self.remote_bye = remote_bye
        self.drop_bye = drop_bye
        self.codec = codec
        self.payload_type = {"PCMU": 0, "PCMA": 8, "G722": 9}[codec]
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM
        )
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        if transport == "tcp":
            self.sock.listen()
        self.sock.settimeout(0.1)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0))
        self.rtp.settimeout(0.1)
        self.outbound_rtp = threading.Event()
        self.stop = threading.Event()
        self.methods: list[str] = []
        self.errors: list[Exception] = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.media: threading.Thread | None = None
        self.conn: socket.socket | None = None
        self.invite = ""
        self.thread.start()

    @property
    def uri(self) -> str:
        return f"sip:secret@127.0.0.1:{self.port}"

    def header(self, msg: str, name: str) -> str:
        aliases = {"Via": "v", "From": "f", "To": "t", "Call-ID": "i", "Contact": "m"}
        match = re.search(rf"(?im)^(?:{name}|{aliases.get(name, name)}):\s*(.+)$", msg)
        assert match, (name, msg)
        return match[1].strip()

    def response(self, msg: str, code: int, body: str = "") -> bytes:
        reason = {100: "Trying", 180: "Ringing", 200: "OK", 486: "Busy Here"}[code]
        headers = [f"SIP/2.0 {code} {reason}"]
        for name in ["Via", "From", "To", "Call-ID", "CSeq"]:
            value = self.header(msg, name)
            if name == "To" and "tag=" not in value:
                value += ";tag=mock"
            headers.append(f"{name}: {value}")
        headers += [f"Contact: <sip:agent@127.0.0.1:{self.port};transport={self.transport}>"]
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append(f"Content-Length: {len(body.encode())}")
        return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()

    def tone(self, address: tuple[str, int]) -> None:
        # Latch to the caller's observed source, as the target platform does.
        while not self.stop.is_set():
            try:
                packet, address = self.rtp.recvfrom(4096)
                if len(packet) >= 12:
                    self.outbound_rtp.set()
                    break
            except TimeoutError:
                continue
        seq = 0
        while not self.stop.wait(0.02):
            # Alternating G.711 mu-law values encode an audible square wave.
            payload = bytes([0x80, 0x00]) * 80 if self.audio else b"\xff" * 160
            packet = (
                struct.pack("!BBHII", 0x80, self.payload_type, seq % 65536, seq * 160, 12345)
                + payload
            )
            self.rtp.sendto(packet, address)
            seq += 1

    def handle(self, msg: str, address: tuple[str, int] | None) -> None:
        method = msg.split()[0]
        self.methods.append(method)

        def send(data: bytes) -> None:
            if self.conn:
                self.conn.sendall(data)
            else:
                assert address
                self.sock.sendto(data, address)

        if method == "INVITE":
            self.invite = msg
            send(self.response(msg, 100))
            time.sleep(0.03)
            send(self.response(msg, 180))
            time.sleep(0.03)
            if self.code != 200:
                send(self.response(msg, self.code))
                return
            port = re.search(r"m=audio (\d+)", msg)
            host = re.search(r"c=IN IP4 ([\d.]+)", msg)
            assert port and host
            body = (
                "v=0\r\no=mock 1 1 IN IP4 127.0.0.1\r\ns=mock\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {self.rtp.getsockname()[1]} RTP/AVP {self.payload_type}\r\n"
                f"a=rtpmap:{self.payload_type} {self.codec}/8000\r\na=sendrecv\r\n"
            )
            send(self.response(msg, 200, body))
            self.media = threading.Thread(
                target=self.tone, args=((host[1], int(port[1])),), daemon=True
            )
            self.media.start()
        elif method == "ACK" and self.remote_bye:
            contact = self.header(self.invite, "Contact").strip("<>")
            headers = [
                f"BYE {contact} SIP/2.0",
                f"Via: SIP/2.0/{self.transport.upper()} 127.0.0.1:{self.port};branch=z9hG4bKbye",
                f"From: {self.header(self.invite, 'To')};tag=mock",
                f"To: {self.header(self.invite, 'From')}",
                f"Call-ID: {self.header(self.invite, 'Call-ID')}",
                "CSeq: 2 BYE",
                "Max-Forwards: 70",
                "Content-Length: 0",
                "",
                "",
            ]
            send("\r\n".join(headers).encode())
        elif method in ("BYE", "CANCEL"):
            if method == "BYE" and self.drop_bye:
                return
            send(self.response(msg, 200))

    def run(self) -> None:
        buffer = b""
        try:
            while not self.stop.is_set():
                try:
                    if self.transport == "tcp":
                        if self.conn is None:
                            self.conn, _ = self.sock.accept()
                            self.conn.settimeout(0.1)
                        data = self.conn.recv(65536)
                        if not data:
                            break
                        buffer += data
                        while b"\r\n\r\n" in buffer:
                            head, rest = buffer.split(b"\r\n\r\n", 1)
                            length = re.search(rb"(?im)^(?:Content-Length|l):\s*(\d+)", head)
                            size = int(length[1]) if length else 0
                            if len(rest) < size:
                                break
                            msg = head + b"\r\n\r\n" + rest[:size]
                            buffer = rest[size:]
                            self.handle(msg.decode(), None)
                    else:
                        data, address = self.sock.recvfrom(65536)
                        self.handle(data.decode(), address)
                except TimeoutError:
                    continue
        except Exception as exc:
            if not self.stop.is_set():
                self.errors.append(exc)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        if self.media:
            self.media.join(timeout=2)
        if self.conn:
            self.conn.close()
        self.sock.close()
        self.rtp.close()
        assert not self.errors


@pytest.fixture
def agent_factory() -> Iterator[object]:
    with contextlib.ExitStack() as stack:

        def create(transport: str = "tcp", **kwargs: object) -> MockAgent:
            agent = MockAgent(transport, **kwargs)
            stack.callback(agent.close)
            return agent

        yield create
