from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tripl.core.adapters.base import (
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
)
from tripl.core.analyzers.cardinality import CardinalityResult, _is_json_type
from tripl.models.event_type import EventType
from tripl.models.schema_drift import SchemaDrift
from tripl.observability.metrics import schema_drifts_detected_total
from tripl.worker.tasks._errors import _CURATED_ERRORS
from tripl.worker.tasks.metrics.property_contracts import property_contract_expectations_for

logger = logging.getLogger(__name__)

# Logical FieldDefinition.field_type values that `_ensure_event_type_with_fields`
# can create automatically. type_changed drift only fires when the previously
# auto-created type disagrees with what we'd auto-create now — user-curated
# field_types ("enum", "number", "boolean", "url") are left alone, since
# choosing them is an intentional schema decision, not drift.
_AUTO_FIELD_TYPES = {"string", "json"}
_SAMPLE_VALUE_MAX_LEN = 255
# `schema_drifts.observed_type` is String(128), but a warehouse type name has no
# length contract: a labelled ClickHouse `Enum8('checkout_started' = 1, ...)` or a
# nested `Map(String, Tuple(...))` renders well past 128 characters. Postgres
# rejects the over-long value and the DataError unwinds the whole catalog sync, so
# the value is bounded on its way into the row rather than at each producer. Keep
# in step with the model column — test_batch3_d1 asserts the two still agree.
_OBSERVED_TYPE_MAX_LEN = 128
_CONTRACT_DECLARED_TYPES = {
    "required_null_violation": "required",
    "enum_violation": "enum",
    "regex_violation": "regex",
    "range_violation": "range",
}


def _infer_logical_field_type(col: ColumnInfo) -> str:
    return "json" if _is_json_type(col.type_name) else "string"


def _pick_sample_value(result: CardinalityResult | None) -> str | None:
    """First non-empty observed value for a column, truncated for storage."""
    if result is None:
        return None
    for raw in result.sample_values or []:
        if raw is None:
            continue
        text = str(raw)
        if not text:
            continue
        if len(text) > _SAMPLE_VALUE_MAX_LEN:
            return text[: _SAMPLE_VALUE_MAX_LEN - 1] + "…"
        return text
    return None


