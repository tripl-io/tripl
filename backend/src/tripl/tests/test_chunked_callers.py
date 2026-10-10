"""``_id_chunks.chunked`` is the one slicer; each statement keeps its own size.

The Telegram delivery split and the bind-budget batches in ``metric_rows`` used
to slice by hand. They now go through ``chunked`` with the size their own
constant documents, and ``chunked`` hands a list back as lists, so nothing that
took a list before has to copy one now.
"""

from __future__ import annotations

from typing import cast

from tripl.alerting_matching import AlertMatchCandidate
from tripl.models.alert_destination import AlertDestinationType
from tripl.services._id_chunks import IN_CHUNK_SIZE, chunked
from tripl.worker.tasks.metrics.dispatch import _delivery_chunks
from tripl.worker.tasks.metrics.metric_rows import _MAX_BIND_PARAMS, _chunk_keys, _chunk_rows


def test_chunked_slices_keep_the_input_type() -> None:
    # The sizes are pinned in ``test_prelaunch_services_api``; this is the type.
    assert all(isinstance(chunk, list) for chunk in chunked(list(range(IN_CHUNK_SIZE + 1))))
    assert list(chunked((1, 2, 3), 2)) == [(1, 2), (3,)]


def test_a_telegram_delivery_carries_at_most_eight_items() -> None:
    anomalies = [cast(AlertMatchCandidate, object()) for _ in range(17)]

    chunks = _delivery_chunks(anomalies, channel=AlertDestinationType.telegram.value)

    assert [len(chunk) for chunk in chunks] == [8, 8, 1]
    assert [item for chunk in chunks for item in chunk] == anomalies
    assert all(isinstance(chunk, list) for chunk in chunks)
    # Slack has no item ceiling, and a digest is never split by count.
    assert _delivery_chunks(anomalies, channel=AlertDestinationType.slack.value) == [anomalies]
    assert _delivery_chunks(
        anomalies, channel=AlertDestinationType.telegram.value, chunk_items=False
    ) == [anomalies]


def test_row_and_key_batches_stay_lists_inside_the_bind_budget() -> None:
    columns = 7
    rows: list[dict[str, object]] = [
        {f"c{index}": row for index in range(columns)} for row in range(20_000)
    ]

    batches = list(_chunk_rows(rows))

    assert all(isinstance(batch, list) for batch in batches)
    assert all(len(batch) * columns <= _MAX_BIND_PARAMS for batch in batches)
    assert [row for batch in batches for row in batch] == rows

    keys = tuple((index, index) for index in range(40_000))
    key_batches = list(_chunk_keys(keys))

    assert all(isinstance(batch, list) for batch in key_batches)
    assert all(len(batch) * 2 + 1 <= _MAX_BIND_PARAMS for batch in key_batches)
    assert [key for batch in key_batches for key in batch] == list(keys)
