"""Command-line entry point for outbound SIP practice calls.

Run this from the repository root.  The normal LiveKit worker remains
``backend/src/agent.py``; this command only orchestrates outbound calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from outbound_calling import (  # noqa: E402
    ConfigurationError,
    OutboundCallService,
    OutboundConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceForBharat outbound SIP calling")
    parser.add_argument(
        "--outbound", action="store_true", help="Place an outbound SIP call"
    )
    parser.add_argument("--to", dest="destination", help="Override OUTBOUND_SIP_URI")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and print the call plan only"
    )
    parser.add_argument(
        "--check-telephony",
        action="store_true",
        help="Validate telephony configuration without calling",
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Unique request key; generated when omitted",
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow reuse of an idempotency key"
    )
    args = parser.parse_args()
    if not args.outbound and not args.check_telephony:
        parser.error("Choose --outbound or --check-telephony.")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = OutboundConfig.from_env(args.destination)
    service = OutboundCallService(config)
    try:
        if args.check_telephony:
            service.dry_run()
            print("Telephony configuration: valid")
            print(
                f"Agent: {config.agent_name}; destination: {service.dry_run()['destination']}"
            )
            return 0
        if args.dry_run:
            print("Outbound dry run: no real call will be made.")
            print(json.dumps(service.dry_run(), indent=2))
            return 0
        result = asyncio.run(
            service.place_call(
                idempotency_key=args.idempotency_key or __import__("uuid").uuid4().hex,
                force=args.force,
            )
        )
        print(f"Room created: {result.room_name}")
        print(f"Outcome: {result.outcome.value}")
        return 0 if result.outcome.value in {"completed", "answered"} else 2
    except ConfigurationError as exc:
        print(f"Telephony configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
