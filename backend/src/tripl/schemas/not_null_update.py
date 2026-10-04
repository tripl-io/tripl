"""Refuse an explicit ``null`` on a PATCH field that maps to a NOT NULL column.

Every partial-update schema in this package spells its fields ``X | None =
None`` so that ``model_dump(exclude_unset=True)`` can tell "not sent" from
"sent". That ``| None`` is a wire convention, not a statement about the column:
a client that sends ``{"status": null}`` gets past validation, the service
``setattr``s ``None`` onto a NOT NULL column, and the commit raises
``IntegrityError``, which the unhandled-exception handler renders as a blank 500.
An unset field never reaches a validator at all, so rejecting
``None`` at the boundary costs the honest client nothing and turns the 500 into
a 422 that names the field.

Only fields whose column is genuinely NOT NULL belong in a caller's frozenset —
``owner_id`` is nullable on purpose (an explicit null unassigns the owner) and
must stay out of it.
"""

from collections.abc import Collection, Mapping


def reject_explicit_nulls(data: object, not_null_fields: Collection[str]) -> object:
    """Raise if ``data`` carries an explicit ``None`` for a NOT NULL field.

    Written as a ``mode="before"`` model validator rather than one
    ``field_validator`` per field because the check is about the column, not the
    value: the field's own type is irrelevant, and a per-field version would need
    one narrowing wrapper per annotation. ``data`` is whatever was handed to the
    model — a mapping for a JSON request body, anything at all for a direct
    ``model_validate`` — so a non-mapping is passed through untouched for
    pydantic to reject in its own words.
    """
    if not isinstance(data, Mapping):
        return data
    nulled = sorted(field for field in not_null_fields if field in data and data[field] is None)
    if nulled:
        fields = ", ".join(nulled)
        msg = f"Field(s) cannot be null: {fields}"
        raise ValueError(msg)
    return data
