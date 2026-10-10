"""The check rules: pure, synchronous, total functions of a ``Snapshot``.

Zero HTTP, zero ``await``, and no clock read that is not ``snapshot.now``. That
is what makes the interesting half of doctor testable as arithmetic, and it is
why a check may read data another check "owns" — everything was fetched before
any of this ran.

Universal rule (TRAP 3): a read that did not return 200 is NEVER treated as an
empty list. Every such case becomes ``endpoint_unexpected_status`` naming the
path and the code, because "the drift endpoint 404'd" silently read as "no
drifts" is what invalidated the 2026-07-30 audit.
"""

from __future__ import annotations

from datetime import datetime

from tripl_cli.api import event_types as event_types_api
from tripl_cli.api import projects as projects_api
from tripl_cli.diagnostics.scan_checks import (
    CHECK_ID as SCANS_CHECK_ID,
)
from tripl_cli.diagnostics.scan_checks import (
    CHECK_TITLE as SCANS_CHECK_TITLE,
)
from tripl_cli.diagnostics.scan_checks import (
    FailureStreak,
    check_scans,
    streaks_by_config,
)
from tripl_cli.model import (
    HEALTH_PATH,
    SCOPE_PROJECT,
    Check,
    Fetched,
    Finding,
    JsonDict,
    Severity,
    Snapshot,
    Target,
    format_duration,
    is_healthy,
    parse_time,
    text_of,
    to_rfc3339,
    worst,
)

CONNECTIVITY_FAILED_SKIP = "the instance is not reachable, so nothing else could be checked"
NO_PROJECT_SKIP = "no project could be selected, so nothing project-scoped could be checked"
PROJECT_SCOPE_SKIP = (
    "GET /data-sources is instance-level and returns 403 for a project-scoped key by design; "
    "re-run with an instance-scoped key to see data-source probe results"
)

# How many drifts to name in the aggregate finding. Three is enough to recognise
# the pattern and short enough that the line stays readable in a ticket.
DRIFT_EXAMPLES = 3


def _skipped(check_id: str, title: str, reason: str) -> Check:
    return Check(id=check_id, title=title, status=Severity.SKIP, summary=reason, skip_reason=reason)


def check_connectivity(snapshot: Snapshot) -> Check:
    health = snapshot.health
    url = f"{snapshot.base_url}{HEALTH_PATH}"
    source = snapshot.base_url_source
    evidence: dict[str, object] = {
        "url": url,
        "status_code": health.status_code,
        "body_status": (health.value or {}).get("status") if health.value else None,
    }

    def failed(code: str, message: str) -> Check:
        return Check(
            id="connectivity",
            title="Instance reachability",
            status=Severity.FAIL,
            summary=message,
            findings=(
                Finding(code=code, severity=Severity.FAIL, message=message, evidence=evidence),
            ),
        )

    if health.status_code is None:
        return failed(
            "instance_unreachable",
            f"Could not reach {url} (from {source}): {health.error}. "
            "Nothing else could be checked.",
        )
    if health.status_code == 503:
        return failed(
            "instance_database_unreachable",
            f"{snapshot.base_url} is running but reports its database is unreachable "
            "(/health -> component=database). The API will fail on almost every request "
            "until the database is back.",
        )
    if is_healthy(health):
        return Check(
            id="connectivity",
            title="Instance reachability",
            status=Severity.PASS,
            summary=(
                f"Reached {snapshot.base_url} (from {source}); the API and its database are up."
            ),
        )
    return failed(
        "instance_not_tripl",
        f"Reached {url} but /health did not answer like a tripl instance ({health.error}). "
        "The URL probably points at a proxy or the wrong host.",
    )


