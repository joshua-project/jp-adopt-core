import type { StatusKind } from "./vocab";

export interface ContactStatusInput {
  party_kind: string;
  adopter_status?: string | null;
  facilitator_status?: string | null;
}

/**
 * Pick the badge to show for a contact, keyed strictly by party_kind.
 * adopter_status and facilitator_status are disjoint enums — a facilitator
 * must never render its (stray) adopter_status, or you get nonsense like a
 * "Matched" pill on an org. Returns null when the relevant status is unset.
 */
export function contactStatusBadge(
  c: ContactStatusInput,
): { status: string; kind: StatusKind } | null {
  if (c.party_kind === "facilitator") {
    return c.facilitator_status
      ? { status: c.facilitator_status, kind: "facilitator" }
      : null;
  }
  if (c.party_kind === "adopter") {
    return c.adopter_status
      ? { status: c.adopter_status, kind: "adopter" }
      : null;
  }
  return null;
}
