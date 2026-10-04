"""The audit action vocabulary, served to the audit filter (``GET /audit/actions``).

The Audit tab used to hand-maintain this list, and it drifted: every action a
router started recording had to be remembered on the frontend too, and those
that were not could be read in the feed but not filtered for. It lives here now,
next to the ``audit_service.record(...)`` calls that write the actions, and
``tests/test_fj5g_batch_a.py`` fails when a recorded action is missing from it.

Two halves, because the Audit tab's query is always narrowed to one project:

* ``PROJECT_GROUPS`` — actions recorded with ``project=`` / ``project_slug=``;
* ``WORKSPACE_GROUPS`` — actions recorded with no project, whose subject belongs
  to the workspace. A project-scoped query can never match them.

The ``*.<verb>`` families are read off the typed literals the routers record
them from, so a new verb joins the filter with the literal.
"""

from typing import get_args

from tripl.schemas.alerting import AlertInboxAction
from tripl.schemas.audit import AuditActionCatalog, AuditActionGroup
from tripl.schemas.plan_branch import BranchTransitionAction
from tripl.schemas.schema_drift import SchemaDriftAction


def _family(prefix: str, literal: object) -> list[str]:
    return [f"{prefix}.{verb}" for verb in get_args(literal)]


PROJECT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Events",
        (
            "event.create",
            "event.bulk_create",
            "event.update",
            "event.bulk_update",
            "event.delete",
            "event.bulk_delete",
            "event.duplicate_dismiss",
            "event_comment.create",
            "event_comment.delete",
            "event_comment.action",
            "event_photo.upload",
            "event_photo.figma_attach",
            "event_photo.reorder",
            "event_photo.delete",
            "event_photo.comment_create",
            "event_photo.comment_delete",
        ),
    ),
    (
        "Schema",
        (
            "event_type.create",
            "event_type.update",
            "event_type.delete",
            "event_type.add_owner",
            "event_type.remove_owner",
            "field.create",
            "field.update",
            "field.delete",
            "meta_field.create",
            "meta_field.update",
            "meta_field.delete",
            "relation.create",
            "relation.update",
            "relation.delete",
            *_family("schema_drift", SchemaDriftAction),
        ),
    ),
    (
        "Variables",
        (
            "variable.create",
            "variable.update",
            "variable.delete",
            "variable.bulk_update",
            "variable.bulk_delete",
            "variable.values_clear",
            "variable.override_set",
            "variable.override_delete",
            "variable.override_bulk_set",
            "variable.override_bulk_delete",
            "variable.drift_action",
            "variable.property_drift_action",
        ),
    ),
    (
        "Versioning",
        (
            "plan_revision.create",
            "plan_branch.create",
            "plan_branch.delete",
            *_family("plan_branch", BranchTransitionAction),
            "plan_branch.merge",
            "plan_branch.revert",
            "plan_branch.update_from_main",
            "plan_branch.add_reviewer",
            "plan_branch.remove_reviewer",
            "plan_branch.comment_create",
            "plan_branch.comment_delete",
            "plan_branch.resolution_save",
            "plan_branch.resolution_delete",
            "plan_branch_settings.update",
        ),
    ),
    (
        "Scans & reconciliation",
        (
            "scan_config.create",
            "scan_config.update",
            "scan_config.delete",
            "scan_config.run",
            "scan_config.metrics_replay",
            "scan_config.event_groups.apply",
            "scan_job.cancel",
            # Accepting a shadow candidate files ``event.create`` under Events.
            "shadow_event.dismiss",
        ),
    ),
    (
        "Metrics & fact tables",
        (
            "metric_definition.create",
            "metric_definition.update",
            "metric_definition.bulk_update",
            "metric_definition.delete",
            "metric_definition.collect",
            "fact_table.create",
            "fact_table.update",
            "fact_table.delete",
            # The SQL-executing previews: they store nothing but run an editor's
            # SQL against a warehouse credential.
            "fact_table.preview",
            "metric.preview",
            "metric.fact_preview",
            "metric.series_preview",
        ),
    ),
    (
        "Alerting",
        (
            "alert_destination.create",
            "alert_destination.update",
            "alert_destination.delete",
            "alert_destination.test",
            "alert_rule.create",
            "alert_rule.update",
            "alert_rule.delete",
            "alert_rule.mute",
            "alert_rule.unmute",
            "alert_delivery.retry",
            *_family("alert_inbox", AlertInboxAction),
            "alert_inbox.notify_owners",
            "anomaly_scope_override.delete",
            "anomaly_settings.update",
        ),
    ),
    (
        "Signals",
        (
            "signal.acknowledge",
            "signal.unacknowledge",
            "signal.mute",
            "signal.unmute",
            "signal.mark_expected",
            "signal.unmark_expected",
            "signal.verdict",
            "signal.clear_verdict",
            "signal.notify_owners",
        ),
    ),
    (
        "Docs",
        (
            # Organization notes are written through a project, so these carry
            # that project too, whichever scope the note is in (F22).
            "doc.create",
            "doc.update",
            "doc.move",
            "doc.delete",
            "doc.folder_delete",
            "doc.restore",
            "doc.import",
            # F24: a note's or folder's sharing changed (before/after, no
            # content), and an org owner/admin read a note hidden from them.
            "doc.share_update",
            "doc.break_glass_read",
            # A note's stored translations: an AI run asked for, a hand edit or
            # a restore, a translation removed.
            "doc.translate",
            "doc.translation_edit",
            "doc.translation_restore",
            "doc.translation_delete",
        ),
    ),
    (
        "Project",
        (
            # ``project.delete`` is recorded after its subject is gone, so it has
            # no project: it is under Workspace.
            "project.create",
            "project.update",
            "project.reset",
            "project.member_add",
            "project.member_update",
            "project.member_remove",
            "project_tracker_config.update",
            "project.reset_anomalies",
            "project.reset_drifts",
            "project.retire_unused_variables",
            # The docs catalog's default languages for agents and people.
            "project.docs_languages",
            "chart_annotation.create",
            "chart_annotation.delete",
            # F18: windows in which anomalies are expected and not alerted.
            "planned_event.create",
            "planned_event.update",
            "planned_event.delete",
            # Carries a project only when the key is scoped to one.
            "api_key.create",
        ),
    ),
)

