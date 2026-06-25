"use client";

import { useCallback, useEffect, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  clearDuplicateDecision,
  decideDuplicateConflict,
  formatApiError,
  listDuplicateConflicts,
} from "../lib/api-client";
import { BTN } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";
import { formatTimestamp, humanizeStatus } from "../lib/vocab";
import { EmptyState, LoadingRows } from "./DataTable";

type ConflictRow =
  paths["/v1/admin/duplicate-conflicts"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];
type ContactSide = NonNullable<ConflictRow["dt_contact"]>;

function rowKey(r: ConflictRow): string {
  return `${r.email}::${r.dt_source_id}`;
}

function ContactCard({
  title,
  contact,
  tone,
}: {
  title: string;
  contact: ContactSide | null;
  tone: "dt" | "owner";
}) {
  return (
    <div
      className={`flex-1 rounded border p-3 ${
        tone === "owner"
          ? "border-emerald-200 bg-emerald-50/40"
          : "border-slate-200 bg-slate-50"
      }`}
    >
      <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
        {title}
      </div>
      {contact ? (
        <>
          <a
            href={`/contacts/${contact.id}`}
            className="mt-0.5 block font-heading text-sm font-semibold text-jp-accent hover:underline"
          >
            {contact.display_name}
          </a>
          <div className="mt-1 space-y-0.5 text-[11px] text-slate-600">
            <div>{contact.email_normalized ?? "No email"}</div>
            {contact.adopter_status ? (
              <div>{humanizeStatus(contact.adopter_status, "adopter")}</div>
            ) : null}
            {contact.created_at ? (
              <div className="text-slate-400">
                Created {formatTimestamp(contact.created_at)}
              </div>
            ) : null}
          </div>
        </>
      ) : (
        <div className="mt-1 text-[11px] text-slate-400">
          No matching contact found.
        </div>
      )}
    </div>
  );
}

export function DuplicateReviewAdmin() {
  const ctx = useApiContext();
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [items, setItems] = useState<ConflictRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setForbidden(false);
    try {
      const res = await listDuplicateConflicts(ctx, {});
      setItems(res.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
        setItems([]);
        return;
      }
      setErr(formatApiError(e));
    } finally {
      setLoading(false);
    }
  }, [ctx]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (r: ConflictRow, action: "merge" | "ignore" | "clear") => {
      setBusyKey(rowKey(r));
      setErr(null);
      try {
        if (action === "clear") {
          await clearDuplicateDecision(ctx, {
            email: r.email,
            dt_source_id: r.dt_source_id,
          });
        } else {
          await decideDuplicateConflict(ctx, {
            email: r.email,
            dt_source_id: r.dt_source_id,
            decision: action,
          });
        }
        await load();
      } catch (e) {
        setErr(formatApiError(e));
      } finally {
        setBusyKey(null);
      }
    },
    [ctx, load],
  );

  if (forbidden) {
    return (
      <EmptyState
        title="You can't review duplicates"
        description="Duplicate review is gated to the staff_admin role."
      />
    );
  }

  const pending = items.filter((r) => r.decision == null).length;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-600">
          {loading
            ? "Loading…"
            : `${pending} to review${
                items.length > pending
                  ? ` · ${items.length - pending} queued to merge`
                  : ""
              }`}
        </span>
        <button
          type="button"
          className={BTN}
          disabled={loading}
          onClick={() => void load()}
        >
          Refresh
        </button>
      </div>

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      {loading ? (
        <div className="rounded-md border border-slate-200 bg-white">
          <LoadingRows count={4} />
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title="No duplicates to review."
          description="DT↔forms collisions with matching names merge automatically each hour. Anything ambiguous shows up here."
        />
      ) : (
        <ul className="space-y-3">
          {items.map((r) => {
            const busy = busyKey === rowKey(r);
            const queued = r.decision === "merge";
            const shared = r.cluster_size > 1;
            return (
              <li
                key={rowKey(r)}
                className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-slate-700">
                    {r.email}
                  </span>
                  {shared ? (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800">
                      Shared by {r.cluster_size} DT records — likely different
                      people
                    </span>
                  ) : null}
                  {queued ? (
                    <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium text-emerald-800">
                      Queued to merge — applies next sync
                    </span>
                  ) : null}
                </div>

                <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-stretch">
                  <ContactCard
                    title="DT record"
                    contact={r.dt_contact ?? null}
                    tone="dt"
                  />
                  <div className="flex items-center justify-center text-slate-400">
                    →
                  </div>
                  <ContactCard
                    title="Keeps the email"
                    contact={r.owner_contact ?? null}
                    tone="owner"
                  />
                </div>

                <div className="mt-3 flex flex-wrap justify-end gap-2">
                  {queued ? (
                    <button
                      type="button"
                      className={BTN}
                      disabled={busy}
                      onClick={() => void act(r, "clear")}
                    >
                      {busy ? "…" : "Undo"}
                    </button>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="rounded border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                        disabled={busy}
                        onClick={() => void act(r, "ignore")}
                      >
                        {busy ? "…" : "Not a duplicate"}
                      </button>
                      <button
                        type="button"
                        className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                        disabled={busy}
                        onClick={() => void act(r, "merge")}
                      >
                        {busy ? "Saving…" : "Same person — merge"}
                      </button>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
