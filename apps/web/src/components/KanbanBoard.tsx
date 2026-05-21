"use client";

import type { ReactNode } from "react";

import { humanizeStatus } from "../lib/vocab";

/**
 * Horizontal-scroll kanban: one column per status, cards inside each
 * column for the rows in that status. The board respects the order of
 * `statuses` (so columns line up with the pipeline funnel).
 *
 * Empty columns still render so the user can see the full pipeline
 * shape — useful when a filter is hiding everything in a stage and
 * Amy wants to know "where did all the matched ones go?"
 */
export interface KanbanBoardProps<T> {
  /** Status order — left to right. */
  statuses: readonly string[];
  /** All items to bucket; each must expose the field used as the column key. */
  items: readonly T[];
  /** Pull the bucket key off an item (e.g. r => r.adopter_status). */
  getStatus: (item: T) => string | null | undefined;
  /** Counts to badge each column header (uses `__unset__` for nulls). */
  counts?: Record<string, number>;
  /** Render one card; receives the item. */
  renderCard: (item: T) => ReactNode;
  /** Optional tone hint per status — defaults to slate. */
  toneFor?: (status: string) => "green" | "amber" | "slate" | "rose" | "teal";
}

const TONE_BORDER: Record<string, string> = {
  green: "border-l-emerald-400",
  amber: "border-l-amber-400",
  slate: "border-l-slate-300",
  rose: "border-l-rose-400",
  teal: "border-l-teal-400",
};

export function KanbanBoard<T>({
  statuses,
  items,
  getStatus,
  counts,
  renderCard,
  toneFor,
}: KanbanBoardProps<T>) {
  // Bucket items by status. Nulls go to `__unset__`.
  const buckets: Record<string, T[]> = {};
  for (const s of statuses) buckets[s] = [];
  for (const item of items) {
    const raw = getStatus(item) ?? "__unset__";
    if (!buckets[raw]) buckets[raw] = [];
    buckets[raw].push(item);
  }

  return (
    <div className="overflow-x-auto pb-3">
      <div
        className="flex gap-3"
        style={{ minWidth: `${statuses.length * 280}px` }}
      >
        {statuses.map((status) => {
          const bucket = buckets[status] ?? [];
          const tone = toneFor?.(status) ?? "slate";
          const label =
            status === "__unset__" ? "Unset" : humanizeStatus(status);
          const count = counts?.[status] ?? bucket.length;
          return (
            <div
              key={status}
              className={`flex w-[260px] shrink-0 flex-col rounded-md border border-slate-200 bg-slate-50/60 ${
                TONE_BORDER[tone] ?? TONE_BORDER.slate
              } border-l-4`}
            >
              <div className="flex items-baseline justify-between gap-2 px-3 py-2 border-b border-slate-200/80">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-700">
                  {label}
                </h3>
                <span className="rounded-full bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-600">
                  {count}
                </span>
              </div>
              <div className="flex flex-1 flex-col gap-2 p-2">
                {bucket.length === 0 ? (
                  <div className="rounded border border-dashed border-slate-200 px-3 py-4 text-center text-[11px] text-slate-400">
                    Empty
                  </div>
                ) : (
                  bucket.map((item, i) => (
                    <div key={i}>{renderCard(item)}</div>
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
