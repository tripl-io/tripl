"""One rule for free-text query filters, applied where they enter the process.

Postgres ``text`` cannot represent U+0000, so a filter value carrying one aborts
inside asyncpg (``CharacterNotInRepertoireError``) before any SQL runs, and the
caller gets a 500. That was fixed for ``/search`` by sanitising inside the
search funnel — genuinely the single funnel for ``/search`` and
``ai_service.ask_plan``. But the defect class is "user text binds
straight into a Postgres parameter", and a review found more query parameters,
across five routers, doing exactly that through ``ILIKE``.

Sanitising in each service would be the same rule written seven times, and this
repository has lost production twice to a rule written more than once. So it is
applied here instead, as a type on the route parameter: that is the one place
every value must pass before a service can see it. A new free-text filter gets
the behaviour by declaring the type, and no service can receive an unsanitised
string without someone editing a signature.

Why strip rather than reject with 422 — and note this is NOT the argument
``sanitize_query`` makes for ``/search``, which claims the rewrite is a no-op on
the result set. That claim does not hold here: ``ILIKE '%check\\x00out%'`` matches
nothing while ``ILIKE '%checkout%'`` matches, so stripping genuinely widens the
filter.

It is still the right call, for a different reason. The columns these filters
compare against are ``text``/``varchar``, so no stored value can contain a NUL,
which leaves a laced filter three possible fates: a 500 (what happened), an
always-empty result (correct and useless), or the value the user plainly meant.
Only the third is ever of use to anyone, no client can be relying on either of
the others, and it is what ``/search`` already does — so the alternative is not
"stricter", it is "inconsistent". A filter that must match nothing is not a
result worth preserving.

That reasoning does NOT generalise past U+0000: every other character carries
filtering meaning and can legitimately appear in stored data, so this removes
exactly U+0000 and leaves everything else alone.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator

NUL = "\x00"


def strip_nul_bytes(value: Any) -> Any:
    """Drop U+0000 from a string; see the module docstring for why that is the right call.

    Deliberately NOT described as lossless: for a substring filter it is not, and
    the paragraph above spells out why it is right anyway. This line used to
    claim the opposite of the argument it points at (Copilot, PR #78).

    Non-strings pass through untouched so pydantic still reports its own type
    error for them, rather than this raising ``AttributeError`` and turning a
    422 into a 500 — the exact trade this module exists to stop making.
    """
    return value.replace(NUL, "") if isinstance(value, str) else value


# Declare a free-text query filter as ``FreeTextFilter | None = None``.
FreeTextFilter = Annotated[str, BeforeValidator(strip_nul_bytes)]