def _truncate_sample_value(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > _SAMPLE_VALUE_MAX_LEN:
        return text[: _SAMPLE_VALUE_MAX_LEN - 1] + "…"
    return text


def _truncate_observed_type(value: object | None) -> str | None:
    """Fit a warehouse type name into `schema_drifts.observed_type`.

    Truncate from the tail: the head carries the outer constructor (`Nullable(`,
    `Map(`, `Tuple(`, `Enum8(`), which is exactly what
    `schema_drift_service._logical_type_from_observed` substring-matches when a
    user accepts the drift into a FieldDefinition.
    """
    if value is None:
        return None
    text = str(value)
    if len(text) > _OBSERVED_TYPE_MAX_LEN:
        return text[: _OBSERVED_TYPE_MAX_LEN - 1] + "…"
    return text


def _carries_no_data(name: str, results: dict[str, CardinalityResult]) -> bool:
    """Whether this column held nothing at all for the event type being diffed.

    A grouped scan reads ONE flat table and hands every event type the same
    global column list, while the cardinality results it also passes are already
    scoped to that type's rows. Without this check each type reports `new_field`
    for every column it never populates — so a purchase-only ``amount`` becomes
    drift on Screen View and on Click, and the demo alone produced ~24 such rows
    per scan. That is noise, not drift: the column is not new,
    this event simply does not use it.

    ``count`` is a distinct count built with NULLs excluded
    (``cardinality._build_analysis``), so 0 means "no value in any row of this
    group". Absent from ``results`` is treated as "we have no evidence" and left
    alone, which keeps the ungrouped path — where callers may pass no results at
    all — reporting exactly as before.
    """
    result = results.get(name)
    return result is not None and result.count == 0


def _diff_event_type_schema(
    event_type: EventType,
    columns: list[ColumnInfo],
    skip_columns: set[str],
    cardinality_results: dict[str, CardinalityResult] | None = None,
) -> list[dict[str, object]]:
    """Return drift items comparing observed columns vs declared FieldDefinitions.

    drift_type ∈ {new_field, missing_field, type_changed}. Skip columns
    (event_type_column, time_column) are excluded from comparison.

    When ``cardinality_results`` is supplied, new_field / type_changed
    entries get a ``sample_value`` for the catalog UI; missing_field stays
    null (we have no observed data for a vanished column).
    """
    observed = {col.name: col for col in columns if col.name not in skip_columns}
    declared = {fd.name: fd for fd in event_type.field_definitions}
    results = cardinality_results or {}

    drift_items: list[dict[str, object]] = []
    for name, col in observed.items():
        if name in declared:
            continue
        if _carries_no_data(name, results):
            continue
        drift_items.append(
            {
                "field_name": name,
                "drift_type": "new_field",
                "observed_type": col.type_name,
                "declared_type": None,
                "sample_value": _pick_sample_value(results.get(name)),
            }
        )

    for name, fd in declared.items():
        if name in observed:
            continue
        # A RESERVED column is not missing — it is simply not catalog-managed.
        # ``observed`` has skip_columns filtered out above, so without this a
        # declared field whose column is reserved reads as "the warehouse stopped
        # sending it". Latent until tripl-jfm3.57 put event-group-rule columns in
        # the reserved set: event_type/time columns are essentially never also
        # declared as fields, but a grouping column very often is — production
        # groups on ``action`` and declares ``action`` on the same event type,
        # which fired a false "missing_field action" alert straight after deploy.
        if name in skip_columns:
            continue
        drift_items.append(
            {
                "field_name": name,
                "drift_type": "missing_field",
                "observed_type": None,
                "declared_type": fd.field_type,
                "sample_value": None,
            }
        )

    for name, col in observed.items():
        definition = declared.get(name)
        if definition is None or definition.field_type not in _AUTO_FIELD_TYPES:
            continue
        inferred = _infer_logical_field_type(col)
        if inferred != definition.field_type:
            drift_items.append(
                {
                    "field_name": name,
                    "drift_type": "type_changed",
                    "observed_type": col.type_name,
                    "declared_type": definition.field_type,
                    "sample_value": _pick_sample_value(results.get(name)),
                }
            )

    return drift_items


def _field_contract_expectations(
    event_type: EventType,
    columns: list[ColumnInfo],
    skip_columns: set[str],
) -> list[FieldContractExpectation]:
    observed = {col.name for col in columns if col.name not in skip_columns}
    expectations: list[FieldContractExpectation] = []
    for field in event_type.field_definitions:
        if field.name not in observed:
            continue

        bad_rate = float(field.contract_max_bad_rate or 0.0)
        if field.is_required:
            threshold = (
                float(field.contract_required_max_null_rate)
                if field.contract_required_max_null_rate is not None
                else bad_rate
            )
            expectations.append(
                FieldContractExpectation(
                    field_name=field.name,
                    drift_type="required_null_violation",
                    threshold=threshold,
                )
            )

        enum_options = tuple(str(option) for option in field.enum_options or [])
        if field.field_type == "enum" and enum_options:
            expectations.append(
                FieldContractExpectation(
                    field_name=field.name,
                    drift_type="enum_violation",
                    threshold=bad_rate,
                    enum_options=enum_options,
                )
            )

        if field.contract_regex:
            expectations.append(
                FieldContractExpectation(
                    field_name=field.name,
                    drift_type="regex_violation",
                    threshold=bad_rate,
                    regex=field.contract_regex,
                )
            )

        if field.contract_min_value is not None or field.contract_max_value is not None:
            expectations.append(
                FieldContractExpectation(
                    field_name=field.name,
                    drift_type="range_violation",
                    threshold=bad_rate,
                    min_value=field.contract_min_value,
                    max_value=field.contract_max_value,
                )
            )

    return expectations


def _property_contract_expectations(
    session: Session,
    event_type: EventType,
    columns: list[ColumnInfo],
    skip_columns: set[str],
) -> list[FieldContractExpectation]:
    """The contracts the type's typed properties imply (F23, #306).

    Keyed on the property's path (``<json_column>.<path>``) rather than a
    column, and only for paths of the JSON columns this run observed; see
    ``core.property_contracts``. A run that read no JSON column has no
    property to check and touches no table for them.
    """
    json_columns = {
        col.name for col in columns if col.name not in skip_columns and _is_json_type(col.type_name)
    }
    if not json_columns:
        return []
    return property_contract_expectations_for(
        session, event_type_id=event_type.id, json_columns=json_columns
    )


def _contract_observed_type(violation: FieldContractViolation) -> str:
    return (
        f"bad_rate={violation.bad_rate:.2%}; "
        f"max={violation.threshold:.2%}; "
        f"bad={violation.bad_count}; total={violation.total_count}"
    )


def _contract_violation_drift_items(
    violations: list[FieldContractViolation],
) -> list[dict[str, object]]:
    return [
        {
            "field_name": violation.field_name,
            "drift_type": violation.drift_type,
            "observed_type": _contract_observed_type(violation),
            "declared_type": _CONTRACT_DECLARED_TYPES.get(violation.drift_type, "contract"),
            "sample_value": _truncate_sample_value(violation.sample_value),
        }
        for violation in violations
    ]


def _record_drift_metrics(rows: list[dict[str, object]]) -> None:
    """Bump the schema_drifts_detected counter once per upserted row.

    Upserts fire on every scan, so this over-counts when a drift persists
    across runs — that's intentional for a counter (rate = drift activity).
    """
    for row in rows:
        drift_type = str(row.get("drift_type") or "unknown")
        schema_drifts_detected_total.labels(drift_type=drift_type).inc()


def _upsert_schema_drifts(
    session: Session,
    *,
    event_type_id: uuid.UUID,
    scan_config_id: uuid.UUID,
    drift_items: list[dict[str, object]],
) -> None:
    if not drift_items:
        return

    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "event_type_id": event_type_id,
            "scan_config_id": scan_config_id,
            "field_name": item["field_name"],
            "drift_type": item["drift_type"],
            "observed_type": _truncate_observed_type(item["observed_type"]),
            "declared_type": item["declared_type"],
            "sample_value": item.get("sample_value"),
            "detected_at": now,
        }
        for item in drift_items
    ]

    if session.bind is not None and session.bind.dialect.name == "sqlite":
        sqlite_stmt = sqlite_insert(SchemaDrift).values(rows)
        sqlite_stmt = sqlite_stmt.on_conflict_do_update(
            index_elements=["event_type_id", "field_name", "drift_type"],
            # coalesce(new, old): a re-upsert must never blank the provenance that
            # signals.py (alerting), detection_reset_service.py (reset) and
            # demo_runtime.py (pruning) all filter on — a NULL scan_config_id makes
            # the drift invisible to every one of them. Direction matters: a real id
            # still wins over a row whose FK was nulled by ondelete="SET NULL".
            set_={
                "scan_config_id": func.coalesce(
                    sqlite_stmt.excluded.scan_config_id, SchemaDrift.scan_config_id
                ),
                "observed_type": sqlite_stmt.excluded.observed_type,
                "declared_type": sqlite_stmt.excluded.declared_type,
                "sample_value": sqlite_stmt.excluded.sample_value,
                "detected_at": sqlite_stmt.excluded.detected_at,
            },
        )
        session.execute(sqlite_stmt)
        _record_drift_metrics(rows)
        return

    pg_stmt = pg_insert(SchemaDrift).values(rows)
    pg_stmt = pg_stmt.on_conflict_do_update(
        constraint="uq_schema_drift_event_type_field_kind",
        set_={
            "scan_config_id": func.coalesce(
                pg_stmt.excluded.scan_config_id, SchemaDrift.scan_config_id
            ),
            "observed_type": pg_stmt.excluded.observed_type,
            "declared_type": pg_stmt.excluded.declared_type,
            "sample_value": pg_stmt.excluded.sample_value,
            "detected_at": pg_stmt.excluded.detected_at,
        },
    )
    session.execute(pg_stmt)
    _record_drift_metrics(rows)


