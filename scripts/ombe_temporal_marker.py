#!/usr/bin/env python3
"""Append an operator action timestamp during an O-MBE temporal test.

Run on the same workstation as Growth Monitor immediately before or after a
manual window, cable, power, or process action.  The marker file is separate
from the GUI-owned trace so concurrent appends cannot corrupt either stream.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="operator_actions.jsonl")
    parser.add_argument("label", help="Short action label, e.g. mistral_close")
    parser.add_argument("--phase", choices=("before", "after", "note"), default="note")
    parser.add_argument(
        "--source",
        choices=("rheed", "pyrometer", "mistral", "evap", "gui", "computer"),
        default="gui",
    )
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "label": args.label,
        "phase": args.phase,
        "source": args.source,
        "notes": args.notes,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds",
        ),
        "recorded_local": datetime.now().astimezone().isoformat(
            timespec="milliseconds",
        ),
        "recorded_monotonic_ns": time.perf_counter_ns(),
        "pid": os.getpid(),
    }
    with args.output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
