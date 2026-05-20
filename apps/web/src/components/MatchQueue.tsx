"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  getMatchQueue,
} from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

type QueueResponse =
  paths["/v1/matches/queue"]["get"]["responses"]["200"]["content"]["application/json"];

type MatchSummary = QueueResponse["items"][number];

function StatusPill({ status }: { status: string }) {
  const palette: Record<string, string> = {
    recommended: "bg-amber-100 text-amber-900 border-amber-200",
    triage: "bg-blue-100 text-blue-900 border-blue-200",
    accepted: "bg-emerald-100 text-emerald-900 border-emerald-200",
  };
  const cls = palette[status] ?? "bg-slate-100 text-slate-900 border-slate-200";
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {status}
    </span>
  );
}

function MatchRow({ item }: { item: MatchSummary }) {
  const topScore =
    item.candidates && item.candidates.length > 0
      ? item.candidates[0]?.score ?? null
      : null;
  return (
    <li className="flex flex-col gap-1 px-4 py-3 hover:bg-slate-50">
      <div className="flex items-baseline justify-between gap-3">
        <div className="font-medium text-slate-900">
          {item.contact_display_name}
        </div>
        <StatusPill status={item.status} />
      </div>
      <div className="text-xs text-slate-500">
        rop3: {item.rop3 ?? "—"} · facilitator:{" "}
        <span className="font-mono">{item.facilitator_name}</span>
        {topScore !== null ? (
          <>
            {" "}
            · top score:{" "}
            <span className="font-mono">{topScore.toFixed(3)}</span>
          </>
        ) : null}
      </div>
      <div className="text-xs text-slate-500">
        recommended:{" "}
        {new Date(item.recommended_at).toLocaleString(undefined, {
          dateStyle: "short",
          timeStyle: "short",
        })}
        {" · "}
        <Link
          className="text-slate-700 underline-offset-2 hover:underline"
          href={`/matches/${item.id}`}
        >
          review →
        </Link>
      </div>
    </li>
  );
}

export function MatchQueue() {
  const ctx = useApiContext();
  const [data, setData] = useState<QueueResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const queue = await getMatchQueue(ctx);
      setData(queue);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load queue");
      if (e instanceof ApiError && e.status === 401) {
        setErr("Sign in required to view the match queue.");
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
      <div className="flex items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">
            Match queue
          </h1>
          <p className="text-sm text-slate-600">
            Pending recommendations awaiting review.
          </p>
        </div>
        <button
          type="button"
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      {data ? (
        <div className="space-y-2">
          <p className="text-sm text-slate-500">{data.total} pending</p>
          {data.total === 0 ? (
            <p className="rounded border border-slate-200 bg-white px-4 py-6 text-center text-sm text-slate-500">
              Queue is empty. Nice work.
            </p>
          ) : (
            <ul className="divide-y divide-slate-200 overflow-hidden rounded border border-slate-200 bg-white">
              {data.items.map((item) => (
                <MatchRow key={item.id} item={item} />
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
