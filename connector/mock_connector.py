#!/usr/bin/env python3
"""Reference biometric-device connector.

This is the piece that will run on the restaurant's LAN next to the real
machine. It pulls punches off the device, maps them to employees and posts
them to the attendance API over HTTPS. Here the device is mocked, so the same
connector can be exercised without hardware.

    python connector/mock_connector.py --device-id 1 --api-key <key> \
        --api http://localhost:8000 --punches EMP001:09:02:IN EMP001:18:05:OUT

Design notes that carry over to the real thing:

* Every punch gets a stable ``external_ref``. The API is idempotent on it, so
  a retry after a network failure can never double-punch.
* Failed batches are queued to a local spool file and retried on the next run,
  which is what keeps attendance from silently disappearing.
* The connector never needs staff credentials - it authenticates with the
  device's own API key.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

SPOOL = Path(__file__).with_name("pending_events.json")
MAX_ATTEMPTS = 4


def read_device_punches(specs: list[str], day: date) -> list[dict]:
    """Stand-in for the vendor SDK call that reads new records off the machine.

    Each spec is ``EMPLOYEE_CODE:HH:MM:IN|OUT``.
    """
    events = []
    for spec in specs:
        code, hour, minute, kind = spec.split(":")
        stamp = datetime(day.year, day.month, day.day, int(hour), int(minute))
        events.append(
            {
                "employee_code": code.upper(),
                "event_type": "CHECK_IN" if kind.upper().startswith("IN") else "CHECK_OUT",
                "event_time": stamp.isoformat(),
                # Stable per punch: the device's own record id would be used here.
                "external_ref": f"{code.upper()}-{stamp.isoformat()}-{kind.upper()}",
            }
        )
    return events


def load_spool() -> list[dict]:
    if not SPOOL.exists():
        return []
    try:
        return json.loads(SPOOL.read_text())
    except json.JSONDecodeError:
        return []


def save_spool(events: list[dict]) -> None:
    if events:
        SPOOL.write_text(json.dumps(events, indent=2))
    elif SPOOL.exists():
        SPOOL.unlink()


def post_batch(api: str, device_id: int, api_key: str, events: list[dict]) -> dict:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/devices/{device_id}/sync",
        data=json.dumps({"events": events}).encode(),
        headers={"Content-Type": "application/json", "X-Device-Key": api_key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def sync(api: str, device_id: int, api_key: str, events: list[dict]) -> int:
    pending = load_spool() + events
    if not pending:
        print("Nothing to sync.")
        return 0

    delay = 2
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = post_batch(api, device_id, api_key, pending)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if 400 <= exc.code < 500 and exc.code != 429:
                print(f"Rejected by the server ({exc.code}): {body}", file=sys.stderr)
                save_spool(pending)
                return 1
            print(f"Attempt {attempt} failed ({exc.code}); retrying in {delay}s", file=sys.stderr)
        except urllib.error.URLError as exc:
            print(f"Attempt {attempt} failed ({exc.reason}); retrying in {delay}s", file=sys.stderr)
        else:
            print(
                f"Synced: received {result['received']}, accepted {result['accepted']}, "
                f"duplicates {result['duplicates']}, failed {result['failed']}"
            )
            for error in result.get("errors", []):
                print(f"  ! {error}", file=sys.stderr)
            # Anything the server refused stays queued for a human to look at.
            save_spool([] if result["failed"] == 0 else pending)
            return 0 if result["failed"] == 0 else 1

        time.sleep(delay)
        delay *= 2

    print("Giving up for now; events are spooled and will be retried.", file=sys.stderr)
    save_spool(pending)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--device-id", type=int, required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument(
        "--punches",
        nargs="*",
        default=[],
        metavar="CODE:HH:MM:IN|OUT",
        help="Punches the mock device reports, e.g. EMP001:09:02:IN",
    )
    args = parser.parse_args()

    events = read_device_punches(args.punches, date.fromisoformat(args.date))
    return sync(args.api, args.device_id, args.api_key, events)


if __name__ == "__main__":
    raise SystemExit(main())
