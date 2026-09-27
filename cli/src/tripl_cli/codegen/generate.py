"""Config + plan model -> the set of files ``tripl codegen`` writes. Pure: no I/O but
reading templates.

One file per (event type, language), named after the event type's namespace
type (``SeTracking.swift``, ``SeTracking.kt``, ``seTracking.ts``), plus one
shared transport file per language (``TriplTransport.swift``/``.kt``,
``triplTransport.ts``) holding the ``TriplDestination`` seam every generated
tracker without a configured ``transport`` forwards to.

Output is a function of the config and the plan alone — sorted inputs, no
timestamps — so ``tripl codegen --check`` can compare bytes. The header names
the plan's content hash (``plan_hash``), never its revision id, so a revision
that changes nothing the files depend on does not make them drift; an export
without a ``plan_hash`` gets a sha256 over the generated files instead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

from tripl_cli.check.config import (
    CODEGEN_LANGUAGES,
    STYLE_SCREEN_VIEW,
    STYLE_STRUCTURED,
    CheckConfig,
)
from tripl_cli.codegen import naming
from tripl_cli.codegen.context import (
    PLAN_HASH_PLACEHOLDER,
    Target,
    header_lines,
    structured_context,
)
from tripl_cli.codegen.context_named import named_context
from tripl_cli.codegen.languages import DIALECTS
from tripl_cli.codegen.model import CodegenModel
from tripl_cli.codegen.naming import TS
from tripl_cli.codegen.template import Template, TemplateError
from tripl_cli.errors import TriplConfigError, TriplError

# `type_names` keys that name a member, not a type: checked as identifiers only.
MEMBER_OVERRIDES = frozenset({"function"})

TRANSPORT_FILES = {
    "swift": "TriplTransport.swift",
    "kotlin": "TriplTransport.kt",
    "ts": "triplTransport.ts",
}


@dataclass(frozen=True)
class GeneratedFile:
    path: Path
    content: str
    language: str
    event_type: str | None = None


def builtin_template(name: str) -> str:
    """A packaged template, e.g. ``structured.swift``."""
    resource = resources.files("tripl_cli.codegen").joinpath("templates", f"{name}.mustache")
    return resource.read_text(encoding="utf-8")


def _template(target: Target, language: str) -> Template:
    custom = target.codegen.templates.get(language)
    if custom is None:
        return Template(builtin_template(f"{target.style}.{language}"), source=target.style)
    try:
        text = custom.read_text(encoding="utf-8")
    except OSError as exc:
        raise TriplConfigError(
            f"event_types.{target.event_type.name}.codegen.template: cannot read {custom}: {exc}"
        ) from None
    try:
        return Template(text, source=str(custom))
    except TemplateError as exc:
        raise TriplConfigError(f"template {exc}") from None


def languages_for(
    config: CheckConfig, own: Sequence[str], wanted: Sequence[str] | None
) -> list[str]:
    chosen = list(own or config.codegen.languages or CODEGEN_LANGUAGES)
    if wanted:
        chosen = [language for language in chosen if language in wanted]
    return chosen


def out_dir(config: CheckConfig, language: str, out: Path | None, *, flat: bool = False) -> Path:
    """Where ``language``'s files go. ``--out DIR`` is DIR itself when the run
    writes ONE language (``--lang swift --out App/Generated``), else DIR/<lang>."""
    if out is not None:
        return out if flat else out / language
    if language in config.codegen.out:
        return config.codegen.out[language]
    if config.codegen.out_root is not None:
        return config.codegen.out_root / language
    raise TriplConfigError(
        f"say where the {language} files go: --out DIR, or `codegen: {{out: DIR}}` "
        "(or `out: {swift: DIR, kotlin: DIR, ts: DIR}`) in the check config."
    )


def render_target(target: Target, language: str) -> str:
    dialect = DIALECTS[language]
    context: dict[str, Any]
    if target.style in (STYLE_STRUCTURED, STYLE_SCREEN_VIEW):
        context = structured_context(target, dialect)
    else:
        context = named_context(target, dialect)
    try:
        text = _template(target, language).render(context)
    except TemplateError as exc:
        raise TriplConfigError(f"template {exc}") from None
    return text.rstrip("\n") + "\n"


def file_name(context_namespace: str, language: str) -> str:
    if language == TS:
        return naming.lower_camel(context_namespace) + ".ts"
    return f"{context_namespace}.{DIALECTS[language].extension}"


def generate(
    config: CheckConfig,
    model: CodegenModel,
    project: str,
    *,
    languages: Sequence[str] | None = None,
    out: Path | None = None,
) -> list[GeneratedFile]:
    specs = [spec for spec in config.event_types if spec.codegen is not None]
    if not specs:
        raise TriplConfigError(
            "no event type has a `codegen:` block in the check config; add one, e.g. "
            "`event_types: {se: {codegen: {style: structured}}}`."
        )
    missing = [spec.name for spec in specs if model.event_type(spec.name) is None]
    if missing:
        raise TriplError(
            "the plan has no event type named "
            + ", ".join(repr(name) for name in missing)
            + (f" on branch {model.branch}" if model.branch else "")
            + "; fix `event_types:` in the check config or pick another --branch."
        )
    for spec in specs:
        assert spec.codegen is not None
        validate_type_names(
            spec.name,
            spec.codegen.type_names,
            languages_for(config, spec.codegen.languages, languages),
        )
    files: list[GeneratedFile] = []
    used_languages: list[str] = []
    selected = {
        language
        for spec in specs
        if spec.codegen is not None
        for language in languages_for(config, spec.codegen.languages, languages)
    }
    flat = len(selected) == 1
    for spec in specs:
        assert spec.codegen is not None
        event_type = model.event_type(spec.name)
        assert event_type is not None
        target = Target(
            spec=spec,
            codegen=spec.codegen,
            event_type=event_type,
            model=model,
            project=project,
            kotlin_package=config.codegen.kotlin_package,
        )
        for language in languages_for(config, spec.codegen.languages, languages):
            namespace = _namespace(target, language)
            path = out_dir(config, language, out, flat=flat) / file_name(namespace, language)
            files.append(GeneratedFile(path, render_target(target, language), language, spec.name))
            if language not in used_languages:
                used_languages.append(language)
    for language in used_languages:
        header = "\n".join(header_lines(None, model, project))
        context = {"header": header, "kotlin_package": config.codegen.kotlin_package}
        text = Template(builtin_template(f"transport.{language}"), source="transport").render(
            context
        )
        path = out_dir(config, language, out, flat=flat) / TRANSPORT_FILES[language]
        files.append(GeneratedFile(path, text.rstrip("\n") + "\n", language))
    seen: dict[Path, str] = {}
    for item in files:
        owner = seen.get(item.path)
        if owner is not None:
            raise TriplConfigError(
                f"event types {owner!r} and {item.event_type!r} would both write {item.path}; "
                "give one of them `type_names: {namespace: …}`."
            )
        seen[item.path] = item.event_type or "shared"
    return sorted(_with_plan_hash(files), key=lambda item: str(item.path))


def validate_type_names(
    event_type: str, type_names: Mapping[str, str], languages: Sequence[str]
) -> None:
    """A ``type_names`` override is taken verbatim, so it must be an identifier in
    EVERY language it is generated for — and, for a type, not one the language
    already owns (``String``, ``Map``), which a nested declaration would shadow."""
    for key, name in type_names.items():
        for language in languages:
            problem = None
            if not naming.is_identifier(name, language):
                problem = f"is not an identifier in {language}"
            elif key not in MEMBER_OVERRIDES and name in naming.RESERVED_TYPES[language]:
                problem = f"would shadow {language}'s own {name}"
            if problem is not None:
                raise TriplConfigError(
                    f"event_types.{event_type}.codegen.type_names.{key}: {name!r} {problem}; "
                    "pick another name."
                )


def _with_plan_hash(files: list[GeneratedFile]) -> list[GeneratedFile]:
    """Fill the header's plan hash when the export had none: a sha256 over the
    generated files (name and content), so it still changes exactly when they do."""
    if not any(PLAN_HASH_PLACEHOLDER in item.content for item in files):
        return files
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda item: (item.language, item.path.name)):
        digest.update(f"{item.language}/{item.path.name}\n".encode())
        digest.update(item.content.encode("utf-8"))
        digest.update(b"\x00")
    value = digest.hexdigest()
    return [
        replace(item, content=item.content.replace(PLAN_HASH_PLACEHOLDER, value)) for item in files
    ]


def _namespace(target: Target, language: str) -> str:
    override = target.type_override("namespace")
    if override is not None:
        return override
    return naming.type_name(target.event_type.name + " tracking", language)
