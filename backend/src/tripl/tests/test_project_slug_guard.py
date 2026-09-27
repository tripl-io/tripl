"""No code outside ``services/project_lookup.py`` filters projects by slug (F20 PR2).

A slug names a project only inside an organization, so a bare ``Project.slug ==``
lookup silently reaches across organizations once slugs stop being globally
unique. Every lookup goes through ``resolve_project`` / ``resolve_project_id``,
or ``project_slug_clause`` inside a join; this scan fails on any other
comparison. Reading ``Project.slug`` as a column (``select(Project.slug)``) is
not a lookup and is not flagged.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
#: ``backend/scripts``: operator scripts resolve slugs too (they bind an org with
#: ``bound_org``), so they are held to the same rule. Reported as ``scripts/...``.
SCRIPTS_ROOT = PACKAGE_ROOT.parents[1] / "scripts"
_EXCLUDED_DIRS = frozenset({"tests", "alembic"})

#: path (relative to the package, or ``scripts/...``) -> the number of sites it
#: may hold, or None for any number. Each entry says why.
ALLOWLIST: dict[str, int | None] = {
    # The one resolver.
    "services/project_lookup.py": None,
    # create_project's and update_project's slug-availability checks. They stay
    # instance-wide while uq_projects_slug is global (until PR5 swaps it for a
    # per-organization constraint): an org-scoped check would let a cross-org
    # duplicate through to an IntegrityError 500.
    "services/project_service.py": 2,
}

_MATCHER_METHODS = frozenset(
    {"in_", "not_in", "notin_", "like", "ilike", "not_like", "not_ilike", "startswith",
     "endswith", "contains", "is_", "isnot", "is_not", "is_distinct_from", "op"}
)  # fmt: skip
#: SQL functions a slug may be wrapped in on one side of a comparison
#: (``func.lower(Project.slug) == s``, ``cast(Project.slug, ...)``).
_WRAPPER_FUNCS = frozenset({"lower", "upper", "trim", "cast", "coalesce", "type_coerce"})
#: ``[alias.]slug`` compared with a bind parameter, a literal or a list:
#: ``p.slug = :s``, ``slug IN (...)``, ``projects.slug LIKE '%x'``. Prose such as
#: "the slug in the URL" has no operand of that shape and is not flagged.
_RAW_SQL = re.compile(
    r"\b(?:\w+\.)?slug\s*(?:=|!=|<>|\bin\b|\blike\b|\bilike\b)\s*[:%?'\"(]",
    re.IGNORECASE,
)
_RAW_SQL_TABLE = re.compile(r"\bprojects\b", re.IGNORECASE)


def _project_names(tree: ast.Module) -> set[str]:
    """Names bound to ``Project``: the class, ``as`` aliases, ``aliased(Project)``."""
    names = {"Project"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Project" and alias.asname:
                    names.add(alias.asname)
    # aliased() may wrap an alias bound anywhere above, so iterate to a fixpoint.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _call_name(node.value) == "aliased"
                and node.value.args
                and _is_project_ref(node.value.args[0], names)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        changed = True
    return names


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_project_ref(node: ast.AST, names: set[str]) -> bool:
    """``Project``, an alias of it, or ``something.Project`` (``models.Project``)."""
    if isinstance(node, ast.Name):
        return node.id in names
    return isinstance(node, ast.Attribute) and node.attr == "Project"


def _is_project_slug(node: ast.AST, names: set[str]) -> bool:
    if not (isinstance(node, ast.Attribute) and node.attr == "slug"):
        return False
    receiver = node.value
    if _is_project_ref(receiver, names):
        return True
    # Project.__table__.c.slug
    return (
        isinstance(receiver, ast.Attribute)
        and receiver.attr == "c"
        and isinstance(receiver.value, ast.Attribute)
        and receiver.value.attr == "__table__"
        and _is_project_ref(receiver.value.value, names)
    )


def _is_slug_operand(node: ast.AST, names: set[str]) -> bool:
    """A project slug, possibly inside SQL wrapper functions."""
    if _is_project_slug(node, names):
        return True
    return (
        isinstance(node, ast.Call)
        and _call_name(node) in _WRAPPER_FUNCS
        and any(_is_slug_operand(arg, names) for arg in node.args)
    )


def _docstrings(tree: ast.Module) -> set[int]:
    """ids of docstring constants: prose, never SQL."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _violations(tree: ast.Module) -> list[int]:
    names = _project_names(tree)
    docstrings = _docstrings(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_is_slug_operand(o, names) for o in operands):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _MATCHER_METHODS
                and _is_slug_operand(func.value, names)
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "filter_by"
                and any(kw.arg == "slug" for kw in node.keywords)
            ):
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _RAW_SQL_TABLE.search(node.value)
            and _RAW_SQL.search(node.value)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def _scan_root(root: Path, prefix: str, found: dict[str, list[int]]) -> None:
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _EXCLUDED_DIRS & set(rel.parts):
            continue
        lines = _violations(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if lines:
            found[prefix + rel.as_posix()] = lines


def _scan() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    _scan_root(PACKAGE_ROOT, "", found)
    if SCRIPTS_ROOT.is_dir():
        _scan_root(SCRIPTS_ROOT, "scripts/", found)
    return found


def test_no_bare_project_slug_lookups_outside_project_lookup() -> None:
    offenders: list[str] = []
    for rel, lines in _scan().items():
        if rel in ALLOWLIST:
            allowed = ALLOWLIST[rel]
            if allowed is None or len(lines) == allowed:
                continue
            offenders.append(f"{rel}: {len(lines)} sites (allowlisted for {allowed}) at {lines}")
            continue
        offenders.append(f"{rel}: lines {lines}")
    assert not offenders, (
        "Resolve projects through tripl.services.project_lookup (resolve_project, "
        "resolve_project_id, or project_slug_clause in a join), not a bare "
        "Project.slug comparison:\n" + "\n".join(offenders)
    )


def test_allowlisted_counts_are_exact() -> None:
    """A stale allowlist entry is an error too: it would silently admit a new site."""
    found = _scan()
    for rel, allowed in ALLOWLIST.items():
        if allowed is not None:
            assert len(found.get(rel, [])) == allowed, rel


def test_the_scanner_flags_the_forms_it_guards() -> None:
    source = """
from tripl.models.project import Project
from tripl.models.project import Project as P
a = select(Project).where(Project.slug == s)
b = select(Project).where(s != Project.slug)
c = select(Project).where(P.slug == s)
d = select(Project).where(Project.slug.in_(xs))
e = select(Project).where(Project.slug.ilike(x))
f = q.filter_by(slug=s)
g = text("SELECT id FROM projects WHERE projects.slug = :s")
h = select(Project).where(func.lower(Project.slug) == s)
i = select(models.Project).where(models.Project.slug == s)
p = aliased(Project)
j = select(p).where(p.slug == s)
k = select(Project).where(Project.__table__.c.slug == s)
m = text("SELECT id FROM projects p WHERE p.slug = :s")
n = text("SELECT id FROM projects WHERE slug = :s")
ok1 = select(Project.slug).where(Project.id == i)
ok2 = select(Project.id, Project.slug.label("project_slug"))
ok3 = text("SELECT id FROM users WHERE slug = :s")
"""
    assert _violations(ast.parse(source)) == [4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 17]


def test_the_scan_sees_the_package() -> None:
    # Guard against the scan silently matching nothing (a wrong package root).
    assert (PACKAGE_ROOT / "services" / "project_lookup.py").is_file()
    assert sum(1 for _ in PACKAGE_ROOT.rglob("*.py")) > 100
    assert SCRIPTS_ROOT.is_dir()
