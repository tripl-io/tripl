"""Line-free identities for ``tripl check`` findings, shared by every machine format.

A CI system tells a new finding from an old one by comparing fingerprints
between two pipelines: GitLab Code Quality reports "new" and "resolved" as a
plain set difference of them, and a baseline (or a SARIF viewer tracking an
alert) does the same. So a fingerprint must survive an edit ABOVE the call that
moves it down the file, and must not survive a change to the call itself.

It is therefore built from what the call IS, never from where it sits: the file
(relative to the repository, so two check configurations in one monorepo never
collide), the callee, the event type, the name and the field values, the
finding's code and field. Two identical calls in one file (the same event logged
from two branches of a ``when``) are told apart by their order among identical
calls, not by their lines. Nothing the plan says goes in either — not the
identity the server matched, not the message — so a plan edit that leaves a
finding standing leaves its fingerprint alone too.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from tripl_cli.check.model import CheckItem, CheckReport

# The SARIF ``partialFingerprints`` key, and the version mixed into every hash:
# a change to what is hashed must change the key, or a consumer would compare
# fingerprints of two different recipes and read every finding as new.
FINGERPRINT_KEY = "triplCheck/v2"


def site_key(item: CheckItem, path: str) -> str:
    """What a call site (or captured event) is, without its position in the file."""
    return json.dumps(
        [path, item.origin.snippet, item.event_type, item.name, sorted(item.fields.items())],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def finding_fingerprints(report: CheckReport) -> tuple[tuple[str, ...], ...]:
    """One fingerprint per finding, shaped like ``report.results`` and their ``findings``.

    Computed over the whole run, because the occurrence number of a call among
    identical calls in its file is only known from the calls before it.
    """
    sites: Counter[str] = Counter()
    fingerprints: list[tuple[str, ...]] = []
    for result in report.results:
        site = site_key(result.item, report.repo_path(result.item.origin))
        sites[site] += 1
        # Two findings of one code on one field (two policy rules, say) still
        # need two fingerprints, or GitLab would show one of them.
        repeats: Counter[tuple[str, str | None]] = Counter()
        own: list[str] = []
        for finding in result.findings:
            repeats[finding.code, finding.field] += 1
            material = json.dumps(
                [
                    FINGERPRINT_KEY,
                    site,
                    sites[site],
                    finding.code,
                    finding.field,
                    repeats[finding.code, finding.field],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            own.append(hashlib.sha256(material.encode()).hexdigest())
        fingerprints.append(tuple(own))
    return tuple(fingerprints)
