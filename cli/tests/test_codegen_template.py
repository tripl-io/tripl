"""The Mustache subset (``tripl_cli.codegen.template``)."""

from __future__ import annotations

import pytest

from tripl_cli.codegen.template import Template, TemplateError, render


def test_variables_are_inserted_verbatim() -> None:
    # No HTML escaping: this renders source code, and values arrive pre-quoted.
    assert render("x = {{value}};", {"value": '"a" < b & c'}) == 'x = "a" < b & c;'
    assert render("{{&value}}", {"value": "<raw>"}) == "<raw>"


def test_dotted_names_and_the_current_item() -> None:
    context = {"function": {"name": "track"}, "items": ["a", "b"]}
    assert render("{{function.name}}", context) == "track"
    assert render("{{#items}}[{{.}}]{{/items}}", context) == "[a][b]"


def test_a_section_iterates_a_list_and_pushes_each_item() -> None:
    context = {"cases": [{"ident": "home"}, {"ident": "profile"}], "enum": "Screen"}
    text = render("{{#cases}}{{enum}}.{{ident}} {{/cases}}", context)
    assert text == "Screen.home Screen.profile "


def test_a_section_renders_a_mapping_once_and_truthy_scalars_once() -> None:
    assert render("{{#known}}{{count}}{{/known}}", {"known": {"count": 3}}) == "3"
    assert render("{{#flag}}yes{{/flag}}", {"flag": True}) == "yes"


@pytest.mark.parametrize("value", [None, False, "", [], {}])
def test_falsy_sections_render_nothing_and_inverted_ones_render(value: object) -> None:
    assert render("{{#v}}x{{/v}}", {"v": value}) == ""
    assert render("{{^v}}x{{/v}}", {"v": value}) == "x"


def test_a_missing_section_is_falsy_rather_than_an_error() -> None:
    assert render("{{#missing}}x{{/missing}}{{^missing}}y{{/missing}}", {}) == "y"


def test_standalone_tags_take_their_whole_line() -> None:
    template = "start\n  {{#items}}\n  - {{.}}\n  {{/items}}\n  {{! a note }}\nend\n"
    assert render(template, {"items": ["a", "b"]}) == "start\n  - a\n  - b\nend\n"


def test_inline_tags_keep_their_line() -> None:
    assert render("a {{#f}}b{{/f}} c\n", {"f": True}) == "a b c\n"


def test_comments_are_dropped() -> None:
    assert render("a{{! ignore me }}b", {}) == "ab"


def test_an_unknown_variable_is_an_error_with_its_line() -> None:
    with pytest.raises(TemplateError, match=r"custom\.mustache:3: unknown value \{\{nmae\}\}"):
        Template("one\ntwo\n{{nmae}}\n", source="custom.mustache").render({"name": "x"})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{{#a}}x", r"1: \{\{#a\}\} is never closed"),
        ("x\n{{/a}}", r"2: \{\{/a\}\} closes nothing"),
        ("{{#a}}\n{{/b}}", r"2: \{\{/b\}\} closes \{\{#a\}\} opened on line 1"),
        ("{{a b}}", r"is not a valid tag"),
    ],
)
def test_malformed_templates_are_refused(text: str, message: str) -> None:
    with pytest.raises(TemplateError, match=message):
        Template(text)


def test_line_numbers_survive_standalone_stripping() -> None:
    text = "{{#a}}\n{{/a}}\n\n{{oops}}\n"
    with pytest.raises(TemplateError, match=r"^4: "):
        Template(text).render({"a": True})


def test_a_parsed_template_renders_many_times() -> None:
    template = Template("{{x}}")
    assert [template.render({"x": n}) for n in (1, 2)] == ["1", "2"]
