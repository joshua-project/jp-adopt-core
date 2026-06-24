/**
 * Tests for the pipeline-view search box (U2). Focus: the debounced
 * ``q`` search input lands on PipelineView, drives ``GET /v1/contacts``
 * with a ``q`` param, resets paging to offset 0, clears back to the
 * unfiltered list, and aborts an in-flight request on a fresh query so
 * stale results never overwrite a newer one.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/useApiContext", () => ({
  useApiContext: () => ({ instance: null, accounts: [] }),
}));

const apiFetch = vi.fn();

vi.mock("../../lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}));

import { PipelineView } from "../PipelineView";

const STATUSES = ["new", "contacted", "matched"] as const;

type ContactRow = {
  id: string;
  display_name: string;
  email_normalized: string | null;
  country_code: string | null;
  adopter_status: string | null;
  facilitator_status: string | null;
  created_at: string | null;
};

function contact(overrides: Partial<ContactRow>): ContactRow {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    display_name: "Someone",
    email_normalized: "someone@example.com",
    country_code: "US",
    adopter_status: "new",
    facilitator_status: null,
    created_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

/**
 * apiFetch is called twice per fetchData: the contacts list and the
 * status_counts. Route by path so each resolves with the right shape.
 */
function routeApiFetch(items: ContactRow[]) {
  apiFetch.mockImplementation(async (_ctx: unknown, path: string) => {
    if (path.startsWith("/v1/contacts/status_counts")) {
      return { counts: {}, total: items.length };
    }
    return { items, total: items.length };
  });
}

/** Pull the contacts-list URL out of the most recent apiFetch calls. */
function lastListUrl(): string {
  const call = apiFetch.mock.calls
    .filter(
      (c) =>
        typeof c[1] === "string" &&
        c[1].startsWith("/v1/contacts?"),
    )
    .at(-1);
  return (call?.[1] as string) ?? "";
}

describe("PipelineView search", () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  it("debounces typing into the q param (not per keystroke)", async () => {
    routeApiFetch([contact({ display_name: "Jane Doe" })]);
    render(
      <PipelineView
        partyKind="adopter"
        title="Adopters"
        subtitle="sub"
        statuses={STATUSES}
        searchDebounceMs={0}
      />,
    );

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    // Initial (unfiltered) load has no q.
    expect(lastListUrl()).not.toContain("q=");

    await userEvent.type(screen.getByRole("searchbox"), "jane");

    await waitFor(() => expect(lastListUrl()).toContain("q=jane"));
  });

  it("resets paging to offset 0 on a new query", async () => {
    routeApiFetch([contact({ display_name: "Jane Doe" })]);
    render(
      <PipelineView
        partyKind="adopter"
        title="Adopters"
        subtitle="sub"
        statuses={STATUSES}
        searchDebounceMs={0}
      />,
    );

    await userEvent.type(screen.getByRole("searchbox"), "jane");

    await waitFor(() => expect(lastListUrl()).toContain("q=jane"));
    expect(lastListUrl()).toContain("offset=0");
  });

  it("clearing the box returns to the unfiltered list", async () => {
    routeApiFetch([contact({ display_name: "Jane Doe" })]);
    render(
      <PipelineView
        partyKind="adopter"
        title="Adopters"
        subtitle="sub"
        statuses={STATUSES}
        searchDebounceMs={0}
      />,
    );

    const box = screen.getByRole("searchbox");
    await userEvent.type(box, "jane");
    await waitFor(() => expect(lastListUrl()).toContain("q=jane"));

    await userEvent.clear(box);
    await waitFor(() => expect(lastListUrl()).not.toContain("q="));
  });

  it("aborts the in-flight request when the query changes", async () => {
    const signals: AbortSignal[] = [];
    apiFetch.mockImplementation(
      async (
        _ctx: unknown,
        path: string,
        init?: { signal?: AbortSignal },
      ) => {
        if (path.startsWith("/v1/contacts?") && init?.signal) {
          signals.push(init.signal);
        }
        if (path.startsWith("/v1/contacts/status_counts")) {
          return { counts: {}, total: 0 };
        }
        return { items: [], total: 0 };
      },
    );

    render(
      <PipelineView
        partyKind="adopter"
        title="Adopters"
        subtitle="sub"
        statuses={STATUSES}
        searchDebounceMs={0}
      />,
    );

    await waitFor(() => expect(signals.length).toBeGreaterThan(0));
    const firstSignal = signals[0];

    await userEvent.type(screen.getByRole("searchbox"), "jane");

    // A fresh query supersedes the prior fetch; its signal must abort.
    await waitFor(() => expect(firstSignal.aborted).toBe(true));
  });
});
