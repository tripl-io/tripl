"""The legacy instance role is gone (F20, GH #273).

Organization roles (``organization_members``) and project roles replaced the
instance role in F20 PR4, and revision ``a2c4e6f8b0d3`` dropped
``users.role``, ``invitations.role`` and the ``user_role`` enum. A role that
applied across the whole instance would be a cross-organization hole: an
``owner`` in organization B passing an instance-wide check in organization A.
These tests keep it from coming back:

* ``User`` and ``Invitation`` map no ``role`` (column or attribute), and no
  table of the model metadata uses a ``user_role`` enum;
* ``invitations.org_role`` is NOT NULL: the invitee's role is always stated;
* the legacy enum and helper names are gone from the code;
* a pydantic schema validated ``from_attributes`` that declares a ``role`` field
  says which ORM attribute it reads.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import sqlalchemy as sa
from pydantic import BaseModel

import tripl.models  # noqa: F401  (populates Base.metadata with every table)
import tripl.schemas
from tripl.models import domain_enums
from tripl.models.base import Base
from tripl.models.invitation import Invitation
from tripl.models.user import User
from tripl.services import project_access

# ``from_attributes`` schemas with a ``role`` field, and the ORM attribute each
# one really reads. Anything validated from a ``User`` must not be here.
ROLE_SCHEMAS_READ_FROM = {
    "tripl.schemas.invitation.InvitationResponse": "org_role",
}


def test_user_and_invitation_map_no_role() -> None:
    for model in (User, Invitation):
        assert "role" not in model.__table__.c, model.__tablename__
        assert not hasattr(model, "role"), model.__name__


def test_no_table_uses_the_legacy_enum() -> None:
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.Enum) and column.type.name == "user_role"
    ]
    assert offenders == []


def test_an_invitation_always_states_its_organization_role() -> None:
    assert Invitation.__table__.c.org_role.nullable is False


def test_the_legacy_names_are_gone() -> None:
    assert not hasattr(domain_enums, "UserRole")
    assert not hasattr(project_access, "is_instance_owner")


def test_no_schema_reads_a_role_off_an_orm_row_implicitly() -> None:
    found: dict[str, str] = {}
    for info in pkgutil.walk_packages(tripl.schemas.__path__, "tripl.schemas."):
        module = importlib.import_module(info.name)
        for name, obj in vars(module).items():
            if not (
                inspect.isclass(obj)
                and issubclass(obj, BaseModel)
                and obj.__module__ == module.__name__
                and obj.model_config.get("from_attributes")
                and "role" in obj.model_fields
            ):
                continue
            alias = obj.model_fields["role"].validation_alias
            found[f"{module.__name__}.{name}"] = alias if isinstance(alias, str) else "role"
    assert found == ROLE_SCHEMAS_READ_FROM
