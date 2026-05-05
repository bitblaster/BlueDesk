#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
BlueDesk status helper.

Reads ~/.cache/bluedesk/state.json (kept fresh by bluedesk_client.py:
updated on click, on timer reset, and on the break -> active edge to
account for break time pushing the deadline forward).

Output examples:
  23m -> STAND
  now -> SIT          (within last minute or just past the deadline)
  ?                   (no state file yet: client not running)
"""

import json
import os
import sys
import time
from pathlib import Path


CACHE_DIR  = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bluedesk"
STATE_FILE = CACHE_DIR / "state.json"


def main() -> int:
    if not STATE_FILE.exists():
        print("?")
        return 0

    try:
        data = json.loads(STATE_FILE.read_text())
        next_switch = float(data["next_switch_unix"])
        label = data.get("next_preset_label", "?")
    except Exception:
        print("?")
        return 0

    remaining = next_switch - time.time()

    if remaining > 60:
        # Round up to the nearest minute so we never show "0m" while
        # there's still real time left
        minutes = int((remaining + 59) // 60)
        text = f"{minutes}m"
    else:
        # Less than a minute or already past
        text = "now"

    print(f"{text} ➤ {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
