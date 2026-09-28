"""Which data sources a project may point at — one rule, every door.

Four request doors ask this question: a ``sql`` metric's save and preview
(``metric_definition_service.load_project_data_source``) and a fact table's save
and preview (``fact_table_service._verify_data_source`` and
``fact_table_introspection_service._load_project_data_source``). Until this
module they answered it differently — the metric doors asked who OWNS the
source, the fact-table doors asked whether a ``ScanConfig`` binds it to this
project — and a user in the fact-table wizard could meet both verdicts for one
id inside a single flow (tripl-0zpq.177, tripl-0zpq.353).

OWNERSHIP is the rule that survived, because the binding rule is unworkable on
the metric doors: a ``sql`` metric needs no scan at all (``check_metric_
definitions_due`` selects active metrics with no ``ScanConfig`` join), creating
a scan config is org-admin-only so there is no way for an owner to bless a
metrics-only warehouse, and the check re-runs on every definition update — so a
colour-only PATCH would 404 because the form resends the stored data source with
it. What ownership costs the fact-table doors is written down in
:func:`data_source_out_of_project_scope`.
"""

import uuid
from collections.abc import Collection

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.data_source import DataSource
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig

# The one sentence all four doors refuse with, at one status (404). It names the
# real cause instead of claiming the row is missing: the older "Data source not
# found" was justified as anti-enumeration, but ``GET /api/v1/data-sources``
# hands every authenticated user the whole workspace inventory (id, name,
# db_type, project_id; only the connection fields are redacted) and
# ``GET /api/v1/data-sources/{id}`` separates 200 from 404 for any UUID — so the
# only person the vague message fenced was the legitimate editor, sent hunting
# for a row that exists (tripl-0zpq.354).
#
# The wording stays UNIFORM across "no such row" and "out of scope", which is the
# half that does buy something: a project-scoped API key is refused on those
# slug-less data-source routes (``api.deps``), so for that caller distinct
# messages here would be the one way to tell a non-existent id from another
# project's source.
DATA_SOURCE_NOT_AVAILABLE = "Data source is not available in this project."


def data_source_out_of_project_scope(
    data_source: DataSource,
    *,
    project_id: uuid.UUID,
    project_organization_id: uuid.UUID,
    scanning_project_ids: Collection[uuid.UUID],
) -> bool:
    """Whether ``data_source`` is identifiably ANOTHER project's warehouse.

    A pure function of three facts, so that the sync collector can apply the
    identical verdict to an ALREADY-STORED ``data_source_id`` without importing
    the async request path (tripl-0zpq.347). One rule, every caller: a predicate
    that drifts between save, preview and collect is the shape of the defect
    this came from.

    A data source is refused when it is:

    * in a different organization from the project — whatever its
      ``project_id`` says, another organization's warehouse is never shared;
    * owned by a different project — ``project_id`` set to someone else, the
      demo case, where that column exists precisely to scope a synthetic
      warehouse to one workspace;
    * workspace-global (``project_id`` NULL) but scanned by some other project
      and not by this one — an owner pointed it at that project, and an editor
      here should not borrow its credential.

    ``scanning_project_ids`` is every project with a ``ScanConfig`` bound to this
    source. It is consulted only for a workspace-global source, where it is the
    sole evidence of a claim; for an owned source the column decides, and the
    caller may pass an empty set rather than query (see
    :func:`scanning_project_ids_for`).

    WHAT THIS DELIBERATELY ALLOWS, and did not before on the fact-table doors: a
    workspace-global source that NO project scans is reachable from every
    project. That is what the NULL means — shared — and it is the configuration
    an owner creates for a warehouse that is queried but never scanned. So is a
    source owned by this project with no ``ScanConfig`` yet. In the other
    direction the rule is stricter than the binding rule it replaced: a source
    OWNED by project Z stays refused here even if this project happens to scan
    it, where "has a ScanConfig in this project" would have let it through.
    """
    if data_source.organization_id != project_organization_id:
        return True
    if data_source.project_id is not None:
        return data_source.project_id != project_id
    # NULL means shared, so a source nobody scans is everyone's. One that some
    # other project scans — and this one does not — is theirs in all but the
    # column, and an editor here should not borrow its credential.
    return bool(scanning_project_ids) and project_id not in scanning_project_ids


async def project_organization_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    """The organization ``project_id`` belongs to, for the org half of the rule."""
    org_id = await session.scalar(select(Project.organization_id).where(Project.id == project_id))
    if org_id is None:
        msg = f"Project {project_id} does not exist"
        raise LookupError(msg)
    return org_id


async def scanning_project_ids_for(
    session: AsyncSession, data_source: DataSource
) -> set[uuid.UUID]:
    """Every project with a ``ScanConfig`` bound to this source — or nothing.

    Skipped for an owned source, where ``data_source_out_of_project_scope``
    ignores the set and the column decides on its own; one query otherwise.
    """
    if data_source.project_id is not None:
        return set()
    rows = await session.execute(
        select(ScanConfig.project_id).where(ScanConfig.data_source_id == data_source.id)
    )
    return set(rows.scalars().all())


def usable_by_project_clause(
    project_id: uuid.UUID, organization_id: uuid.UUID
) -> ColumnElement[bool]:
    """SQL: the ``DataSource`` row is NOT out of ``project_id``'s scope.

    :func:`data_source_out_of_project_scope` as a filter, for lists that pick
    among many sources at once (the docs catalog's ``[[data-source:NAME]]``
    links and link suggestions): the project's organization, and either owned
    by this project, or workspace-global and scanned by this project or by no
    project at all.
    """
    scanned_here = exists().where(
        ScanConfig.data_source_id == DataSource.id, ScanConfig.project_id == project_id
    )
    scanned_elsewhere = exists().where(
        ScanConfig.data_source_id == DataSource.id, ScanConfig.project_id != project_id
    )
    return and_(
        DataSource.organization_id == organization_id,
        or_(
            DataSource.project_id == project_id,
            and_(DataSource.project_id.is_(None), or_(scanned_here, ~scanned_elsewhere)),
        ),
    )