def check_auth(snapshot: Snapshot) -> Check:
    auth = snapshot.auth
    if auth is None:
        return _skipped("auth", "API key", CONNECTIVITY_FAILED_SKIP)

    def failed(code: str, message: str) -> Check:
        return Check(
            id="auth",
            title="API key",
            status=Severity.FAIL,
            summary=message,
            findings=(
                Finding(
                    code=code,
                    severity=Severity.FAIL,
                    message=message,
                    evidence={"status_code": auth.status_code, "detail": auth.error},
                ),
            ),
        )

    if auth.ok:
        # The role, and nothing else. Never the email and never a key prefix:
        # doctor output gets pasted into tickets. `role` earns its place because
        # it is what explains every subsequent 403.
        role = text_of(auth.value or {}, "role") or "unknown"
        return Check(
            id="auth",
            title="API key",
            status=Severity.PASS,
            summary=f"The API key authenticates as an instance-wide key (role: {role}).",
        )
    if auth.status_code == 403:
        # Not a problem, so no finding: `deps.py::_enforce_project_scope` refuses
        # every slug-less route for a project-bound key BY DESIGN (TRAP 5).
        return Check(
            id="auth",
            title="API key",
            status=Severity.PASS,
            summary=(
                "The API key authenticates and is scoped to a single project, so instance-wide "
                "endpoints are refused by design."
            ),
        )
    if auth.status_code == 401:
        return failed(
            "api_key_invalid",
            f"The API key is not valid for {snapshot.base_url} (401). The key came from "
            f"{snapshot.api_key_source}.",
        )
    return failed(
        "auth_unexpected_status",
        f"GET /auth/me answered HTTP {auth.status_code}; the key could not be verified.",
    )


def _project_target(project: JsonDict) -> Target:
    return Target(kind="project", id=text_of(project, "id"), name=text_of(project, "slug"))


def check_projects(snapshot: Snapshot) -> Check:
    selection = snapshot.selection
    if selection is None:
        return _skipped("projects", "Projects", CONNECTIVITY_FAILED_SKIP)
    findings: list[Finding] = []

    if selection.requested_slugs:
        for slug in selection.requested_slugs:
            fetched = selection.by_slug.get(slug)
            if fetched is None or fetched.ok:
                continue
            if fetched.status_code == 404:
                findings.append(
                    Finding(
                        code="project_not_found",
                        severity=Severity.FAIL,
                        project=slug,
                        target=Target(kind="project", name=slug),
                        message=f"No project with slug {slug!r} on this instance.",
                        evidence={"slug": slug, "status_code": 404},
                    )
                )
            elif fetched.status_code == 403:
                findings.append(
                    Finding(
                        code="project_not_authorized",
                        severity=Severity.FAIL,
                        project=slug,
                        target=Target(kind="project", name=slug),
                        message=f"The API key is scoped to a different project than {slug!r}.",
                        evidence={"slug": slug, "status_code": 403},
                    )
                )
            else:
                findings.append(
                    _endpoint_finding(projects_api.DETAIL.format(slug=slug), fetched, slug)
                )
    elif snapshot.api_key_scope == SCOPE_PROJECT:
        findings.append(
            Finding(
                code="project_selector_required",
                severity=Severity.FAIL,
                message=(
                    "This API key is scoped to a single project, so listing projects returns 403 "
                    "by design and doctor cannot discover the slug. Re-run with --project <slug>."
                ),
                evidence={"api_key_scope": SCOPE_PROJECT},
            )
        )
    elif selection.listing is not None and not selection.listing.ok:
        if selection.listing.status_code == 403:
            findings.append(
                Finding(
                    code="project_list_forbidden",
                    severity=Severity.FAIL,
                    message=(
                        "Listing projects was refused (403) for a key that is not project-scoped "
                        "- the backing user's role is probably insufficient. Name the project "
                        "explicitly with --project <slug>."
                    ),
                    evidence={"status_code": 403, "error": selection.listing.error},
                )
            )
        else:
            findings.append(_endpoint_finding(projects_api.LIST, selection.listing, None))
    elif not selection.projects:
        findings.append(
            Finding(
                code="no_projects_selected",
                severity=Severity.WARN,
                message=(
                    "No non-demo projects on this instance. Re-run with --include-demo to check "
                    "the demo, or --project <slug>."
                ),
                evidence={"excluded_demo_count": selection.excluded_demo_count},
            )
        )

    for project in selection.projects:
        slug = text_of(project, "slug") or ""
        generation = text_of(project, "generation_status")
        if generation == "failed":
            findings.append(
                Finding(
                    code="project_generation_failed",
                    severity=Severity.FAIL,
                    project=slug,
                    target=_project_target(project),
                    message=(
                        f"Project {slug!r} failed to finish provisioning: "
                        f"{text_of(project, 'generation_error') or 'no error was recorded'}."
                    ),
                    evidence={
                        "generation_status": generation,
                        "generation_stage": text_of(project, "generation_stage"),
                        "generation_error": text_of(project, "generation_error"),
                    },
                )
            )
        elif generation in ("pending", "seeding"):
            findings.append(
                Finding(
                    code="project_generating",
                    severity=Severity.WARN,
                    project=slug,
                    target=_project_target(project),
                    message=(
                        f"Project {slug!r} is still provisioning (stage: "
                        f"{text_of(project, 'generation_stage') or 'unknown'}) and its data is "
                        "incomplete."
                    ),
                    evidence={
                        "generation_status": generation,
                        "generation_stage": text_of(project, "generation_stage"),
                    },
                )
            )

    count = len(selection.projects)
    excluded = selection.excluded_demo_count
    summary = f"{count} project{'' if count == 1 else 's'} selected"
    if excluded:
        summary += f" ({excluded} demo project{'' if excluded == 1 else 's'} excluded; "
        summary += "--include-demo adds them)"
    return Check(
        id="projects",
        title="Projects",
        status=worst(finding.severity for finding in findings),
        summary=summary + ".",
        findings=tuple(findings),
    )


