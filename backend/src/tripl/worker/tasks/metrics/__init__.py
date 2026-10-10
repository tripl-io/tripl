"""Celery tasks for collecting time-bucketed event metrics from the warehouse.

Scheduled collection refreshes catalog events with the same cardinality analysis
pipeline as the manual scan, then collects time-bucketed counts. Explicit
metrics replay reuses the existing catalog so replay chunking bounds every
warehouse query it issues.

Importing the package registers the collection tasks: ``schedule`` pulls in
``metric_collect`` and ``tasks``. ``freshness_sweep`` is registered by
``celery_app`` itself. Everything else is imported from its own submodule.
"""

from __future__ import annotations

from tripl.worker.tasks.metrics.schedule import check_metrics_due
from tripl.worker.tasks.metrics.tasks import collect_metrics

__all__ = ["check_metrics_due", "collect_metrics"]
