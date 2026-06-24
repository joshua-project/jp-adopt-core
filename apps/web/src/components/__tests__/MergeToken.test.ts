import { describe, expect, it } from "vitest";

import {
  placeholdersToTokens,
  tokensToPlaceholders,
  type MergeTokenDef,
} from "../editor/MergeToken";

const TOKENS: MergeTokenDef[] = [
  { name: "contact_display_name", label: "Recipient name" },
];

describe("MergeToken transforms", () => {
  it("converts a known placeholder to a chip span on load", () => {
    const out = placeholdersToTokens(
      "<p>Hi {{ contact_display_name }}</p>",
      TOKENS,
    );
    expect(out).toContain('data-merge-token="contact_display_name"');
    expect(out).toContain("Recipient name");
    expect(out).not.toContain("{{ contact_display_name }}");
  });

  it("leaves unknown placeholders untouched on load", () => {
    const out = placeholdersToTokens("<p>{{ mystery }}</p>", TOKENS);
    expect(out).toBe("<p>{{ mystery }}</p>");
  });

  it("converts a chip span back to a literal placeholder on save", () => {
    const out = tokensToPlaceholders(
      '<p>Hi <span data-merge-token="contact_display_name" data-label="Recipient name">Recipient name</span></p>',
    );
    expect(out).toBe("<p>Hi {{ contact_display_name }}</p>");
  });

  it("round-trips stored placeholder → chip → stored placeholder", () => {
    const stored = "<p>Hello {{ contact_display_name }}, welcome.</p>";
    const editor = placeholdersToTokens(stored, TOKENS);
    const back = tokensToPlaceholders(editor);
    expect(back).toBe(stored);
  });

  it("converts multiple/adjacent known placeholders independently", () => {
    const out = placeholdersToTokens(
      "{{ contact_display_name }}{{ contact_display_name }}",
      TOKENS,
    );
    const matches = out.match(/data-merge-token="contact_display_name"/g);
    expect(matches).toHaveLength(2);
  });

  it("collapses two adjacent chips back to two separate placeholders", () => {
    const editor =
      '<span data-merge-token="contact_display_name" data-label="Recipient name">Recipient name</span>' +
      '<span data-merge-token="contact_display_name" data-label="Recipient name">Recipient name</span>';
    expect(tokensToPlaceholders(editor)).toBe(
      "{{ contact_display_name }}{{ contact_display_name }}",
    );
  });

  it("normalizes whitespace-variant placeholders", () => {
    const out = placeholdersToTokens("{{  contact_display_name  }}", TOKENS);
    expect(out).toContain('data-merge-token="contact_display_name"');
    expect(tokensToPlaceholders(out)).toBe("{{ contact_display_name }}");
  });

  it("escapes HTML-significant characters in a token label", () => {
    const out = placeholdersToTokens("{{ x }}", [
      { name: "x", label: 'A"<b>' },
    ]);
    expect(out).not.toContain('data-label="A"<b>"');
    expect(out).toContain("&quot;");
    expect(out).toContain("&lt;b&gt;");
  });
});