def _endpoint_finding(path: str, fetched: Fetched[object], project: str | None) -> Finding:
    return Finding(
        code="endpoint_unexpected_status",
        severity=Severity.FAIL,
        project=project,
        message=(
            f"GET {path} answered HTTP {fetched.status_code}; the state it reports is unknown, "
            "not empty."
        ),
        evidence={"path": path, "status_code": fetched.status_code, "error": fetched.error},
    )


def check_data_sources(snapshot: Snapshot, streaks: dict[tuple[str, str], FailureStreak]) -> Check:
    title = "Data sources"
    if snapshot.selection is None:
        return _skipped("data_sources", title, CONNECTIVITY_FAILED_SKIP)
    if snapshot.api_key_scope == SCOPE_PROJECT:
        return _skipped("data_sources", title, PROJECT_SCOPE_SKIP)
    if not snapshot.selected_projects:
        return _skipped("data_sources", title, NO_PROJECT_SKIP)
    fetched = snapshot.data_sources
    if fetched is None:
        return _skipped("data_sources", title, "GET /data-sources was not attempted")
    if not fetched.ok or fetched.value is None:
        return _skipped(
            "data_sources",
            title,
            f"GET /data-sources answered HTTP {fetched.status_code}, so the connection probes "
            "of the referenced data sources are unknown",
        )

    # Only the data sources a selected project's scan configs actually reference
    # are judged; an unused source failing its probe is not this run's business.
    referenced: dict[str, list[tuple[str, str]]] = {}
    earliest_failure: dict[str, datetime] = {}
    for project in snapshot.selected_projects:
        slug = text_of(project, "slug") or ""
        scans = snapshot.scans.get(slug)
        if scans is None or scans.value is None:
            continue
        for config in scans.value:
            source_id = text_of(config, "data_source_id")
            if not source_id:
                continue
            config_id = text_of(config, "id") or ""
            referenced.setdefault(source_id, []).append(
                (config_id, text_of(config, "name") or "(unnamed)")
            )
            streak = streaks.get((slug, config_id))
            if streak is not None and streak.length and streak.first_failed_at is not None:
                current = earliest_failure.get(source_id)
                if current is None or streak.first_failed_at < current:
                    earliest_failure[source_id] = streak.first_failed_at

    findings: list[Finding] = []
    judged = 0
    untested = 0
    for source in fetched.value:
        source_id = text_of(source, "id")
        if source_id is None or source_id not in referenced:
            continue
        judged += 1
        names = [name for _, name in referenced[source_id]]
        config_ids = [config_id for config_id, _ in referenced[source_id]]
        source_name = text_of(source, "name") or "(unnamed)"
        probe_status = text_of(source, "last_test_status")
        probed_at = parse_time(source.get("last_test_at"))
        message_text = text_of(source, "last_test_message")
        target = Target(kind="data_source", id=source_id, name=source_name)
        if probe_status is None or probed_at is None:
            untested += 1
            continue
        evidence = {
            "data_source_id": source_id,
            "data_source_name": source_name,
            "last_test_status": probe_status,
            "last_test_at": to_rfc3339(probed_at),
            "last_test_message": message_text,
            # `data_sources._redact_connection` blanks last_test_message to null
            # for every user below owner. Reporting that as "no message" would be
            # a lie about the data source; the flag says which it is.
            "message_redacted": message_text is None,
            "scan_config_ids": config_ids,
        }
        if probe_status == "failed":
            used_by = ", ".join(repr(name) for name in names)
            if message_text:
                tail = f": {message_text!r}"
            else:
                tail = (
                    ". The error text is only returned to an owner-role key, so re-run doctor "
                    "with one to see it"
                )
            findings.append(
                Finding(
                    code="data_source_probe_failed",
                    severity=Severity.FAIL,
                    target=target,
                    message=(
                        f"Data source {source_name!r} (used by scan config {used_by}) last failed "
                        f"its connection test at {to_rfc3339(probed_at)}{tail}."
                    ),
                    evidence=evidence,
                )
            )
            continue
        started = earliest_failure.get(source_id)
        if probe_status == "success" and started is not None and probed_at < started:
            age = (snapshot.now - probed_at).total_seconds()
            findings.append(
                Finding(
                    code="data_source_probe_stale",
                    severity=Severity.WARN,
                    target=target,
                    message=(
                        f"Data source {source_name!r} last tested OK {format_duration(age)} ago, "
                        f"before scan config {names[0]!r} started failing - that probe is stale, "
                        "not evidence of health. It is only written on an explicit connection "
                        "test or a create/update."
                    ),
                    evidence={**evidence, "streak_started_at": to_rfc3339(started)},
                )
            )

    if judged == 0:
        summary = "No selected scan config references a data source."
    elif findings:
        summary = f"{judged} referenced data source(s); see below."
    else:
        summary = (
            f"{judged} data source(s) referenced by the selected scan configs; "
            "none reports a failed probe."
        )
        if untested:
            summary += f" ({untested} has never been connection-tested.)"
    return Check(
        id="data_sources",
        title=title,
        status=worst(finding.severity for finding in findings),
        summary=summary,
        findings=tuple(findings),
    )


