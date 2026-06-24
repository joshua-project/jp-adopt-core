/**
 * Smoke test for the REAL Tiptap editor (not mocked) — guards against init
 * crashes the mocked StepEditForm test can't catch (e.g. duplicate-extension
 * errors when StarterKit already bundles an extension we also add).
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RichTextEditor } from "../editor/RichTextEditor";

const TOKENS = [{ name: "contact_display_name", label: "Recipient name" }];

describe("RichTextEditor (real Tiptap)", () => {
  it("mounts without throwing and shows the toolbar", async () => {
    render(
      <RichTextEditor
        value="<p>Hello {{ contact_display_name }}</p>"
        onChange={vi.fn()}
        tokens={TOKENS}
      />,
    );
    // immediatelyRender:false means the editor mounts after an effect tick.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Bold" })).toBeTruthy(),
    );
    // The merge-token insert control is present.
    expect(screen.getByRole("button", { name: "Recipient name" })).toBeTruthy();
  });
});
