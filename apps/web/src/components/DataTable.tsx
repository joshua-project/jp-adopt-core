"use client";

import type { ReactNode } from "react";

/**
 * Shared list-table primitive used by the match queue, contacts list, and
 * facilitator portal. We deliberately keep this as a *list of rows* rather
 * than a true <table>: every record has a name + status + a couple of
 * metadata bullets + a navigation link, and the row layout reads well on
 * narrow viewports without horizontal scrolling.
 *
 * The semantic role is preserved with role="list" + role="listitem" so screen
 * readers announce it as a list, and `tabIndex={0}` on rows makes them
 * keyboard-focusable for the linked review action.
 */
export interface DataRowProps {
  id: string;
  href?: string;
  title: ReactNode;
  /** Right-side primary badge / status pill. */
  badge?: ReactNode;
  /** Single line of inline metadata under the title (code chips, scores, etc.). */
  meta?: ReactNode;
  /** Smaller, dimmer secondary line — typically a timestamp. */
  subtle?: ReactNode;
  /** Optional trailing action (button, link). */
  action?: ReactNode;
}

export function DataTable({
  rows,
  empty,
  caption,
}: {
  rows: ReactNode;
  empty?: ReactNode;
  caption?: ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      {caption ? (
        <div className="border-b border-slate-200 bg-slate-50/70 px-4 py-2 text-xs font-medium uppercase tracking-wide text-slate-500">
          {caption}
        </div>
      ) : null}
      {rows ? (
        <ul role="list" className="divide-y divide-slate-100">
          {rows}
        </ul>
      ) : (
        empty
      )}
    </div>
  );
}

export function DataRow({
  href,
  title,
  badge,
  meta,
  subtle,
  action,
}: DataRowProps) {
  const body = (
    <div className="flex flex-col gap-1 px-4 py-3 transition-colors hover:bg-slate-50">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1 font-medium text-slate-900">
          {title}
        </div>
        {badge ? <div className="shrink-0">{badge}</div> : null}
      </div>
      {meta ? (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
          {meta}
        </div>
      ) : null}
      <div className="flex items-end justify-between gap-3">
        {subtle ? (
          <div className="text-[11px] text-slate-400">{subtle}</div>
        ) : (
          <div />
        )}
        {action ? <div className="shrink-0 text-xs">{action}</div> : null}
      </div>
    </div>
  );
  // A linked row sets the whole row as the navigation target; a non-linked
  // row falls back to a plain list item.
  if (href) {
    return (
      <li role="listitem">
        <a
          href={href}
          className="block focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-orange-500"
        >
          {body}
        </a>
      </li>
    );
  }
  return <li role="listitem">{body}</li>;
}

export function EmptyState({
  title,
  description,
}: {
  title: string;
  description?: ReactNode;
}) {
  return (
    <div className="px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {description ? (
        <p className="mt-1 text-xs text-slate-500">{description}</p>
      ) : null}
    </div>
  );
}

export function LoadingRows({ count = 3 }: { count?: number }) {
  return (
    <ul role="list" className="divide-y divide-slate-100">
      {Array.from({ length: count }).map((_, i) => (
        <li key={i} className="px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="h-4 w-40 animate-pulse rounded bg-slate-200" />
            <div className="h-4 w-16 animate-pulse rounded-full bg-slate-200" />
          </div>
          <div className="mt-2 h-3 w-3/4 animate-pulse rounded bg-slate-100" />
          <div className="mt-2 h-3 w-1/3 animate-pulse rounded bg-slate-100" />
        </li>
      ))}
    </ul>
  );
}