WORKSPACE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Workspace",
        (
            "data_source.create",
            "data_source.update",
            "data_source.delete",
            "user.invite",
            "user.invite_revoke",
            "user.invite_accept",
            "user.role_update",
            "api_key.revoke",
            "settings.update",
            # Written by the removed ``PUT /settings/ai``; older entries carry it.
            "settings.ai_update",
            "project.delete",
        ),
    ),
    (
        # F20 PR6. ``org.delete_complete`` is filed at platform scope (no
        # organization: it is gone), so no organization's feed ever lists it.
        "Organization",
        (
            "org.create",
            "org.update",
            # Written by ``PATCH /orgs/{org}`` before it became ``org.update``
            # (default project role); older entries carry it.
            "org.rename",
            "org.delete_request",
            "org.delete_cancel",
            "org.delete_complete",
            "org.member_role_update",
            "org.member_remove",
            "org.transfer_ownership",
            "org.group.create",
            "org.group.update",
            "org.group.delete",
            "org.group.member_add",
            "org.group.member_remove",
        ),
    ),
    (
        # F20: reading the log out — the export (filed when it starts) and the
        # audit webhook's settings (``org.audit_webhook.*``).
        "Audit log",
        (
            "org.audit_export",
            "org.audit_webhook.create",
            "org.audit_webhook.update",
            "org.audit_webhook.delete",
            "org.audit_webhook.rotate_secret",
            "org.audit_webhook.test",
        ),
    ),
    (
        # F20: an organization's OIDC single sign-on — its settings and domains
        # (``org.sso.*``) and the sign-ins through it (``user.sso_*``), all
        # filed in the organization.
        "Single sign-on",
        (
            "org.sso.update",
            "org.sso.domain_add",
            "org.sso.domain_verify",
            "org.sso.domain_remove",
            "user.sso_login",
            "user.sso_provision",
            "user.sso_link",
            # Sign in with Google, the instance's own client (sav5.2).
            "user.google_sign_in",
        ),
    ),
    (
        # F20: SCIM 2.0 provisioning. The owner's token and mapping changes
        # (``org.scim.token_*``, ``org.scim.config_update``) carry the owner;
        # everything the identity provider does through a token carries no
        # user and ``{"via": "scim", "token_prefix": ...}`` in the payload. A
        # role the admin-group mapping changes is ``org.member_role_update``
        # (under Organization) with ``"via": "scim_admin_group"``.
        "Provisioning (SCIM)",
        (
            "org.scim.token_create",
            "org.scim.token_revoke",
            "org.scim.config_update",
            "org.scim.user_provision",
            "org.scim.user_link",
            "org.scim.user_update",
            "org.scim.user_deactivate",
            "org.scim.user_reactivate",
            "org.scim.group_create",
            "org.scim.group_update",
            "org.scim.group_delete",
        ),
    ),
    (
        # F20 PR14: the platform console. Suspension and step-ins are filed in
        # the TARGET organization, so its owners read them in their own feed;
        # platform-admin grants have no organization (platform scope).
        "Platform",
        (
            "org.suspend",
            "org.unsuspend",
            "platform.step_in",
            "platform.step_in_end",
            "platform.admin_grant",
            "platform.admin_revoke",
        ),
    ),
)


def action_catalog() -> AuditActionCatalog:
    return AuditActionCatalog(
        project=[
            AuditActionGroup(label=label, actions=list(actions))
            for label, actions in PROJECT_GROUPS
        ],
        workspace=[
            AuditActionGroup(label=label, actions=list(actions))
            for label, actions in WORKSPACE_GROUPS
        ],
    )


def all_actions() -> set[str]:
    return {
        action for _label, actions in (*PROJECT_GROUPS, *WORKSPACE_GROUPS) for action in actions
    }
