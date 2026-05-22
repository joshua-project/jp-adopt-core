"use client";

import type { Dispatch, SetStateAction } from "react";

import { humanizeStatus, type StatusKind } from "../lib/vocab";

/**
 * Multi-select chip row used on /adopters and /facilitators to filter
 * by status. Each chip shows the human label + a live count badge.
 *
 * - When `selected` is empty, ALL statuses pass through (no filter).
 * - Clicking a chip toggles it in/out of the selected set.
 * - "Clear" wipes the selection back to "no filter."
 *
 * The unset key (`__unset__`) is rendered as "Unset" so contacts with
 * NULL status still get a visible bucket.
 */
export interface StatusFilterProps {
  /** Ordered list of statuses to render, in pipeline order. */
  statuses: readonly string[];
  /** Counts keyed by status (including `__unset__`). Missing = 0. */
  counts: Record<string, number>;
  /** Currently selected status set. */
  selected: ReadonlySet<string>;
  /** Setter for `selected`. */
  onChange: Dispatch<SetStateAction<Set<string>>>;
  /** Total across all statuses, shown when no filter is active. */
  total?: number;
  /** Which enum these statuses belong to. Drives the label table. */
  kind?: StatusKind;
}

export function StatusFilter({
  statuses,
  counts,
  selected,
  onChange,
  total,
  kind = "adopter",
}: StatusFilterProps) {
  const hasSelection = selected.size > 0;
  const toggleStatus = (s: string) => {
    onChange((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={() => onChange(new Set())}
        className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium tracking-wide transition ${
          hasSelection
            ? "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
            : "border-slate-900 bg-slate-900 text-white"
        }`}
      >
        All
        {typeof total === "number" ? (
          <span
            className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
              hasSelection
                ? "bg-slate-100 text-slate-700"
                : "bg-white/20 text-white"
            }`}
          >
            {total}
          </span>
        ) : null}
      </button>

      {statuses.map((status) => {
        const isSelected = selected.has(status);
        const count = counts[status] ?? 0;
        const label =
          status === "__unset__" ? "Unset" : humanizeStatus(status, kind);
        return (
          <button
            key={status}
            type="button"
            onClick={() => toggleStatus(status)}
            className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium tracking-wide transition ${
              isSelected
                ? "border-jp-accent bg-jp-accent text-white shadow-sm"
                : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50"
            }`}
            disabled={count === 0 && !isSelected}
            aria-pressed={isSelected}
          >
            {label}
            <span
              className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
                isSelected
                  ? "bg-white/20 text-white"
                  : "bg-slate-100 text-slate-700"
              }`}
            >
              {count}
            </span>
          </button>
        );
      })}
    </div>
  );
}
