"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, apiFetch } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import { formatTimestamp, humanizeStatus } from "../lib/vocab";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";
import { KanbanBoard } from "./KanbanBoard";
import { StatusBadge } from "./StatusBadge";
import { StatusFilter } from "./StatusFilter";
import { ViewToggle, type ViewMode } from "./ViewToggle";

type ContactsResponse =
  paths["/v1/contacts"]["get"]["responses"]["200"]["content"]["application/json"];
type StatusCountsResponse =
  paths["/v1/contacts/status_counts"]["get"]["responses"]["200"]["content"]["application/json"];
type ContactRow = ContactsResponse["items"][number];

export type PartyKind = "adopter" | "facilitator";

export interface PipelineViewProps {
  partyKind: PartyKind;
  title: string;
  subtitle: string;
  /** Status enum order used for filter chips + kanban columns. */
  statuses: readonly string[];
  /** Optional empty-state copy when there's nothing at all. */
  emptyTitle?: string;
  emptyBody?: string;
}

const STATUS_TONE_DEFAULT: Record<
  string,
  "green" | "amber" | "slate" | "rose" | "teal"
> = {
  matched: "green",
  active: "green",
  ready: "green",
  contacted: "amber",
  engaged: "amber",
  sent_back: "amber",
  not_ready: "amber",
  potential_adopter: "amber",
  inactive: "rose",
  do_not_engage: "rose",
  declined: "rose",
  new: "slate",
  draft: "slate",
};

/**
 * Shared pipeline UI used by /adopters and /facilitators.
 *
 * Three controls on top: party-restricted status filter chips (with
 * live counts), a table/kanban view toggle, and an implicit refresh
 * that runs whenever the filter changes.
 *
 * Status filters are sent to the API as repeated query params; the
 * kanban view always shows ALL columns the user is filtering to, so
 * a column with zero rows still appears as "Empty" — that's a feature,
 * not a bug, since it lets staff see pipeline shape at a glance.
 */
export function PipelineView({
  partyKind,
  title,
  subtitle,
  statuses,
  emptyTitle,
  emptyBody,
}: PipelineViewProps) {
  const ctx = useApiContext();

  const [items, setItems] = useState<ContactRow[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [countsTotal, setCountsTotal] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [view, setView] = useState<ViewMode>("table");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const statusParam =
    partyKind === "adopter" ? "adopter_status" : "facilitator_status";

  // Stable string for the dependency array so React doesn't re-fetch on
  // every re-render just because Set identity changed.
  const selectedKey = useMemo(
    () =>
      Array.from(selected)
        .sort()
        .join(","),
    [selected],
  );

  const fetchData = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const qs = new URLSearchParams();
      qs.set("party_kind", partyKind);
      qs.set("limit", "200");
      for (const s of selected) {
        if (s === "__unset__") continue; // server can't filter NULLs yet
        qs.append(statusParam, s);
      }
      const [listResp, countsResp] = await Promise.all([
        apiFetch<ContactsResponse>(ctx, `/v1/contacts?${qs.toString()}`),
        apiFetch<StatusCountsResponse>(
          ctx,
          `/v1/contacts/status_counts?party_kind=${partyKind}`,
        ),
      ]);
      if (listResp) {
        setItems(listResp.items);
        setTotal(listResp.total);
      }
      if (countsResp) {
        setCounts(countsResp.counts);
        setCountsTotal(countsResp.total);
      }
    } catch (e) {
      setErr(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "Failed to load",
      );
    } finally {
      setLoading(false);
    }
  }, [ctx, partyKind, selectedKey, statusParam]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void fetchData();
  }, [fetchData]);

  const statusField =
    partyKind === "adopter" ? "adopter_status" : "facilitator_status";
  const toneFor = (s: string) =>
    s === "__unset__" ? "slate" : STATUS_TONE_DEFAULT[s] ?? "slate";

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-heading text-3xl font-bold text-slate-900">
            {title}
          </h1>
          <p className="mt-1 text-sm text-slate-600">{subtitle}</p>
        </div>
        <div className="flex items-center gap-2">
          <ViewToggle value={view} onChange={setView} />
          <button
            type="button"
            onClick={() => void fetchData()}
            className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            Refresh
          </button>
        </div>
      </header>

      <StatusFilter
        statuses={statuses}
        counts={counts}
        selected={selected}
        onChange={setSelected}
        total={countsTotal}
        kind={partyKind}
      />

      {err ? (
        <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
          {err}
        </div>
      ) : null}

      {loading ? (
        <div className="rounded-md border border-slate-200 bg-white">
          <LoadingRows count={5} />
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title={emptyTitle ?? "Nothing in this view yet."}
          description={
            emptyBody ??
            (selected.size > 0
              ? "Try clearing the status filter or selecting another stage."
              : `No ${partyKind}s have been recorded yet.`)
          }
        />
      ) : view === "table" ? (
        <DataTable
          rows={items.map((c) => (
            <ContactRowView
              key={c.id}
              contact={c}
              statusField={statusField}
              kind={partyKind}
            />
          ))}
        />
      ) : (
        <KanbanBoard
          statuses={statuses}
          items={items}
          getStatus={(c) => c[statusField] as string | null | undefined}
          counts={counts}
          toneFor={toneFor}
          kind={partyKind}
          renderCard={(c) => (
            <ContactCard contact={c} statusField={statusField} kind={partyKind} />
          )}
        />
      )}

      <p className="text-xs text-slate-500">
        Showing {items.length} of {total}.
      </p>
    </div>
  );
}

