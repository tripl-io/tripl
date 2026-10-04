"""``tripl watch`` — follow mode for an operator sitting in front of an incident.

POLLING, NOT SSE, and that is a measured decision rather than a shortcut. The
headline acceptance criterion of this feature is live replay chunk progress, and
``_heartbeat_replay_progress`` (backend worker/tasks/metrics/tasks.py) writes
that progress into ``ScanJob.result_summary`` and commits with NO
``publish_project_event`` call — so it is invisible on the realtime bus. The bus
additionally no-ops entirely when Redis is absent while still reporting a healthy
socket, and its endpoint is ``include_in_schema=False``, which puts it beyond the
reach of ``tests/test_contract.py``. Revisit when one ``publish_project_event``
call is added to that heartbeat; until then a hybrid would be more transport for
strictly less information.

A sibling of ``diagnostics``, not a member of it: ``diagnostics`` is the verdict
machinery (``checks``, ``scan_checks``) and watch reaches no verdict. The two
things watch does share with doctor — the ``Fetched`` read regime and the
package-root ``model``/``render``/``report`` — it imports; nothing in
``diagnostics`` imports watch at runtime.
"""
