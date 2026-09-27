"""Config + plan model -> the set of files ``tripl codegen`` writes. Pure: no I/O but
reading templates.

One file per (event type, language), named after the event type's namespace
type (``SeTracking.swift``, ``SeTracking.kt``, ``seTracking.ts``), plus one
shared transport file per language (``TriplTransport.swift``/``.kt``,
``triplTransport.ts``) holding the ``TriplDestination`` seam every generated
tracker without a configured ``transport`` forwards to.

An event type's ``codegen.files`` adds more files rendered from the same
context (``Acme{{names.pascal}}.kt`` once per plan field with ``each``); they sit
next to the event type's own file, must start with ``{{header}}``, and are
compared by ``--check`` and spared by stale cleanup like every other file.

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
    ExtraFile,
    check_file_name,
)
from tripl_cli.codegen import naming
from tripl_cli.codegen.context import (
    MARKER,
    PLAN_HASH_PLACEHOLDER,
    Target,
    field_names,
    header_lines,
    plan_line_prefix,
    structured_context,
)
from tripl_cli.codegen.context_named import named_context
from tripl_cli.codegen.context_plan import plan_context
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
    return _read_template(custom, f"event_types.{target.event_type.name}.codegen.template")


def _read_template(path: Path, where: str) -> Template:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TriplConfigError(f"{where}: cannot read {path}: {exc}") from None
    try:
        return Template(text, source=str(path))
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


def target_context(target: Target, language: str) -> dict[str, Any]:
    """Everything a template sees: the style's own keys, plus ``plan``, ``vars``
    (and ``transport``, from the style context) for custom templates."""
    dialect = DIALECTS[language]
    # One naming scope for the fields, so a field has the same `names` in `plan`,
    # `function.params` and `transport.args`.
    names = field_names(target, language)
    context: dict[str, Any]
    if target.style in (STYLE_STRUCTURED, STYLE_SCREEN_VIEW):
        context = structured_context(target, dialect, names)
    else:
        context = named_context(target, dialect, names)
    context["plan"] = plan_context(target, dialect, names)
    context["vars"] = {**target.vars, **target.codegen.vars}
    context["language"] = language
    return context


def render_target(target: Target, language: str) -> str:
    return _render(_template(target, language), target_context(target, language))


def _render(template: Template, context: Mapping[str, Any], *frames: Mapping[str, Any]) -> str:
    try:
        text = template.render(context, *frames)
    except TemplateError as exc:
        raise TriplConfigError(f"template {exc}") from None
    return text.rstrip("\n") + "\n"


def render_extra_files(
    target: Target, extra: ExtraFile, context: Mapping[str, Any], directory: Path
) -> list[GeneratedFile]:
    """One ``codegen.files`` entry: one file, or one per ``each`` item."""
    template = _read_template(extra.template, f"{extra.where}.template")
    try:
        name_template = Template(extra.file, source=f"{extra.where}.file")
    except TemplateError as exc:
        raise TriplConfigError(f"{exc}") from None
    frames: list[tuple[Mapping[str, Any], ...]] = [()]
    if extra.each is not None:
        plan = context["plan"]
        if extra.each == "closed_fields":
            items = [item for item in plan["fields"] if item["closed"]]
        else:
            items = plan[extra.each]
        frames = [({"item": item}, item) for item in items]
    files: list[GeneratedFile] = []
    for scope in frames:
        try:
            name = name_template.render(context, *scope)
        except TemplateError as exc:
            raise TriplConfigError(f"{exc}") from None
        check_file_name(name, extra.language, f"{extra.where}.file")
        if not naming.is_file_name(name):
            raise TriplConfigError(
                f"{extra.where}.file: {name!r} is not a plain file name; "
                "use letters, digits, '_', '-' and '.'."
            )
        content = _render(template, context, *scope)
        lines = content.splitlines()
        if (
            len(lines) < 2
            or MARKER not in lines[0]
            or not lines[1].startswith(plan_line_prefix(target.project))
        ):
            raise TriplConfigError(
                f"{extra.where}.template: {extra.template} must start with {{{{header}}}}, "
                "so `tripl codegen --check` and stale-file cleanup know the file as generated."
            )
        files.append(
            GeneratedFile(directory / name, content, extra.language, target.event_type.name)
        )
    return files


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
            vars=config.codegen.vars,
        )
        own = languages_for(config, spec.codegen.languages, None)
        for extra in spec.codegen.files:
            if extra.language not in own:
                raise TriplConfigError(
                    f"{extra.where}.language: {extra.language} is not one of this event "
                    f"type's languages ({', '.join(own)})."
                )
        for language in languages_for(config, spec.codegen.languages, languages):
            namespace = _namespace(target, language)
            directory = out_dir(config, language, out, flat=flat)
            context = target_context(target, language)
            text = _render(_template(target, language), context)
            files.append(
                GeneratedFile(directory / file_name(namespace, language), text, language, spec.name)
            )
            for extra in spec.codegen.files:
                if extra.language == language:
                    files.extend(render_extra_files(target, extra, context, directory))
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
    seen: dict[str, str] = {}
    for item in files:
        # Case-folded: on macOS and Windows `AcmeScreen.kt` and `acmeScreen.kt` are one file.
        key = str(item.path.parent / item.path.name.casefold())
        owner = seen.get(key)
        if owner is not None and owner == item.event_type:
            raise TriplConfigError(
                f"event type {owner!r} would write {item.path} twice; give its `files` "
                "entries names that differ from each other and from its own file."
            )
        other = item.event_type or "shared"
        if owner is not None and "shared" in (owner, other):
            # The shared transport file is written last; its name cannot change.
            culprit = owner if other == "shared" else other
            raise TriplConfigError(
                f"{item.path.name} is the shared transport file; give the `files` entry "
                f"of event type {culprit!r} another name."
            )
        if owner is not None:
            raise TriplConfigError(
                f"event types {owner!r} and {other!r} would both write {item.path}; "
                "give one of them `type_names: {namespace: …}` or another `files` name."
            )
        seen[key] = item.event_type or "shared"
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
