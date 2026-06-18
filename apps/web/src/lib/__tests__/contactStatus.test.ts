import { describe, expect, it } from "vitest";

import { contactStatusBadge } from "../contactStatus";

describe("contactStatusBadge", () => {
  it("never leaks adopter_status onto a facilitator", () => {
    expect(
      contactStatusBadge({
        party_kind: "facilitator",
        facilitator_status: null,
        adopter_status: "matched",
      }),
    ).toBeNull();
  });

  it("shows facilitator_status for a facilitator", () => {
    expect(
      contactStatusBadge({
        party_kind: "facilitator",
        facilitator_status: "ready",
        adopter_status: null,
      }),
    ).toEqual({ status: "ready", kind: "facilitator" });
  });

  it("shows adopter_status for an adopter", () => {
    expect(
      contactStatusBadge({
        party_kind: "adopter",
        facilitator_status: null,
        adopter_status: "matched",
      }),
    ).toEqual({ status: "matched", kind: "adopter" });
  });

  it("returns null when the relevant status is unset", () => {
    expect(
      contactStatusBadge({
        party_kind: "adopter",
        facilitator_status: "ready",
        adopter_status: null,
      }),
    ).toBeNull();
  });

  it("returns null for an unknown party_kind (never falls back to a status)", () => {
    expect(
      contactStatusBadge({
        party_kind: "",
        facilitator_status: "ready",
        adopter_status: "matched",
      }),
    ).toBeNull();
  });
});
