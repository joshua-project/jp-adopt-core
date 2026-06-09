import { describe, expect, it } from "vitest";

import {
  formatDate,
  formatTimestamp,
  humanize,
  humanizeEnrollReason,
  humanizeOrigin,
  humanizePartyKind,
  humanizeReasonCode,
  humanizeStatus,
} from "../vocab";

describe("humanize", () => {
  it("replaces underscores and capitalizes", () => {
    expect(humanize("send_back_to_queue")).toBe("Send back to queue");
  });

  it("handles single-word input", () => {
    expect(humanize("matched")).toBe("Matched");
  });

  it("returns empty string for empty input", () => {
    expect(humanize("")).toBe("");
  });
});

describe("humanizeStatus", () => {
  it("defaults to the adopter table when no kind is given", () => {
    expect(humanizeStatus("matched")).toBe("Matched");
    expect(humanizeStatus("potential_adopter")).toBe("Needs FPG selection");
  });

  it("dispatches to the facilitator table when kind=facilitator", () => {
    expect(humanizeStatus("ready", "facilitator")).toBe("Ready for matches");
    expect(humanizeStatus("not_ready", "facilitator")).toBe("Onboarding pending");
  });

  it("dispatches to the match table when kind=match", () => {
    expect(humanizeStatus("recommended", "match")).toBe("Awaiting review");
  });

  it("dispatches to the campaign table when kind=campaign", () => {
    expect(humanizeStatus("draft", "campaign")).toBe("Draft");
    expect(humanizeStatus("active", "campaign")).toBe("Active");
    expect(humanizeStatus("paused", "campaign")).toBe("Paused");
    expect(humanizeStatus("archived", "campaign")).toBe("Archived");
  });

  it("renders do_not_engage with a human label (not a mechanical humanize)", () => {
    // AGENTS.md convention: do_not_engage gets an explicit label per kind.
    expect(humanizeStatus("do_not_engage", "adopter")).toBe("Opted out");
    expect(humanizeStatus("do_not_engage", "facilitator")).toBe("Paused");
  });

  it("falls back to humanize for unknown values", () => {
    expect(humanizeStatus("never_seen_before", "adopter")).toBe("Never seen before");
  });

  it("returns dash for null / undefined / empty", () => {
    expect(humanizeStatus(null)).toBe("—");
    expect(humanizeStatus(undefined)).toBe("—");
    expect(humanizeStatus("")).toBe("—");
  });
});

describe("humanizeReasonCode", () => {
  it("returns the documented label for known codes", () => {
    expect(humanizeReasonCode("theological_concern")).toBe("Theological concern");
    expect(humanizeReasonCode("other")).toBe("Other (see notes)");
  });

  it("falls back to humanize for unknown codes", () => {
    expect(humanizeReasonCode("brand_new_code")).toBe("Brand new code");
  });

  it("returns dash for null / undefined", () => {
    expect(humanizeReasonCode(null)).toBe("—");
    expect(humanizeReasonCode(undefined)).toBe("—");
  });
});

describe("humanizeEnrollReason", () => {
  it("returns the documented label for known reasons", () => {
    expect(humanizeEnrollReason("created")).toBe("Enrolled");
    expect(humanizeEnrollReason("already_enrolled")).toBe(
      "Contact is already enrolled in this campaign.",
    );
    expect(humanizeEnrollReason("suppressed")).toBe(
      "Contact email is on the suppression list.",
    );
    expect(humanizeEnrollReason("do_not_engage")).toBe(
      "Contact is opted out — cannot enroll.",
    );
  });

  it("falls back to humanize for an unexpected reason", () => {
    expect(humanizeEnrollReason("brand_new_reason")).toBe("Brand new reason");
  });
});

describe("humanizeOrigin", () => {
  it("returns the documented label for known origins", () => {
    expect(humanizeOrigin("manual_entry")).toBe(
      "Manual entry (walk-in / phone / referral)",
    );
    expect(humanizeOrigin("website")).toBe("Public website form");
  });

  it("returns dash for null", () => {
    expect(humanizeOrigin(null)).toBe("—");
  });
});

describe("humanizePartyKind", () => {
  it("returns the documented labels", () => {
    expect(humanizePartyKind("adopter")).toBe("Adopter");
    expect(humanizePartyKind("facilitator")).toBe("Facilitator");
  });

  it("returns dash for null", () => {
    expect(humanizePartyKind(null)).toBe("—");
  });
});

describe("formatTimestamp", () => {
  it("returns dash for null / undefined / empty", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp(undefined)).toBe("—");
    expect(formatTimestamp("")).toBe("—");
  });

  it("returns dash for an unparseable string", () => {
    expect(formatTimestamp("not-a-date")).toBe("—");
  });

  it("renders a valid ISO timestamp as a non-empty string", () => {
    // Locale-dependent — we only assert it's not the dash fallback.
    const out = formatTimestamp("2026-06-08T15:30:00Z");
    expect(out).not.toBe("—");
    expect(out.length).toBeGreaterThan(0);
  });
});

describe("formatDate", () => {
  it("returns dash for null / undefined", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate(undefined)).toBe("—");
  });

  it("returns dash for an unparseable string", () => {
    expect(formatDate("not-a-date")).toBe("—");
  });

  it("renders a valid ISO date as a non-empty string", () => {
    const out = formatDate("2026-06-08T15:30:00Z");
    expect(out).not.toBe("—");
    expect(out.length).toBeGreaterThan(0);
  });
});
