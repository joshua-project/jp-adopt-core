"use client";

import type { ReactNode } from "react";

/**
 * Semantic status palette.
 * - green  = recommended / accepted / active / ready / matched
 * - amber  = triage / engaged / contacted / sent_back
 * - slate  = neutral defaults (draft, new, unknown)
 * - rose   = declined / do_not_engage / inactive
 */
type Tone = "green" | "amber" | "slate" | "rose" | "teal";

const STATUS_TONE: Record<string, Tone> = {
  recommended: "green",
  accepted: "green",
  active: "green",
  ready: "green",
  matched: "green",
  potential_adopter: "green",
  triage: "amber",
  engaged: "amber",
  contacted: "amber",
  sent_back: "amber",
  not_ready: "amber",
  new: "slate",
  draft: "slate",
  declined: "rose",
  do_not_engage: "rose",
  inactive: "rose",
};

const TONE_CLASS: Record<Tone, string> = {
  green:
    "bg-emerald-50 text-emerald-800 border-emerald-200",
  amber:
    "bg-amber-50 text-amber-900 border-amber-200",
  slate:
    "bg-slate-100 text-slate-700 border-slate-200",
  rose:
    "bg-rose-50 text-rose-800 border-rose-200",
  teal:
    "bg-teal-50 text-teal-800 border-teal-200",
};

export function StatusBadge({
  status,
  tone,
  children,
}: {
  status?: string;
  tone?: Tone;
  children?: ReactNode;
}) {
  const resolvedTone: Tone =
    tone ?? (status ? STATUS_TONE[status] ?? "slate" : "slate");
  const label = children ?? humanizeStatus(status ?? "");
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${TONE_CLASS[resolvedTone]}`}
    >
      {label}
    </span>
  );
}

/** Render a code-style chip — used for FPG codes and other short identifiers. */
export function CodeChip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
      {children}
    </span>
  );
}

function humanizeStatus(s: string): string {
  if (!s) return "";
  return s
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
