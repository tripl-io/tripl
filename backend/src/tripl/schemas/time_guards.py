"""One rule for the request fields that say "stay quiet until then".

Five request bodies across three domains carry such an instant: the incident
inbox's ``muted_until`` (``AlertInboxActionRequest`` and its bulk twin), the
``snoozed_until`` of a schema drift and of a variable value drift, and
``MonitorMuteRequest.muted_until``. Only the last one ever refused an instant
that had already passed — ``mute_monitor`` answers 422 "muted_until must be in
the future" — while the other four accepted it, stored it and returned 200.
A sixth field carries such an instant and is deliberately left
taking a spent one; see WHERE THE RULE STOPS below.

That 200 is exactly the kind of lie the validators it slipped past were written
to catch. ``AlertInboxActionRequest.validate_action`` opens with "Reject the
action bodies that would be accepted and then do nothing", and
``EventCommentActionRequest`` already refuses a ``snoozed_until`` on a non-snooze
action because "accepting and discarding it would hide the mistake". A silence
that ended before it began is discarded just as completely, only a moment later
and by a different reader: whether a silence is still in force is worked out WHEN
THE ROW IS READ — ``_effective_inbox_status`` and the drift open-filters both
compare the stored instant against now — so an expired one leaves the card open
and the drift listed the instant the write commits. Two Mute buttons on one
screen disagreed about it: the monitor's refused the body, the incident's took
it.

READ-TIME LAPSE IS NOT WHAT THIS GUARD CHANGES, and the two must not be
confused. A silence set for Thursday and read on Friday is open again on
purpose — "the state is worked out when the thread is read, not written back by
a background job, so it is never briefly wrong" (website/docs/use/
feature-reference.md), and several tests pin it. This module refuses only a
deadline that was ALREADY behind the clock when it arrived: a request no reader
could ever have honoured, as opposed to one that time caught up with.

WHERE THE RULE STOPS. ``EventCommentActionRequest.snoozed_until`` is that sixth
field, and it is deliberately NOT guarded. There is no second button on that
screen refusing the same body, so there is no asymmetry to settle; and its reply
carries the stored ``snoozed_until`` back beside ``status``, which both
``event_comment_service.unanswered_clause`` and the client's
``commentThreadState.isThreadUnanswered`` read as "open again", badge and all.
A past instant there tells the operator the truth on the next render rather than
a comfortable lie, so there is nothing for this module to catch —
``test_event_comments.py::test_a_lapsed_snooze_counts_as_unanswered_again``
posts one on purpose, and ``test_batch4_services.py`` pins that it still may.

WHY THE RULE LIVES HERE. It is wanted in three schema modules that share no
domain. Reaching it through ``alerting_validation`` would make the two drift
schemas import the alerting module for one comparison, and copying it into each
validator would be the same rule written out four more times beside
``mute_monitor``'s own — which is how the inbox and the monitor came to disagree
in the first place.
``schemas/text_filters.py`` settled this shape for the same class of problem one
layer over: the guard belongs where every value must pass before a service can
see it.

THE NAIVE-TO-UTC COERCION IS NOT DECORATION. Pydantic parses
``"2026-09-20T10:00:00"`` — a body the published schema accepts, since
``format: date-time`` has never demanded an offset — into a NAIVE datetime, and
comparing one of those against ``datetime.now(UTC)`` raises TypeError, which only
``main.py``'s catch-all handles. That was a real bug on the monitor route, and
adding the comparison to four more bodies without the coercion would have been
four fresh copies of it. A bare instant is read as UTC and never as the host's
local time, the same reading ``core.bucketing.to_utc`` and
``_alerting_deliveries._as_utc`` give one.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["require_future_instant"]


def require_future_instant(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` as an aware UTC instant, refusing one that has passed.

    ``field_name`` is interpolated rather than fixed so the four fields that use
    this read back the way the monitor route's own refusal always has —
    "muted_until must be in the future" — instead of four sentences for one rule.

    Raises ``ValueError``, not ``HTTPException``, because every caller is a
    pydantic ``@model_validator(mode="after")``: pydantic wraps it and FastAPI
    answers 422 — the same STATUS ``mute_monitor`` refuses with, and the whole of
    what the two surfaces share. Their bodies differ and nothing here can close
    that gap: ``mute_monitor`` raises ``HTTPException(422, detail=...)`` whose
    ``detail`` is a plain string, while this path produces FastAPI's list of
    error objects.

    In that list THE OFFENDING FIELD IS NAMED IN ``msg`` AND NOWHERE ELSE. A
    ValueError raised from a model validator is located at the model, so pydantic
    reports ``loc: ()`` and FastAPI publishes it as ``["body"]``; only a
    ``@field_validator`` would put the field itself in ``loc``, and all four
    callers are gated on ``self.action``, which a field validator can reach only
    by digging it back out of ``ValidationInfo.data``. That is exactly why
    ``field_name`` is interpolated into the message above rather than left to the
    error's location — ``msg`` is the one place the name survives to a reader.
    ``formatValidationDetail`` (``frontend/src/api/client.ts``) prints it alone,
    pydantic's ``Value error,`` prefix and all, because its ``loc``-minus-``body``
    path comes out empty. So the sentence reaches the operator intact, but a
    client that scopes a 422 to a form control BY ``loc`` has nothing to match on.
    ``test_batch4_services.py`` pins both shapes.
    """
    # Normalize BEFORE comparing, never after: the point of doing it here is
    # that the comparison below cannot be handed a naive value.
    instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    # ``<=`` and not ``<``, matching ``mute_monitor``: an instant that is already
    # now is a silence with no duration left, and the two mute surfaces must draw
    # the boundary in the same place or the asymmetry is back one second wide.
    if instant <= datetime.now(UTC):
        raise ValueError(f"{field_name} must be in the future")
    return instant