def check_drifts(snapshot: Snapshot) -> Check:
    title = "Schema drift"
    if snapshot.selection is None:
        return _skipped("drifts", title, CONNECTIVITY_FAILED_SKIP)
    if not snapshot.selected_projects:
        return _skipped("drifts", title, NO_PROJECT_SKIP)

    findings: list[Finding] = []
    for project in snapshot.selected_projects:
        slug = text_of(project, "slug") or ""
        types = snapshot.event_types.get(slug)
        if types is None:
            continue
        if not types.ok or types.value is None:
            findings.append(_endpoint_finding(event_types_api.LIST.format(slug=slug), types, slug))
            continue
        untriaged: list[str] = []
        oldest: datetime | None = None
        for event_type in types.value:
            type_id = text_of(event_type, "id")
            if type_id is None:
                continue
            fetched = snapshot.drifts.get((slug, type_id))
            if fetched is None:
                continue  # outside the --max-event-types budget; reported below
            if not fetched.ok or fetched.value is None:
                findings.append(
                    _endpoint_finding(
                        event_types_api.DRIFTS.format(slug=slug, event_type_id=type_id),
                        fetched,
                        slug,
                    )
                )
                continue
            type_name = text_of(event_type, "name") or type_id
            for drift in event_types_api.drift_items(fetched.value):
                field_name = text_of(drift, "field_name") or "(unnamed)"
                drift_type = text_of(drift, "drift_type") or "unknown"
                if text_of(drift, "status") == "accepted" and drift_type == "missing_field":
                    # `schema_drift_service` calls session.delete(field) when a
                    # missing_field drift is accepted. That deletion is invisible
                    # in every other surface and it is the 2026-07-30 cause.
                    resolved_at = parse_time(drift.get("resolved_at"))
                    resolved_by = text_of(drift, "resolved_by")
                    # resolved_by is a USER ID, not an email — resolving it to a
                    # person would need owner-gated GET /users plus another call.
                    actor = f" (by user {resolved_by})" if resolved_by else ""
                    when = to_rfc3339(resolved_at) if resolved_at else "an unrecorded time"
                    findings.append(
                        Finding(
                            code="schema_field_deleted_by_accept",
                            severity=Severity.WARN,
                            project=slug,
                            target=Target(kind="event_type", id=type_id, name=type_name),
                            message=(
                                f"Field {field_name!r} was deleted from event type "
                                f"{type_name!r} on {when} when a missing_field drift was "
                                f"accepted{actor}."
                            ),
                            evidence={
                                "field_name": field_name,
                                "event_type_id": type_id,
                                "drift_id": text_of(drift, "id"),
                                "resolved_at": to_rfc3339(resolved_at) if resolved_at else None,
                                "resolved_by": resolved_by,
                            },
                        )
                    )
                elif event_types_api.is_untriaged(drift, snapshot.now):
                    untriaged.append(f"{type_name}.{field_name} ({drift_type})")
                    detected = parse_time(drift.get("detected_at"))
                    if detected is not None and (oldest is None or detected < oldest):
                        oldest = detected
        if untriaged:
            examples = ", ".join(untriaged[:DRIFT_EXAMPLES])
            more = "" if len(untriaged) <= DRIFT_EXAMPLES else ", ..."
            since = f" (oldest detected {to_rfc3339(oldest)})" if oldest else ""
            findings.append(
                Finding(
                    code="schema_drift_open",
                    severity=Severity.WARN,
                    project=slug,
                    message=(
                        f"Project {slug!r} has {len(untriaged)} untriaged schema drifts{since}: "
                        f"{examples}{more}"
                    ),
                    evidence={
                        "untriaged_count": len(untriaged),
                        "oldest_detected_at": to_rfc3339(oldest) if oldest else None,
                        "examples": untriaged[:DRIFT_EXAMPLES],
                    },
                )
            )
        # One finding PER PROJECT, next to that project's other drift findings.
        # The budget is spread round-robin, so a run can leave one project fully
        # examined and another at a third of it; a single instance-wide ratio
        # reads as even coverage and names nobody, which is the one thing an
        # operator needs here — "nothing found in X" and "X was barely looked at"
        # must not print the same.
        coverage = snapshot.drift_coverage.get(slug)
        if coverage is not None and coverage.truncated:
            findings.append(
                Finding(
                    code="drift_scan_truncated",
                    severity=Severity.WARN,
                    project=slug,
                    message=(
                        f"Only {coverage.examined} of {coverage.total} event types in project "
                        f"{slug!r} were checked for schema drift (--max-event-types). The "
                        f"remaining {coverage.total - coverage.examined} were not examined."
                    ),
                    evidence={
                        "project": slug,
                        "examined": coverage.examined,
                        "total": coverage.total,
                    },
                )
            )

    truncated = any(coverage.truncated for coverage in snapshot.drift_coverage.values())
    status = worst(finding.severity for finding in findings)
    if truncated:
        # The instance-wide ratio still earns its place as a one-line headline;
        # which project it is short in is the finding's job, not the summary's.
        summary = (
            f"{snapshot.event_types_examined} of {snapshot.event_types_seen} event types were "
            "examined for schema drift; the partly examined projects are named below."
        )
    elif findings:
        summary = f"{snapshot.event_types_examined} event type(s) examined; see below."
    else:
        summary = (
            f"{snapshot.event_types_examined} event type(s) examined; no untriaged schema drift."
        )
    return Check(id="drifts", title=title, status=status, summary=summary, findings=tuple(findings))


