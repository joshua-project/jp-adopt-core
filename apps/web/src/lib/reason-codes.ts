import type { paths } from "@jp-adopt/contracts";

/**
 * F8: shared reason-code source of truth. The backend enum is the actual
 * source — we tie the type to the generated paths so a backend rename
 * shows up here as a type error, not as a silently-divergent literal list.
 */
export type ReasonCode = NonNullable<
  paths["/v1/matches/{match_id}/decide"]["post"]["requestBody"]["content"]["application/json"]["reason_code"]
>;

export const REASON_CODES: readonly ReasonCode[] = [
  "capacity_full",
  "geography_mismatch",
  "language",
  "theological_concern",
  "not_ready",
  "other",
] as const;

/** Type guard for runtime narrowing from arbitrary string input. */
export function isReasonCode(value: string): value is ReasonCode {
  return (REASON_CODES as readonly string[]).includes(value);
}
