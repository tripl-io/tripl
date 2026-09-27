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
_EXCLUDED_DIRS = frozenset({"tests", "alembic"})

#: path (relative to the package) -> the number of sites it may hold, or None
#: for any number. Each entry says why.
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
_RAW_SQL = re.compile(r"\bprojects\.slug\s*(=|!=|<>|\bin\b|\blike\b|\bilike\b)", re.IGNORECASE)


def _project_names(tree: ast.Module) -> set[str]:
    names = {"Project"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Project" and alias.asname:
                    names.add(alias.asname)
    return names


def _is_project_slug(node: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "slug"
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    )


def _violations(tree: ast.Module) -> list[int]:
    names = _project_names(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_is_project_slug(o, names) for o in operands):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _MATCHER_METHODS
                and _is_project_slug(func.value, names)
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "filter_by"
                and any(kw.arg == "slug" for kw in node.keywords)
            ):
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _RAW_SQL.search(node.value)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def _scan() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(PACKAGE_ROOT)
        if _EXCLUDED_DIRS & set(rel.parts):
            continue
        lines = _violations(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if lines:
            found[rel.as_posix()] = lines
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
ok1 = select(Project.slug).where(Project.id == i)
ok2 = select(Project.id, Project.slug.label("project_slug"))
"""
    assert _violations(ast.parse(source)) == [4, 5, 6, 7, 8, 9, 10]


def test_the_scan_sees_the_package() -> None:
    # Guard against the scan silently matching nothing (a wrong package root).
    assert (PACKAGE_ROOT / "services" / "project_lookup.py").is_file()
    assert sum(1 for _ in PACKAGE_ROOT.rglob("*.py")) > 100
