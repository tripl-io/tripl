"""@mentions in comment bodies (#259).

The composer inserts ``@[Display Name](user-uuid)``; that token is the only
thing parsed. Plain ``@name`` text is never resolved to a user: a name is not
an identity, and guessing would notify the wrong person.
"""

from __future__ import annotations

import re
import uuid

MENTION_PATTERN = re.compile(
    r"@\[(?P<name>[^\]\n]{1,200})\]\((?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)"
)

# How many distinct people one comment may notify by mention.
MAX_MENTIONS_PER_COMMENT = 50


def mentioned_user_ids(body: str) -> list[uuid.UUID]:
    """Distinct mentioned user ids, in order of first appearance."""
    seen: dict[uuid.UUID, None] = {}
    for match in MENTION_PATTERN.finditer(body or ""):
        try:
            user_id = uuid.UUID(match.group("id"))
        except ValueError:
            continue
        seen.setdefault(user_id, None)
        if len(seen) >= MAX_MENTIONS_PER_COMMENT:
            break
    return list(seen)


def plain_text(body: str) -> str:
    """The body with every mention token rendered as ``@Name`` (for titles, emails)."""
    return MENTION_PATTERN.sub(lambda m: f"@{m.group('name')}", body or "")


def excerpt(body: str, limit: int = 280) -> str:
    """A short single-paragraph excerpt of a comment for a notification body."""
    text = " ".join(plain_text(body).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
