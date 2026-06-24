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

/**
 * Status labels are kind-aware: an "adopter" with status `new` and a
 * "facilitator" with status `new` mean different things in context.
 * The default `humanizeStatus(value)` keeps a single source of truth
 * across surfaces (filter chips, row badges, kanban headers) and reads
 * as sentence case ("Reached out", not "Reached Out") to match modern
 * CRM convention. The all-caps treatment in some badges comes from
 * Tailwind `uppercase` on the wrapper, not from the label itself.
 */

const ADOPTER_STATUS_LABELS: Record<string, string> = {
  new: "New",
  potential_adopter: "Needs FPG selection",
  contacted: "Reached out",
  engaged: "In conversation",
  matched: "Matched",
  sent_back: "Returned to queue",
  active: "Active adoption",
  inactive: "Inactive",
  do_not_engage: "Opted out",
  draft: "Draft",
};

const FACILITATOR_STATUS_LABELS: Record<string, string> = {
  new: "New",
  not_ready: "Onboarding pending",
  ready: "Ready for matches",
  do_not_engage: "Paused",
  draft: "Draft",
};

const MATCH_STATUS_LABELS: Record<string, string> = {
  recommended: "Awaiting review",
  accepted: "Accepted",
  active: "In progress",
  triage: "Needs triage",
  declined: "Declined",
  sent_back: "Returned",
  matched: "Matched",
};

const CAMPAIGN_STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  active: "Active",
  paused: "Paused",
  archived: "Archived",
};

const REASON_CODE_LABELS: Record<string, string> = {
  capacity_full: "Facilitator at capacity",
  geography_mismatch: "Geography mismatch",
  language: "Language mismatch",
  theological_concern: "Theological concern",
  not_ready: "Adopter not ready",
  other: "Other (see notes)",
};

export type StatusKind = "adopter" | "facilitator" | "match" | "campaign";

/** Look up a status label by kind. Falls back to `humanize` for unknowns. */
export function humanizeStatus(
  value: string | null | undefined,
  kind: StatusKind = "adopter",
): string {
  if (!value) return "—";
  const table =
    kind === "facilitator"
      ? FACILITATOR_STATUS_LABELS
      : kind === "match"
        ? MATCH_STATUS_LABELS
        : kind === "campaign"
          ? CAMPAIGN_STATUS_LABELS
          : ADOPTER_STATUS_LABELS;
  return table[value] ?? humanize(value);
}

/** Look up a send-back reason code label. */
export function humanizeReasonCode(value: string | null | undefined): string {
  if (!value) return "—";
  return REASON_CODE_LABELS[value] ?? humanize(value);
}

const ENROLL_REASON_LABELS: Record<string, string> = {
  created: "Enrolled",
  already_enrolled: "Contact is already enrolled in this campaign.",
  suppressed: "Contact email is on the suppression list.",
  do_not_engage: "Contact is opted out — cannot enroll.",
  no_campaign: "Campaign not found or not active.",
  no_contact: "Contact not found.",
};

export function humanizeEnrollReason(value: string): string {
  return ENROLL_REASON_LABELS[value] ?? humanize(value);
}

/**
 * Labels for the "Remove contact" affordance (Amy contact-management).
 * Spam → permanent hard-delete; hostile → mark do_not_engage. Kept here so
 * the destructive copy stays out of the component and reads consistently.
 */
const REMOVE_CONTACT_LABELS = {
  trigger: "Remove contact",
  spam: "Spam — delete permanently",
  hostile: "Hostile — do not engage",
  spamConfirm:
    "Permanently delete this contact? This removes all of their data and cannot be undone.",
  hostileConfirm:
    "Mark this contact as do-not-engage? They will be kept but flagged and excluded from outreach.",
} as const;

export function removeContactLabel(
  key: keyof typeof REMOVE_CONTACT_LABELS,
): string {
  return REMOVE_CONTACT_LABELS[key];
}

export function humanize(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
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
