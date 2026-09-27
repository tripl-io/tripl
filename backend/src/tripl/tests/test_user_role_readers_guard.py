"""Nothing reads ``users.role`` any more (F20 PR4, critique #5 and #6).

Organization roles (``organization_members``) and project roles replaced the
instance role. The column stays until a cleanup PR drops it, and a reader left
behind would be a cross-organization hole: once a user is ``owner`` in org B,
an instance-wide ``users.role == "owner"`` check would let them pass in org A.

The guard is syntactic, over the AST of every module under ``src/tripl``
outside the tests, and flags:

* the column itself: ``User.role`` / ``Invitation.role`` (``invitations.role``
  shares the legacy enum);
* the legacy enum or helper by name: ``UserRole``, ``is_instance_owner``;
* ``.role`` read off a name that holds a user or an invitation
  (``user.role``, ``current_user.role``, ``invitation.role``, ...);
* ``getattr(x, "role")``, the spelling that dodges the patterns above.

Plus the implicit reader grep cannot see: a pydantic schema validated
``from_attributes`` that declares a ``role`` field reads it off whatever ORM
row it is given, so every such schema must say where its role comes from.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

from pydantic import BaseModel

import tripl
import tripl.schemas

SRC_ROOT = Path(tripl.__file__).resolve().parent

# Modules allowed to NAME the legacy role: its enum, and the two column
# definitions that stay until the cleanup PR. Nothing here reads a row's value.
ALLOWED_MODULES = {
    "models/domain_enums.py",  # class UserRole
    "models/user.py",  # users.role column definition
    "models/invitation.py",  # invitations.role column definition
}

_LEGACY_NAMES = {"UserRole", "is_instance_owner"}
_ROLE_COLUMN_OWNERS = {"User", "Invitation"}
# Local names that hold a ``User`` (or an ``Invitation``) across the codebase.
_USERISH_NAMES = {
    "user",
    "current_user",
    "api_user",
    "actor",
    "target",
    "owner",
    "creator",
    "author",
    "inviter",
    "invitee",
    "invitation",
    "recipient",
    "member_user",
}

# ``from_attributes`` schemas with a ``role`` field, and the ORM attribute each
# one really reads. Anything validated from a ``User`` must not be here.
ROLE_SCHEMAS_READ_FROM = {
    "tripl.schemas.invitation.InvitationResponse": "org_role",
}


def _modules() -> list[tuple[str, ast.Module]]:
    found: list[tuple[str, ast.Module]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative.startswith("tests/"):
            continue
        found.append((relative, ast.parse(path.read_text(encoding="utf-8"), filename=relative)))
    return found


def _offences(tree: ast.Module) -> list[tuple[int, str]]:
    offences: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _LEGACY_NAMES:
            offences.append((node.lineno, node.id))
        elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] in _LEGACY_NAMES:
            offences.append((getattr(node, "lineno", 0), f"import {node.name}"))
        elif isinstance(node, ast.Attribute):
            if node.attr in _LEGACY_NAMES:
                offences.append((node.lineno, f".{node.attr}"))
            elif (
                node.attr == "role"
                and isinstance(node.value, ast.Name)
                and (node.value.id in _ROLE_COLUMN_OWNERS or node.value.id in _USERISH_NAMES)
            ):
                offences.append((node.lineno, f"{node.value.id}.role"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "role"
        ):
            offences.append((node.lineno, 'getattr(..., "role")'))
    return offences


def test_the_guard_sees_the_source_tree() -> None:
    """Guard the guard: an empty walk would pass vacuously."""
    modules = dict(_modules())
    assert len(modules) > 200
    assert "services/project_access.py" in modules
    assert "api/deps.py" in modules
    assert not any(name.startswith("tests/") for name in modules)


def test_the_guard_catches_every_reader_shape() -> None:
    source = (
        "from tripl.models.domain_enums import UserRole\n"
        "a = user.role == 'owner'\n"
        "b = select(User.id).where(User.role == UserRole.owner.value)\n"
        "c = is_instance_owner(current_user)\n"
        "d = getattr(actor, 'role')\n"
        "e = invitation.role\n"
        "f = project_access.is_instance_owner\n"
    )
    found = {label for _line, label in _offences(ast.parse(source))}
    assert found == {
        "import UserRole",
        "user.role",
        "User.role",
        "UserRole",
        "is_instance_owner",
        'getattr(..., "role")',
        "invitation.role",
        ".is_instance_owner",
    }
    # Other roles are fine: organization and project membership rows.
    clean = "x = membership.role\ny = OrganizationMember.role\nz = row.role\n"
    assert _offences(ast.parse(clean)) == []


def test_nothing_outside_the_allowlist_reads_the_instance_role() -> None:
    offenders = [
        f"{module}:{line}: {label}"
        for module, tree in _modules()
        if module not in ALLOWED_MODULES
        for line, label in _offences(tree)
    ]
    assert offenders == [], (
        "users.role is no longer read (F20 PR4); use the organization role "
        "(services.project_access / api.deps.request_org_role) or the project role:\n"
        + "\n".join(offenders)
    )


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
