/**
 * Tests for the ContactRecord "Remove contact" affordance (U7).
 *
 * Two confirmed destructive paths:
 *   - Spam → DELETE /v1/contacts/{id} via deleteContact, then route away.
 *   - Hostile → POST transition to do_not_engage via transitionContact.
 * Canceling the confirm makes no request; a failed delete keeps the record
 * visible and surfaces the error.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/useApiContext", () => ({
  useApiContext: () => ({ instance: null, accounts: [] }),
}));

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

// load() fans out to five apiFetch calls (contact, matches, transitions,
// activity, enrollments) keyed by path suffix.
const apiFetch = vi.fn(async (_ctx: unknown, path: string) => {
  if (path.endsWith("/matches")) return { items: [], total: 0 };
  if (path.endsWith("/transitions")) return { items: [], total: 0 };
  if (path.endsWith("/activity")) return { items: [], total: 0 };
  if (path.endsWith("/enrollments")) return { items: [], total: 0 };
  // base contact fetch
  return {
    id: "c-1",
    display_name: "Jane Spam",
    party_kind: "adopter",
    adopter_status: "new",
    facilitator_status: null,
    email_normalized: "jane@example.com",
    country_code: null,
    language_codes: null,
    origin: "website",
    assigned_to: null,
    updated_at: "2026-06-01T00:00:00Z",
    profile: null,
  };
});

const deleteContact = vi.fn();
const transitionContact = vi.fn();

vi.mock("../../lib/api-client", () => ({
  apiFetch: (...args: unknown[]) =>
    (apiFetch as (...a: unknown[]) => unknown)(...args),
  deleteContact: (...args: unknown[]) =>
    (deleteContact as (...a: unknown[]) => unknown)(...args),
  transitionContact: (...args: unknown[]) =>
    (transitionContact as (...a: unknown[]) => unknown)(...args),
  enrollInCampaign: vi.fn(),
  listCampaigns: vi.fn(async () => ({ items: [] })),
  sendContactEmail: vi.fn(),
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

import { ContactRecord } from "../ContactRecord";

async function renderLoaded() {
  render(<ContactRecord contactId="c-1" />);
  await waitFor(() =>
    expect(screen.getByText("Jane Spam")).toBeInTheDocument(),
  );
}

async function openRemoveMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Remove contact" }));
}

describe("ContactRecord remove affordance", () => {
  beforeEach(() => {
    push.mockReset();
    deleteContact.mockReset();
    transitionContact.mockReset();
    apiFetch.mockClear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("spam path: confirm → deleteContact → routes away", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    deleteContact.mockResolvedValueOnce(undefined);

    await renderLoaded();
    await openRemoveMenu(user);
    await user.click(
      screen.getByRole("button", { name: "Spam — delete permanently" }),
    );

    await waitFor(() => expect(deleteContact).toHaveBeenCalledTimes(1));
    expect(deleteContact).toHaveBeenCalledWith(expect.anything(), "c-1");
    expect(push).toHaveBeenCalledWith("/contacts");
    expect(transitionContact).not.toHaveBeenCalled();
  });

  it("hostile path: confirm → transition to do_not_engage", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    transitionContact.mockResolvedValueOnce({});

    await renderLoaded();
    await openRemoveMenu(user);
    await user.click(
      screen.getByRole("button", { name: "Hostile — do not engage" }),
    );

    await waitFor(() => expect(transitionContact).toHaveBeenCalledTimes(1));
    expect(transitionContact).toHaveBeenCalledWith(
      expect.anything(),
      "c-1",
      expect.objectContaining({
        kind: "adopter",
        to_state: "do_not_engage",
        reason_code: "other",
      }),
    );
    expect(deleteContact).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("canceling the confirm makes no request", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await renderLoaded();
    await openRemoveMenu(user);
    await user.click(
      screen.getByRole("button", { name: "Spam — delete permanently" }),
    );

    expect(deleteContact).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("a failed delete surfaces an error and leaves the record visible", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    deleteContact.mockRejectedValueOnce(new Error("boom"));

    await renderLoaded();
    await openRemoveMenu(user);
    await user.click(
      screen.getByRole("button", { name: "Spam — delete permanently" }),
    );

    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument());
    expect(push).not.toHaveBeenCalled();
    // Record still on screen.
    expect(screen.getByText("Jane Spam")).toBeInTheDocument();
  });
});
