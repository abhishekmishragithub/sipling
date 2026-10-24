"""Measure the local clock and round-trip echo; prints sanitized JSON."""

from __future__ import annotations

import json
import math
import statistics
import struct
import threading
import time

from local_agent import LocalAgent, tone

from sipling import StreamingCall, call


def voiced(pcm: bytes) -> bool:
    return sum(v * v for v in struct.unpack("<160h", pcm)) / 160 > (32768 * 0.01) ** 2


def receive(handle: StreamingCall, frames: list[tuple[float, bytes]], errors: list[str]) -> None:
    try:
        for pcm in handle.recv_audio():
            frames.append((time.monotonic(), pcm))
    except Exception as exc:
        errors.append(type(exc).__name__)


def main() -> None:
    runs = []
    for transport in ("udp", "tcp"):
        for _ in range(3):
            with LocalAgent(transport) as agent:
                before = time.monotonic()
                with call(agent.uri, transport=transport) as handle:
                    returned = time.monotonic() - before
                    frames: list[tuple[float, bytes]] = []
                    errors: list[str] = []

                    receiver = threading.Thread(target=receive, args=(handle, frames, errors))
                    receiver.start()
                    deadline = time.monotonic() + 3
                    while not handle.answered:
                        if time.monotonic() > deadline:
                            raise RuntimeError("no local answer")
                        time.sleep(0.01)
                    agent.greeting(False)
                    time.sleep(0.3)
                    agent.echo(True)
                    time.sleep(0.1)
                    index = len(frames)
                    started = time.monotonic()
                    handle.send_audio(tone(0.3))
                    elapsed = time.monotonic() - started
                    time.sleep(0.3)
                    echo = [at for at, pcm in frames[index:] if voiced(pcm)]
                    assert echo and not errors
                    intervals = [b[0] - a[0] for a, b in zip(frames, frames[1:], strict=False)]
                    closer = threading.Thread(target=handle.hangup)
                    before_close = time.monotonic()
                    closer.start()
                    receiver.join(timeout=0.2)
                    unblock = time.monotonic() - before_close
                    assert not receiver.is_alive()
                    closer.join()
                    ordered = sorted(intervals)
                    runs.append(
                        {
                            "transport": transport,
                            "dial_return_ms": returned * 1000,
                            "send_300ms_ms": elapsed * 1000,
                            "receive_interval_mean_ms": statistics.mean(intervals) * 1000,
                            "receive_interval_min_ms": min(intervals) * 1000,
                            "receive_interval_p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1]
                            * 1000,
                            "receive_interval_max_ms": max(intervals) * 1000,
                            "echo_onset_ms": (echo[0] - started) * 1000,
                            "echo_voiced_span_ms": (echo[-1] - echo[0]) * 1000,
                            "hangup_receive_unblock_ms": unblock * 1000,
                        }
                    )
    print(json.dumps({"sample_rate": 8000, "frame_ms": 20, "runs": runs}, indent=2))


if __name__ == "__main__":
    main()