def _detect_event_type_drift(
    session: Session,
    *,
    existing_event_type: EventType | None,
    columns: list[ColumnInfo],
    skip_columns: set[str],
    scan_config_id: uuid.UUID,
    cardinality_results: dict[str, CardinalityResult] | None = None,
) -> None:
    """Diff existing event_type schema against observed columns, write drifts."""
    if existing_event_type is None:
        return
    drift_items = _diff_event_type_schema(
        existing_event_type,
        columns,
        skip_columns,
        cardinality_results=cardinality_results,
    )
    _upsert_schema_drifts(
        session,
        event_type_id=existing_event_type.id,
        scan_config_id=scan_config_id,
        drift_items=drift_items,
    )


@dataclass(frozen=True)
class FieldContractOutcome:
    """What one event type's contract check produced — including whether it ran.

    Three numbers rather than one because "checked, found nothing" and "could not
    check" are different answers that both used to report 0 violations, and the
    second one is the one an operator has to be told about: a contract that
    silently stops being evaluated looks exactly like a contract that is being
    met.

    "Could not check" comes in two units, and they are kept apart rather than
    summed. ``checks_failed`` is 0 or 1: the whole check for this event type
    raised and nothing was evaluated. ``expectations_skipped`` counts single
    expectations the adapter declined while it evaluated the rest — a pattern
    the engine's regex library refuses, a REPEATED column BigQuery cannot render,
    a non-finite range bound — which used to leave nothing but a worker log line.
    Folding them into ``checks_failed`` would
    make one refused pattern read as a whole event type going unchecked.
    """

    violations_detected: int = 0
    checks_failed: int = 0
    expectations_skipped: int = 0


