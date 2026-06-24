"""Tests for render_step_html body-from-DB path, the shared step-context
builder, and html2text plain-text derivation (U3 of the drip template editor).
"""

from __future__ import annotations

import pytest

from jp_adopt_api.domain.drips import (
    build_step_context,
    render_step_html,
)

SAMPLE_CONTEXT = build_step_context(
    contact_display_name="Alice Example",
    contact_email="alice@example.com",
    campaign_name="Adopter Welcome",
    step_position=0,
)


def test_body_html_renders_into_branded_shell() -> None:
    body = (
        "<h2>Hello {{ contact_display_name }}</h2>"
        "<p>Welcome to the program.</p>"
    )
    html, _ = render_step_html(body_html=body, context=SAMPLE_CONTEXT)
    # Body content is present...
    assert "Hello Alice Example" in html
    assert "Welcome to the program." in html
    # ...AND the code-managed shell wraps it (header brand + footer year).
    assert "Joshua Project" in html
    assert "&copy;" in html  # footer copyright from _base.html.jinja


def test_body_html_substitutes_merge_token() -> None:
    html, _ = render_step_html(
        body_html="<p>Hi {{ contact_display_name }}!</p>",
        context=SAMPLE_CONTEXT,
    )
    assert "Hi Alice Example!" in html
    assert "{{ contact_display_name }}" not in html


def test_body_html_takes_precedence_over_template_name(tmp_path) -> None:
    # A step that has both a (legacy) template name and body_html renders the
    # body, never the file. The body path resolves the shell from templates_dir.
    (tmp_path / "_base.html.jinja").write_text(
        "<html><body>{% block body %}{% endblock %}</body></html>"
    )
    (tmp_path / "legacy.mjml").write_text("<p>LEGACY FILE</p>")
    html, _ = render_step_html(
        template_name="legacy.mjml",
        body_html="<p>FROM DB</p>",
        context=SAMPLE_CONTEXT,
        templates_dir=tmp_path,
    )
    assert "FROM DB" in html
    assert "LEGACY FILE" not in html


def test_template_name_still_renders_when_no_body(tmp_path) -> None:
    (tmp_path / "hello.mjml").write_text(
        "<html><body>Hi {{ contact_display_name }}</body></html>"
    )
    html, _ = render_step_html(
        template_name="hello.mjml",
        context=SAMPLE_CONTEXT,
        templates_dir=tmp_path,
    )
    assert "Hi Alice Example" in html


def test_plain_text_preserves_link_url_and_list_bullets() -> None:
    body = (
        '<p>See <a href="https://joshuaproject.net/groups">the list</a>.</p>'
        "<ul><li>First</li><li>Second</li></ul>"
    )
    _, plain = render_step_html(body_html=body, context=SAMPLE_CONTEXT)
    # The old regex tag-strip dropped the URL and flattened the list; html2text
    # keeps both.
    assert "https://joshuaproject.net/groups" in plain
    assert "First" in plain and "Second" in plain
    assert "<a" not in plain and "<li>" not in plain


def test_build_step_context_exposes_all_send_and_preview_keys() -> None:
    # Guard against preview/send context divergence (preview historically
    # omitted contact_email, which would render fine in send but raise under
    # StrictUndefined in preview).
    assert set(SAMPLE_CONTEXT) == {
        "contact_display_name",
        "contact_email",
        "campaign_name",
        "step_position",
    }


def test_unknown_merge_token_raises_loudly() -> None:
    # StrictUndefined: a typo'd / unknown token must fail loudly in
    # preview/test, not silently render blank in production.
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        render_step_html(
            body_html="<p>{{ not_a_real_token }}</p>",
            context=SAMPLE_CONTEXT,
        )


def test_requires_body_or_template() -> None:
    with pytest.raises(ValueError):
        render_step_html(context=SAMPLE_CONTEXT)
