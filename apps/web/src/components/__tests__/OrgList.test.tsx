/**
 * Tests for the admin facilitating-org list (U3). Focus: each row
 * surfaces the FPG coverage count as a meta chip so Amy can scan
 * facilitators without opening each org.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../../lib/useApiContext", () => ({
  useApiContext: () => ({ instance: null, accounts: [] }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const listAdminFacilitatingOrgs = vi.fn();

vi.mock("../../lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  createAdminFacilitatingOrg: vi.fn(),
  formatApiError: (e: unknown) => String(e),
  listAdminFacilitatingOrgs: (...args: unknown[]) =>
    listAdminFacilitatingOrgs(...args),
}));

import { OrgList } from "../OrgList";

type Org = {
  id: string;
  name: string;
  country_code: string | null;
  language_codes: string[] | null;
  capacity_total: number;
  capacity_committed: number;
  capacity_remaining: number;
  accepting_potential_adopters: boolean;
  is_triage_org: boolean;
  active: boolean;
  source_system: string | null;
  source_id: string | null;
  coverage_count: number;
  created_at: string;
  updated_at: string;
};

function org(overrides: Partial<Org>): Org {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    name: "Org",
    country_code: "US",
    language_codes: null,
    capacity_total: 10,
    capacity_committed: 2,
    capacity_remaining: 8,
    accepting_potential_adopters: false,
    is_triage_org: false,
    active: true,
    source_system: null,
    source_id: null,
    coverage_count: 0,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

describe("OrgList coverage count", () => {
  it("renders the coverage count chip for each org", async () => {
    listAdminFacilitatingOrgs.mockResolvedValueOnce({
      items: [
        org({
          id: "00000000-0000-0000-0000-000000000001",
          name: "Covered Org",
          coverage_count: 3,
        }),
      ],
      total: 1,
    });

    render(<OrgList />);

    await waitFor(() =>
      expect(screen.getByText("Covered Org")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Coverage: 3/)).toBeInTheDocument();
  });

  it("renders distinct coverage values for orgs with mixed counts", async () => {
    listAdminFacilitatingOrgs.mockResolvedValueOnce({
      items: [
        org({
          id: "00000000-0000-0000-0000-000000000001",
          name: "Covered Org",
          coverage_count: 5,
        }),
        org({
          id: "00000000-0000-0000-0000-000000000002",
          name: "Empty Org",
          coverage_count: 0,
        }),
      ],
      total: 2,
    });

    render(<OrgList />);

    await waitFor(() =>
      expect(screen.getByText("Covered Org")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Coverage: 5/)).toBeInTheDocument();
    expect(screen.getByText(/Coverage: 0/)).toBeInTheDocument();
  });
});
