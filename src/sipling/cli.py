"""CI entry point. Output never includes raw child diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from typing import NoReturn

from .core import SiplingError, call, redact


class RedactingParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        super().error(redact(message))


def main(argv: list[str] | None = None) -> int:
    parser = RedactingParser(prog="sipling")
    sub = parser.add_subparsers(dest="command", required=True)
    dial = sub.add_parser("call")
    dial.add_argument("uri")
    dial.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    dial.add_argument("--timeout", type=float, default=15)
    dial.add_argument("--expect-answer", action="store_true")
    dial.add_argument("--expect-audio", type=float, metavar="SECONDS")
    dial.add_argument("--json", action="store_true")
    dial.add_argument("--ladder-file")
    dial.add_argument("--no-redact", action="store_true")
    args = parser.parse_args(argv)
    try:
        with call(
            args.uri, transport=args.transport, timeout=args.timeout, no_redact=args.no_redact
        ) as result:
            audio_ok = True
            if args.expect_audio is not None:
                audio_ok = result.heard_audio(within=args.expect_audio)
        if args.ladder_file:
            result.save_ladder(args.ladder_file)
        print(
            json.dumps(result.to_dict())
            if args.json
            else f"{result.uri}: answered={result.answered} code={result.final_code} "
            f"codec={result.codec} clean_bye={result.clean_bye}"
        )
        return int((args.expect_answer and not result.answered) or not audio_ok)
    except (SiplingError, ValueError, OSError):
        message = "call failed: invalid input, local backend, or capture error (see README)"
        print(json.dumps({"error": message}) if args.json else message, file=sys.stderr)
        return 2
