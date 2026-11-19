"""Synthetic redirect checkout used by the incident reproduction walkthrough."""

import json
import os
import sys
from pathlib import Path


def checkout(build: str) -> tuple[str, list[str]]:
    events = ["checkout_started"]
    order = {"state": "pending"}
    # In the suspect revision, returning from the payment app fires the
    # redirect callback but drops the analytics listener during remount.
    listeners = [] if build == "suspect" else [events.append]
    order["state"] = "paid"
    for notify in listeners:
        notify("purchase")
    return order["state"], events


if __name__ == "__main__":
    build = sys.argv[1]
    if build not in {"healthy", "suspect", "fixed"}:
        raise SystemExit("expected healthy, suspect or fixed")
    state, events = checkout(build)
    if state != "paid":
        raise SystemExit("checkout did not finish")
    Path(os.environ["TRIPL_JOURNEY_EVENTS"]).write_text(json.dumps(events))
