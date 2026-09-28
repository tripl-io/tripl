"""incident_summaries.facts_hash no longer covers hrefs (F20 PR8, critique #21)

``compute_facts_hash`` hashed each fact's ``[kind, text, href]``. F20 PR8 makes
every fact link org-qualified (``/p/{slug}/...`` -> ``/o/{org}/p/{slug}/...``),
which would have changed every current hash and marked every cached summary
stale, so each would be regenerated through the LLM once for nothing but a link
shape. The hash now covers ``[kind, text]`` only; this migration rewrites the
stored hashes to that definition so a summary that was fresh stays fresh.

Only a row whose stored hash IS the old hash of its own stored facts is
rewritten. Anything else (a summary cached under an older prompt version, or
hand-edited) did not match its facts before and is left alone, so it stays
exactly as stale as it was. The prompt version is frozen here on purpose: this
migration describes the stored rows as of this revision, not whatever
``PROMPT_VERSION`` later becomes.

Stored hrefs are not rewritten on upgrade: they are completed with the
organization when a summary is read (``incident_summary_service._stored_fact``).

Downgrade is the mirror image, with one extra step. A summary generated after
the upgrade stores org-qualified hrefs (``/o/{org}/p/...``), while the pre-PR8
code rebuilds org-less ``/p/...`` hrefs and hashes those. Hashing the stored
``/o/`` hrefs would make every such summary stale and regenerate it through the
LLM, so downgrade strips the organization prefix first and writes the stripped
hrefs back, which also gives the pre-PR8 frontend links it can route.

Revision ID: d4e8f1a2b3c5
Revises: e5b7d9f1a3c6
Create Date: 2026-09-28 20:00:00.000000

"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8f1a2b3c5"
down_revision: str | None = "e5b7d9f1a3c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROMPT_VERSION = "incident-summary-v2"

_summaries = sa.table(
    "incident_summaries",
    sa.column("id", sa.Uuid()),
    sa.column("facts_hash", sa.String(64)),
    sa.column("facts", sa.JSON()),
)


def _hash(facts: list[dict[str, Any]], *, with_href: bool) -> str:
    rows = [
        [fact.get("kind"), fact.get("text"), fact.get("href")]
        if with_href
        else [fact.get("kind"), fact.get("text")]
        for fact in facts
    ]
    canonical = json.dumps(
        {"version": _PROMPT_VERSION, "facts": rows},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_href(href: Any) -> Any:
    """``/o/{org}/p/...`` -> ``/p/...``; anything else unchanged.

    Mirrors ``notification_producers._url_forms``; frozen here like the prompt
    version, so a later change to link shapes does not rewrite this revision.
    """
    if not isinstance(href, str) or not href.startswith("/o/"):
        return href
    parts = href.split("/", 3)
    if len(parts) < 4 or not parts[3].startswith("p/"):
        return href
    return f"/{parts[3]}"


def _load_facts(facts: Any) -> list[dict[str, Any]] | None:
    if isinstance(facts, str):
        facts = json.loads(facts)
    return facts if isinstance(facts, list) else None


def _rows(bind: sa.Connection) -> list[tuple[Any, str, list[dict[str, Any]]]]:
    rows = bind.execute(sa.select(_summaries.c.id, _summaries.c.facts_hash, _summaries.c.facts))
    loaded = []
    for row_id, stored_hash, raw in rows.all():
        facts = _load_facts(raw)
        if facts is not None:
            loaded.append((row_id, stored_hash, facts))
    return loaded


def _rehash_up(bind: sa.Connection) -> None:
    for row_id, stored_hash, facts in _rows(bind):
        if stored_hash != _hash(facts, with_href=True):
            continue
        bind.execute(
            sa.update(_summaries)
            .where(_summaries.c.id == row_id)
            .values(facts_hash=_hash(facts, with_href=False))
        )


def _rehash_down(bind: sa.Connection) -> None:
    for row_id, stored_hash, facts in _rows(bind):
        if stored_hash != _hash(facts, with_href=False):
            continue
        legacy = [{**fact, "href": _legacy_href(fact.get("href"))} for fact in facts]
        bind.execute(
            sa.update(_summaries)
            .where(_summaries.c.id == row_id)
            .values(facts_hash=_hash(legacy, with_href=True), facts=legacy)
        )


def upgrade() -> None:
    _rehash_up(op.get_bind())


def downgrade() -> None:
    _rehash_down(op.get_bind())
