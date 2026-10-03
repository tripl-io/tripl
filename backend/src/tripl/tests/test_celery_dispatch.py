"""Guards on how the API hands work to the broker.

Two failure modes live here, both of which pass every other gate silently:
a publish that blocks the request event loop for as long as a hung broker
feels like, and a beat entry naming a task nobody registered.
"""

import asyncio
import threading

import pytest

from tripl.services._celery_dispatch import dispatch
from tripl.worker.celery_app import celery_app


def test_the_publish_bounds_are_set() -> None:
    """Without these, one unreachable broker holds a caller for ~19 seconds.

    Celery's defaults are a 4s connect timeout against 1+3 publish attempts.
    Every API-side dispatch waits on that, so the bound is what keeps a broker
    outage from turning into an API outage. Pinned here because nothing else
    would notice the day someone drops the two lines.
    """
    assert celery_app.conf.broker_connection_timeout == 2.0

    policy = celery_app.conf.task_publish_retry_policy
    assert policy["max_retries"] == 2
    assert policy["interval_max"] <= 0.5


def test_control_and_event_queues_are_exclusive() -> None:
    """RabbitMQ 4 refuses the transient non-exclusive queues Celery declares by
    default for remote control (pidbox, replies) and events (gossip), and the
    worker dies at startup on them. Exclusive, the broker accepts them."""
    assert celery_app.conf.control_queue_exclusive is True
    assert celery_app.conf.event_queue_exclusive is True
    assert celery_app.control.mailbox.reply_queue.exclusive is True
    assert celery_app.control.mailbox.get_queue("worker@host").exclusive is True


@pytest.mark.asyncio
async def test_dispatch_runs_the_publish_off_the_event_loop() -> None:
    """The point of the helper: the blocking kombu call leaves the loop thread."""
    loop_thread = threading.get_ident()
    ran_on: list[int] = []

    def publish(*_args: object, **_kwargs: object) -> str:
        ran_on.append(threading.get_ident())
        return "queued"

    assert await dispatch(publish, "arg", kw=1) == "queued"
    assert len(ran_on) == 1
    assert ran_on[0] != loop_thread


@pytest.mark.asyncio
async def test_dispatch_propagates_the_broker_failure_to_the_caller() -> None:
    """Callers wrap these publishes in try/except; the helper must not swallow."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broker gone")

    with pytest.raises(RuntimeError, match="broker gone"):
        await dispatch(boom)


@pytest.mark.asyncio
async def test_dispatch_does_not_stall_other_loop_work() -> None:
    """A slow publish must not stop the rest of the loop from making progress."""
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        for _ in range(5):
            ticks += 1
            await asyncio.sleep(0)

    def slow(*_args: object, **_kwargs: object) -> None:
        threading.Event().wait(0.05)

    await asyncio.gather(dispatch(slow), tick())
    assert ticks == 5


def test_every_beat_entry_names_a_registered_task() -> None:
    """A renamed task only ever says "unregistered task" in a worker log.

    The beat tick drives metric collection, scans, variable sweeps, the search
    reindex and the demo runtime, so a typo here stops the product quietly while
    every gate stays green. Importing celery_app registers the task modules, so
    the two ends of each name can be tied together right now.
    """
    assert celery_app.conf.beat_schedule, "the beat schedule is empty"

    missing = sorted(
        entry["task"]
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"] not in celery_app.tasks
    )
    assert not missing, f"beat schedules tasks that are not registered: {missing}"
