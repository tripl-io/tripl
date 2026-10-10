#!/usr/bin/env python3
"""Prove a CI gate ran: fail unless its pytest JUnit report shows real results.

    check-junit.py REPORT LABEL [--allow-xfail]

Every job that gates on something outside the runner (a warehouse, a database)
also sets a *_REQUIRED variable that turns "unreachable" into a failure. This
is the second half: a broken container, a mistyped marker or a collection
error can still leave a job that tested nothing and reports green. So the run
fails when the report is missing, when it holds no test, when any test was
skipped, and when any failed or errored.

pytest writes an xfail into the report as a skip of type `pytest.xfail`. It
counts as a skip, so it fails the run, unless --allow-xfail is given: only for
a suite that records known faults as xfails (the search relevance harness).

CI runs it as the step after each gate's pytest, with `if: always()`. The
messages are GitHub Actions annotations.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

XFAIL = "pytest.xfail"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("report", help="the JUnit XML pytest wrote (--junitxml)")
    parser.add_argument("label", help="what the gate is, for the messages")
    parser.add_argument(
        "--allow-xfail",
        action="store_true",
        help="accept tests that pytest reports as xfailed",
    )
    args = parser.parse_args(argv)
    label = args.label

    try:
        root = ET.parse(args.report).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::error::no {label} JUnit report: {exc}. The gate did not run.")
        return 1

    total = 0
    xfailed: list[str] = []
    skipped: list[str] = []
    broken: list[str] = []
    for case in root.iter("testcase"):
        total += 1
        name = case.get("name") or "<unnamed>"
        for node in case.findall("skipped"):
            (xfailed if node.get("type") == XFAIL else skipped).append(name)
        if case.find("failure") is not None or case.find("error") is not None:
            broken.append(name)
    if not args.allow_xfail:
        skipped, xfailed = skipped + xfailed, []

    print(
        f"{label}: {total} collected, {len(xfailed)} xfailed, "
        f"{len(skipped)} skipped, {len(broken)} failed or errored"
    )
    if total == 0:
        print(
            f"::error::0 {label} tests ran. "
            "A gate that collects nothing is a no-op reporting green."
        )
        return 1
    if skipped:
        print(
            f"::error::{len(skipped)} {label} test(s) skipped in CI: {', '.join(skipped)}. "
            "What the gate runs against was unreachable, so it tested nothing."
        )
        return 1
    if broken:
        print(f"::error::{len(broken)} {label} test(s) failed or errored: {', '.join(broken)}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