def run_checks(snapshot: Snapshot) -> list[Check]:
    """Every check, in the documented order, exactly once each."""
    connectivity = check_connectivity(snapshot)
    if connectivity.status is Severity.FAIL:
        # Short-circuit: with the instance unreachable nothing downstream could
        # have been read, and a skip that says so is the honest answer. It never
        # counts as a pass, so the run still exits non-zero.
        return [connectivity] + [
            _skipped(check_id, title, CONNECTIVITY_FAILED_SKIP)
            for check_id, title in (
                ("auth", "API key"),
                ("projects", "Projects"),
                ("data_sources", "Data sources"),
                # Imported, not re-spelled: the skipped and the executed form of a
                # check must carry the same title, or renaming one silently gives
                # a healthy instance and a skipped one two different rows.
                (SCANS_CHECK_ID, SCANS_CHECK_TITLE),
                ("drifts", "Schema drift"),
            )
        ]
    streaks = streaks_by_config(snapshot)
    scans = (
        check_scans(snapshot, streaks)
        if snapshot.selected_projects
        else _skipped(SCANS_CHECK_ID, SCANS_CHECK_TITLE, NO_PROJECT_SKIP)
    )
    return [
        connectivity,
        check_auth(snapshot),
        check_projects(snapshot),
        check_data_sources(snapshot, streaks),
        scans,
        check_drifts(snapshot),
    ]
