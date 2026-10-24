"""Run: python examples/pair.py [--uri sip:AUTHORIZED_AGENT@HOST]."""

from __future__ import annotations

import argparse
import json
import time
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from earshot import CallSession
from earshot.sipling import SiplingTransport
from local_agent import LocalAgent, tone


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri")
    parser.add_argument("--caller-wav", type=Path, help="mono PCM16LE, 8000 Hz")
    parser.add_argument("--transport", choices=("udp", "tcp"), default="udp")
    args = parser.parse_args()
    with LocalAgent(args.transport) if args.uri is None else nullcontext(None) as agent:
        with CallSession(SiplingTransport(), max_duration=20) as session:
            before = time.monotonic()
            session.dial(agent.uri if agent else args.uri, transport=args.transport)
            dial_return = time.monotonic() - before
            assert session.answered_within(5)
            assert session.first_audio_within(2)
            session.listen(0.12, timeout=3)
            if agent:
                agent.greeting(False)
                time.sleep(0.12)
            before = time.monotonic()
            session.play(args.caller_wav if args.caller_wav else tone(0.3))
            send_elapsed = time.monotonic() - before
            if agent:
                # On-command reply; this fixture has no speech recognizer.
                agent.greeting(True)
            session.listen(0.12, timeout=3)
            session.send_dtmf("12#")
            if agent:
                deadline = time.monotonic() + 3
                while len(agent.dtmf) < 3 and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert agent.dtmf == ("1", "2", "#"), agent.dtmf
        print(
            json.dumps(
                {
                    "dial_return_seconds": dial_return,
                    "send_seconds": send_elapsed,
                    "metrics": asdict(session.metrics()),
                    "rfc4733_received": list(agent.dtmf) if agent else None,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
