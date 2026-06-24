/**
 * Tests for the step body editor (U5): editing a step swaps the template
 * dropdown for a rich-text body editor, saves body_html, and exposes a
 * "Send test" action. The Tiptap editor itself is mocked to a textarea so the
 * form logic is tested without ProseMirror's DOM dependencies.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("../../lib/useApiContext", () => ({
  useApiContext: () => ({ instance: null, accounts: [] }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

// Mock the dynamic Tiptap editor with a plain textarea that speaks the same
// value/onChange contract.
vi.mock("../editor/RichTextEditor", () => ({
  RichTextEditor: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (html: string) => void;
  }) => (
    <textarea
      aria-label="body editor"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

const getCampaign = vi.fn();
const patchCampaignStep = vi.fn();
const sendTestStep = vi.fn();
const listMergeTokens = vi.fn();

vi.mock("../../lib/api-client", () => ({
  ApiError: class ApiError extends Error {},
  formatApiError: (e: unknown) => String(e),
  getCampaign: (...a: unknown[]) => getCampaign(...a),
  patchCampaignStep: (...a: unknown[]) => patchCampaignStep(...a),
  sendTestStep: (...a: unknown[]) => sendTestStep(...a),
  listMergeTokens: (...a: unknown[]) => listMergeTokens(...a),
  activateCampaign: vi.fn(),
  archiveCampaign: vi.fn(),
  deleteCampaignStep: vi.fn(),
  patchCampaign: vi.fn(),
  pauseCampaign: vi.fn(),
  previewCampaignStep: vi.fn(),
  // AddCampaignStepForm (rendered in the same tree) calls addCampaignStep;
  // listMergeTokens is mocked above.
  addCampaignStep: vi.fn(),
}));

import { CampaignDetail } from "../CampaignDetail";

function campaignWithStep() {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Welcome",
    description: null,
    status: "draft",
    trigger_type: "manual",
    trigger_event_type: null,
    auto_enroll_existing: false,
    precedence: 0,
    version: 1,
    created_at: "2026-06-24T00:00:00Z",
    updated_at: "2026-06-24T00:00:00Z",
    steps: [
      {
        id: "22222222-2222-2222-2222-222222222222",
        campaign_id: "11111111-1111-1111-1111-111111111111",
        position: 0,
        delay_days: 0,
        mjml_template_name: null,
        body_html: "<p>Hi {{ contact_display_name }}</p>",
        subject: "Welcome",
        send_at_hour: 9,
        send_at_minute: 0,
        created_at: "2026-06-24T00:00:00Z",
      },
    ],
  };
}

describe("CampaignDetail step body editor", () => {
  it("saves body_html from the editor", async () => {
    getCampaign.mockResolvedValue(campaignWithStep());
    listMergeTokens.mockResolvedValue({
      items: [{ name: "contact_display_name", label: "Recipient name" }],
    });
    patchCampaignStep.mockResolvedValue({});

    render(<CampaignDetail campaignId="11111111-1111-1111-1111-111111111111" />);

    const user = userEvent.setup();
    // Two "Edit" buttons exist (campaign meta + step row); the step's is last.
    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[editButtons.length - 1]!);

    const editor = await screen.findByLabelText("body editor");
    await user.clear(editor);
    await user.type(editor, "<p>New body</p>");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchCampaignStep).toHaveBeenCalled());
    const [, , position, body] = patchCampaignStep.mock.calls[0];
    expect(position).toBe(0);
    expect(body.body_html).toBe("<p>New body</p>");
  });

  it("sends a test of the step", async () => {
    getCampaign.mockResolvedValue(campaignWithStep());
    listMergeTokens.mockResolvedValue({ items: [] });
    sendTestStep.mockResolvedValue({
      to_email: "amy@example.com",
      delivered: true,
    });

    render(<CampaignDetail campaignId="11111111-1111-1111-1111-111111111111" />);

    const user = userEvent.setup();
    // Two "Edit" buttons exist (campaign meta + step row); the step's is last.
    const editButtons = await screen.findAllByRole("button", { name: "Edit" });
    await user.click(editButtons[editButtons.length - 1]!);
    await user.click(
      await screen.findByRole("button", { name: "Send test to me" }),
    );

    await waitFor(() => expect(sendTestStep).toHaveBeenCalled());
    expect(await screen.findByText(/Test sent to amy@example.com/)).toBeTruthy();
  });
});
