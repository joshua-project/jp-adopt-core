"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, getMatchQueue } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

type Queue =
  paths["/v1/matches/queue"]["get"]["responses"]["200"]["content"]["application/json"];

/**
 * Facilitator-facing landing: the queue, server-side filtered to the actor's
 * org memberships. Same endpoint as Amy's view; the API enforces the
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
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">
            My contacts
          </h1>
          <p className="text-sm text-slate-600">
            Adopters matched with your organization.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium hover:bg-slate-50 disabled:opacity-50"
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {err ? (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {err}
        </div>
      ) : null}

      {data ? (
        data.total === 0 ? (
          <p className="rounded border border-slate-200 bg-white px-4 py-6 text-center text-sm text-slate-500">
            No active matches assigned to you right now.
          </p>
        ) : (
          <ul className="divide-y divide-slate-200 overflow-hidden rounded border border-slate-200 bg-white">
            {data.items.map((item) => (
              <li
                key={item.id}
                className="flex flex-col gap-1 px-4 py-3 hover:bg-slate-50"
              >
                <div className="flex items-baseline justify-between">
                  <div className="font-medium text-slate-900">
                    {item.contact_display_name}
                  </div>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs">
                    {item.status}
                  </span>
                </div>
                <div className="text-xs text-slate-500">
                  rop3: {item.rop3 ?? "—"}
                  {item.decided_at ? (
                    <>
                      {" "}
                      · match_date:{" "}
                      {new Date(item.decided_at).toLocaleDateString()}
                    </>
                  ) : null}
                </div>
                <div className="text-xs">
                  <Link
                    href={`/facilitator/contacts/${item.contact_id}?match=${item.id}`}
                    className="text-slate-700 underline-offset-2 hover:underline"
                  >
                    review →
                  </Link>
                </div>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </div>
  );
}