def _take_skipped_field_contracts(adapter: BaseAdapter) -> int:
    """How many expectations ``adapter`` declined since the last call.

    Read through ``getattr`` because the check below is also driven by
    duck-typed adapters that only implement ``validate_field_contracts``; one
    that cannot report a skip has, as far as this caller can know, skipped none.
    """
    take = getattr(adapter, "take_skipped_field_contracts", None)
    if take is None:
        return 0
    return len(take())


def _detect_field_contract_violations(
    session: Session,
    *,
    adapter: BaseAdapter,
    event_type: EventType | None,
    base_query: str,
    columns: list[ColumnInfo],
    skip_columns: set[str],
    scan_config_id: uuid.UUID,
    time_column: str | None,
    time_from: datetime,
    time_to: datetime,
    group_column: str | None = None,
    group_value: str | None = None,
    limit: int = 50000,
) -> FieldContractOutcome:
    """Validate declared field contracts against warehouse data, write drifts.

    A failure here costs this run its contract check and nothing more. That is a
    deliberate swallow, and it is the narrowest one that fixes what it has to:
    contract evaluation is ONE part of a catalog sync, this function is called
    once per event-type group inside ``catalog_sync``'s loop, and neither it nor
    the loop caught anything — so a single expectation the warehouse would not
    accept ended the whole collection, the config stayed due, and it failed again
    on every retry with ``"Scan failed due to an internal error."`` The
    expectations are replayed from contracts a user declared long ago and a
    column can change type or a pattern can stop compiling under them, so a
    permanently-wedged config was reachable without anyone touching the config.

    An ``except Exception`` is the thing to distrust in a change like this, so it
    carries all three of the guarantees that make it legible rather than
    convenient:

    * curated errors are re-raised. ``ScanError`` / ``NameFormatError`` /
      ``WarehouseCapabilityError`` carry an author-written sentence naming the
      setting to change, and ``user_facing_error`` surfaces it verbatim. Those
      the operator can act on; hiding one behind a counter would trade a
      diagnosable failure for an undiagnosable success.
    * the traceback is logged (``logger.exception``, not a message), because the
      class of thing being swallowed includes genuine adapter bugs and the log is
      then the only record of one.
    * the failure is COUNTED and reaches the job summary as
      ``contract_checks_failed`` (see ``CatalogSyncResult`` and the summary in
      ``tasks``). A swallow that reports nothing is the anti-pattern; this one
      reports every time it fires.

    ``contract_checks_failed`` counts only that whole-check failure. A single
    expectation the adapter declined while the rest ran is not an exception and
    never reaches the ``except``; it is read back from the adapter and reported
    separately as ``contract_expectations_skipped`` (see ``FieldContractOutcome``).

    The ``try`` wraps the warehouse call ONLY. A failure writing the drift rows
    belongs to the session, and the task's rollback has to see it: swallowing
    that would report violations the database does not hold.

    What a failed check leaves behind is what a passing one would have: nothing
    on this path ever resolves a drift row — ``_upsert_schema_drifts`` only
    inserts and refreshes — so an open contract drift stays open either way. That
    is the right reading: "we could not check" must not become "it is clean now".
    """
    if event_type is None:
        return FieldContractOutcome()

    expectations = [
        *_field_contract_expectations(event_type, columns, skip_columns),
        *_property_contract_expectations(session, event_type, columns, skip_columns),
    ]
    if not expectations:
        return FieldContractOutcome()

    # Drained before the call as well as after it, so a skip left behind by an
    # earlier caller of this adapter is not reported against this event type.
    _take_skipped_field_contracts(adapter)
    try:
        violations = adapter.validate_field_contracts(
            base_query,
            expectations,
            time_column=time_column,
            time_from=time_from,
            time_to=time_to,
            group_column=group_column,
            group_value=group_value,
            limit=limit,
        )
    except _CURATED_ERRORS:
        raise
    except Exception:
        logger.exception(
            "Field contract evaluation failed for event type %s (%s expectations); "
            "skipping contracts for it this run",
            event_type.id,
            len(expectations),
        )
        # A skip recorded before the raise belongs to a check that did not run;
        # ``checks_failed`` already says so, and counting it twice would not.
        _take_skipped_field_contracts(adapter)
        return FieldContractOutcome(checks_failed=1)

    skipped = _take_skipped_field_contracts(adapter)
    drift_items = _contract_violation_drift_items(violations)
    _upsert_schema_drifts(
        session,
        event_type_id=event_type.id,
        scan_config_id=scan_config_id,
        drift_items=drift_items,
    )
    return FieldContractOutcome(violations_detected=len(drift_items), expectations_skipped=skipped)
