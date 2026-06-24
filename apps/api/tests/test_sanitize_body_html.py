"""Tests for sanitize_body_html (U9) — nh3 allowlist + merge-token survival."""

from __future__ import annotations

from jp_adopt_api.domain.drips import sanitize_body_html


def test_strips_script_tag_and_content() -> None:
    out = sanitize_body_html("<p>Hi</p><script>alert(1)</script>")
    assert "<script" not in out
    assert "alert(1)" not in out  # clean_content_tags drops the content too
    assert "<p>Hi</p>" in out


def test_strips_javascript_href_keeps_safe_links() -> None:
    out = sanitize_body_html(
        '<a href="javascript:alert(1)">x</a>'
        '<a href="https://joshuaproject.net">ok</a>'
    )
    assert "javascript:" not in out
    assert 'href="https://joshuaproject.net"' in out
    # link_rel hardening applied to surviving anchors.
    assert 'rel="noopener noreferrer"' in out


def test_allows_the_editor_tag_set() -> None:
    body = (
        "<h1>T</h1><h2>T</h2><h3>T</h3><p><strong>b</strong> <em>i</em></p>"
        '<ul><li>one</li></ul><ol><li>two</li></ol><br>'
    )
    out = sanitize_body_html(body)
    for tag in ("<h1>", "<h2>", "<h3>", "<strong>", "<em>", "<ul>", "<ol>", "<li>"):
        assert tag in out


def test_strips_disallowed_tag_but_keeps_text() -> None:
    out = sanitize_body_html("<table><tr><td>kept</td></tr></table>")
    assert "<table" not in out and "<td" not in out
    assert "kept" in out


def test_strips_event_handlers_and_disallowed_attrs() -> None:
    out = sanitize_body_html(
        '<p onclick="alert(1)">x</p>'
        '<a href="https://x" title="ok" id="drop">y</a>'
    )
    assert "onclick" not in out
    assert 'id="drop"' not in out
    assert 'title="ok"' in out  # title is allowlisted on <a>


def test_strips_img_and_data_uri() -> None:
    out = sanitize_body_html('<img src=x onerror=alert(1)>')
    assert "<img" not in out and "onerror" not in out
    out2 = sanitize_body_html('<a href="data:text/html,x">z</a>')
    assert "data:" not in out2


def test_merge_token_survives_byte_identical() -> None:
    # The load-bearing invariant: {{ }} placeholders are text, not markup, so
    # nh3 must leave them exactly as-is for Jinja to substitute later.
    body = "<p>Hi {{ contact_display_name }}, welcome.</p>"
    out = sanitize_body_html(body)
    assert "{{ contact_display_name }}" in out