function ContactRowView({
  contact,
  statusField,
  kind,
}: {
  contact: ContactRow;
  statusField: "adopter_status" | "facilitator_status";
  kind: PartyKind;
}) {
  const status = contact[statusField] as string | null | undefined;
  return (
    <DataRow
      id={contact.id}
      href={`/workflow/${contact.id}`}
      title={contact.display_name}
      badge={<StatusBadge status={status ?? undefined} kind={kind} />}
      meta={
        <>
          {contact.email_normalized ? (
            <span className="text-slate-600">{contact.email_normalized}</span>
          ) : (
            <span className="text-slate-400">No email</span>
          )}
          {contact.country_code ? (
            <>
              <span className="text-slate-300">·</span>
              <span className="text-slate-500">
                Country: {contact.country_code}
              </span>
            </>
          ) : null}
        </>
      }
      subtle={
        contact.created_at
          ? `Created ${formatTimestamp(contact.created_at)}`
          : undefined
      }
      action={
        <a
          href={`/workflow/${contact.id}`}
          className="text-sm font-medium text-jp-accent hover:underline"
        >
          Open →
        </a>
      }
    />
  );
}

function ContactCard({
  contact,
  statusField,
  kind,
}: {
  contact: ContactRow;
  statusField: "adopter_status" | "facilitator_status";
  kind: PartyKind;
}) {
  const status = contact[statusField] as string | null | undefined;
  return (
    <a
      href={`/workflow/${contact.id}`}
      className="block rounded border border-slate-200 bg-white p-3 shadow-sm transition hover:border-slate-300 hover:shadow"
    >
      <div className="flex items-start justify-between gap-2">
        <h4 className="font-heading text-sm font-semibold text-slate-900">
          {contact.display_name}
        </h4>
        <StatusBadge status={status ?? undefined} kind={kind}>
          {status ? humanizeStatus(status, kind) : "Unset"}
        </StatusBadge>
      </div>
      <div className="mt-2 space-y-0.5 text-[11px] text-slate-600">
        {contact.email_normalized ? (
          <div className="truncate">{contact.email_normalized}</div>
        ) : null}
        {contact.country_code ? (
          <div>Country: {contact.country_code}</div>
        ) : null}
      </div>
    </a>
  );
}
