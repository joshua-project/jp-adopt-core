/**
 * Vocabulary helpers — keep engineering terms out of the staff-facing UI.
 *
 * These map enum-shaped backend values into human phrases. Wherever the staff
 * needs to *see* the underlying code (e.g. for cross-referencing in Joshua
 * Project's public data) the raw value is rendered as a CodeChip alongside the
 * humanized label, not bare.
 */

const ORIGIN_LABELS: Record<string, string> = {
  manual_entry: "Manual entry (walk-in / phone / referral)",
  core_org: "Core org",
  website: "Public website form",
  third_party_referral: "Third-party referral",
  partner_event: "Partner event",
  other: "Other",
};

export function humanizeOrigin(value: string | null | undefined): string {
  if (!value) return "—";
  return ORIGIN_LABELS[value] ?? humanize(value);
}

export const ORIGIN_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "manual_entry", label: ORIGIN_LABELS.manual_entry },
  { value: "core_org", label: ORIGIN_LABELS.core_org },
  { value: "website", label: ORIGIN_LABELS.website },
  { value: "third_party_referral", label: ORIGIN_LABELS.third_party_referral },
  { value: "partner_event", label: ORIGIN_LABELS.partner_event },
  { value: "other", label: ORIGIN_LABELS.other },
];

const PARTY_KIND_LABELS: Record<string, string> = {
  adopter: "Adopter",
  facilitator: "Facilitator",
};

export function humanizePartyKind(value: string | null | undefined): string {
  if (!value) return "—";
  return PARTY_KIND_LABELS[value] ?? humanize(value);
}

export const PARTY_KIND_OPTIONS: ReadonlyArray<{ value: "adopter" | "facilitator"; label: string }> = [
  { value: "adopter", label: PARTY_KIND_LABELS.adopter },
  { value: "facilitator", label: PARTY_KIND_LABELS.facilitator },
];

export function humanizeStatus(value: string | null | undefined): string {
  if (!value) return "—";
  return humanize(value);
}

export function humanize(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Format an ISO timestamp as a compact local string. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { dateStyle: "medium" });
}
