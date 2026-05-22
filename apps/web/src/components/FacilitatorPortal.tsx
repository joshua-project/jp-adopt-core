"use client";

import { useCallback, useEffect, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, getMatchQueue } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";
import { CodeChip, StatusBadge } from "./StatusBadge";
import { formatDate } from "../lib/vocab";

type Queue =
  paths["/v1/matches/queue"]["get"]["responses"]["200"]["content"]["application/json"];

/**
 * Facilitator-facing landing: the queue, server-side filtered to the actor's
 * org memberships. Same endpoint as the staff view; the API enforces the
 * org-scope. Rendering differs from MatchQueue mainly in the framing
 * ("My contacts") and the accept/decline call-to-action.
 */
export function FacilitatorPortal() {
  const ctx = useApiContext();
  const [data, setData] = useState<Queue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const queue = await getMatchQueue(ctx);
      setData(queue);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setErr("Sign in required to view your contacts.");
      } else if (e instanceof ApiError && e.status === 403) {
        setErr(
          "Your account isn't linked to a facilitating organization yet. Ask staff to grant access.",
        );
      } else {
        setErr(e instanceof Error ? e.message : "Failed to load");
      }
    } finally {
      setLoading(false);
    }
  }, [ctx]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
            My contacts
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Adopters matched with your organization.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 shadow-sm hover:bg-slate-50 disabled:opacity-50"
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {err ? (
        <div
          role="alert"
          className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
        >
          {err}
        </div>
      ) : null}

      {loading && !data ? (
        <DataTable rows={null} empty={<LoadingRows />} />
      ) : data ? (
        <DataTable
          caption={
            <>
              {data.total} assigned to you
            </>
          }
          rows={
            data.items.length > 0
              ? data.items.map((item) => (
                  <DataRow
                    key={item.id}
                    id={item.id}
                    href={`/facilitator/contacts/${item.contact_id}?match=${item.id}`}
                    title={item.contact_display_name}
                    badge={<StatusBadge status={item.status} kind="match" />}
                    meta={
                      <>
                        {item.rop3 ? (
                          <span className="inline-flex items-center gap-1">
                            <span className="text-slate-500">FPG:</span>
                            <CodeChip>{item.rop3}</CodeChip>
                          </span>
                        ) : (
                          <span className="text-slate-500">FPG: —</span>
                        )}
                        {item.decided_at ? (
                          <>
                            <span className="text-slate-300">·</span>
                            <span>
                              <span className="text-slate-500">Matched:</span>{" "}
                              <span className="text-slate-800">
                                {formatDate(item.decided_at)}
                              </span>
                            </span>
                          </>
                        ) : null}
                      </>
                    }
                    action={<>Review →</>}
                  />
                ))
              : null
          }
          empty={
            <EmptyState
              title="No active matches assigned to you right now."
              description="When staff routes a new adopter to your organization it will show up here."
            />
          }
        />
      ) : null}
    </div>
  );
}
