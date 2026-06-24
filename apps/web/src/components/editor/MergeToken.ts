import { Node, mergeAttributes } from "@tiptap/core";

/**
 * An atomic, non-editable inline chip representing a personalization token
 * (e.g. the recipient's name). Stored body HTML carries the literal
 * `{{ token }}` placeholder so server-side Jinja can substitute it; in the
 * editor it shows as a friendly chip the user can insert or delete whole, but
 * never type into and corrupt.
 *
 * In the editor the node serializes to `<span data-merge-token="...">label</span>`.
 * The asymmetry between that and the stored `{{ token }}` form is bridged by
 * the deterministic transforms below — applied on load (placeholders → chips)
 * and on save (chips → placeholders).
 */
export const MergeToken = Node.create({
  name: "mergeToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      token: {
        default: null,
        parseHTML: (el) => el.getAttribute("data-merge-token"),
        renderHTML: (attrs) =>
          attrs.token ? { "data-merge-token": attrs.token } : {},
      },
      label: {
        default: null,
        parseHTML: (el) => el.getAttribute("data-label"),
        renderHTML: (attrs) => (attrs.label ? { "data-label": attrs.label } : {}),
      },
    };
  },

  parseHTML() {
    return [{ tag: "span[data-merge-token]" }];
  },

  renderHTML({ HTMLAttributes, node }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        class:
          "merge-token-chip rounded bg-sky-100 px-1.5 py-0.5 text-sky-800 text-xs font-medium",
      }),
      String(node.attrs.label ?? node.attrs.token ?? ""),
    ];
  },
});

export type MergeTokenDef = { name: string; label: string };

const TOKEN_SPAN_RE =
  /<span[^>]*data-merge-token="([^"]+)"[^>]*>[\s\S]*?<\/span>/g;
const PLACEHOLDER_RE = /\{\{\s*(\w+)\s*\}\}/g;

/**
 * Editor HTML → stored HTML: replace each token chip span with its literal
 * `{{ token }}` placeholder so Jinja sees it verbatim.
 */
export function tokensToPlaceholders(html: string): string {
  return html.replace(TOKEN_SPAN_RE, (_m, token) => `{{ ${token} }}`);
}

/**
 * Stored HTML → editor HTML: replace each KNOWN `{{ token }}` placeholder with
 * the chip span the MergeToken node parses. Unknown tokens are left as-is (they
 * render as plain text, surfacing the typo rather than silently vanishing).
 */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function placeholdersToTokens(
  html: string,
  tokens: MergeTokenDef[],
): string {
  const labelByName = new Map(tokens.map((t) => [t.name, t.label]));
  return html.replace(PLACEHOLDER_RE, (whole, name) => {
    const label = labelByName.get(name);
    if (label === undefined) return whole;
    // name is \w+ (matched by PLACEHOLDER_RE); escape the label in case a
    // future server-provided label carries HTML-significant characters.
    const safeLabel = escapeHtml(label);
    return `<span data-merge-token="${name}" data-label="${safeLabel}">${safeLabel}</span>`;
  });
}
