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


def test_email_templates_dir_resolves_to_the_shell() -> None:
    # Regression: the installed-package container layout (no `src` level) made
    # the old 4-levels-up computation point at a nonexistent dir, so
    # `{% extends '_base.html.jinja' %}` raised TemplateNotFound at send time.
    from jp_adopt_api.domain.drips import EMAIL_TEMPLATES_DIR

    assert EMAIL_TEMPLATES_DIR.is_dir()
    assert (EMAIL_TEMPLATES_DIR / "_base.html.jinja").is_file()


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


def test_every_merge_token_resolves_in_context() -> None:
    # The editor's insertable tokens (MERGE_TOKENS) must all be keys the render
    # context provides, or inserting one would render as inert literal text
    # instead of personalizing. Guards against the two lists drifting.
    from jp_adopt_api.domain.drips import MERGE_TOKENS

    token_names = {name for name, _label in MERGE_TOKENS}
    assert token_names <= set(SAMPLE_CONTEXT)


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


def test_unknown_merge_token_renders_as_inert_literal() -> None:
    # An unknown / typo'd token in an authored body must NOT crash the send
    # (it would poison the whole drip batch). It renders as literal text.
    html, _ = render_step_html(
        body_html="<p>Hi {{ not_a_real_token }}</p>",
        context=SAMPLE_CONTEXT,
    )
    assert "{{ not_a_real_token }}" in html


def test_body_html_is_not_evaluated_as_a_template_ssti() -> None:
    # Authored bodies are attacker-influenced (staff type anything; nh3 does not
    # strip {{ }}/{% %}). They must be substituted as data, never compiled — or
    # a body like the gadget below would achieve RCE. Assert it renders
    # literally and is NOT evaluated.
    payload = "<p>{{ 7 * 7 }} {% for x in range(3) %}x{% endfor %}</p>"
    html, _ = render_step_html(body_html=payload, context=SAMPLE_CONTEXT)
    assert "49" not in html
    assert "xxx" not in html
    assert "{{ 7 * 7 }}" in html  # left as literal text, unevaluated

    gadget = "<p>{{ campaign_name.__class__.__mro__ }}</p>"
    html2, _ = render_step_html(body_html=gadget, context=SAMPLE_CONTEXT)
    assert "__mro__" in html2  # literal — object traversal never executed
    assert "<class" not in html2


def test_merge_value_is_html_escaped() -> None:
    # A display name containing markup must be escaped, not injected as live HTML.
    ctx = build_step_context(
        contact_display_name="<b>Mallory</b>",
        contact_email="m@example.com",
        campaign_name="C",
        step_position=0,
    )
    html, _ = render_step_html(
        body_html="<p>Hi {{ contact_display_name }}</p>", context=ctx
    )
    assert "&lt;b&gt;Mallory&lt;/b&gt;" in html
    assert "<b>Mallory</b>" not in html


def test_bare_authored_tags_get_brand_inline_styles() -> None:
    # Tiptap emits bare tags; render injects brand inline styles so authored
    # bodies match the seeded copy (and email clients, which ignore <style>).
    html, _ = render_step_html(
        body_html="<h2>Title</h2><p>Body</p>", context=SAMPLE_CONTEXT
    )
    assert 'style="margin:0 0 16px;color:#2C474B;font-size:18px"' in html
    assert 'style="margin:0 0 16px;color:#374151"' in html


def test_lists_get_explicit_markers_for_email_clients() -> None:
    # Outlook resets list styling; render must inject explicit list-style-type
    # so numbered/bulleted lists still show markers in email.
    html, _ = render_step_html(
        body_html="<ol><li><p>One</p></li></ol><ul><li><p>a</p></li></ul>",
        context=SAMPLE_CONTEXT,
    )
    assert "list-style-type:decimal" in html
    assert "list-style-type:disc" in html


def test_authored_tags_keep_their_own_style() -> None:
    # A tag that already carries a style (e.g. seeded content) is left alone.
    html, _ = render_step_html(
        body_html='<p style="color:red">x</p>', context=SAMPLE_CONTEXT
    )
    assert 'style="color:red"' in html
    assert "color:#374151" not in html


def test_requires_body_or_template() -> None:
    with pytest.raises(ValueError):
        render_step_html(context=SAMPLE_CONTEXT)
