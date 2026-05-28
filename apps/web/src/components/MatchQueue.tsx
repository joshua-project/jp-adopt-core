"use client";

import { useCallback, useEffect, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  getMatchQueue,
} from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";
import { CodeChip, StatusBadge } from "./StatusBadge";
import { formatTimestamp } from "../lib/vocab";

type QueueResponse =
  paths["/v1/matches/queue"]["get"]["responses"]["200"]["content"]["application/json"];

type MatchSummary = QueueResponse["items"][number];

function MatchRow({ item }: { item: MatchSummary }) {
  const topScore =
    item.candidates && item.candidates.length > 0
      ? item.candidates[0]?.score ?? null
      : null;
  return (
    <DataRow
      id={item.id}
      href={`/matches/${item.id}`}
      title={item.contact_display_name}
      badge={<StatusBadge status={item.status} kind="match" />}
      meta={
        <>
          {item.people_id3 ? (
            <span className="inline-flex items-center gap-1">
              <span className="text-slate-500">FPG:</span>
              <CodeChip>{item.people_id3}</CodeChip>
            </span>
          ) : (
            <span className="text-slate-500">FPG: —</span>
          )}
          <span className="text-slate-300">·</span>
          <span>
            <span className="text-slate-500">Facilitator:</span>{" "}
            <span className="text-slate-800">{item.facilitator_name}</span>
          </span>
          {topScore !== null ? (
            <>
              <span className="text-slate-300">·</span>
              <span>
                <span className="text-slate-500">Top score:</span>{" "}
                <span className="font-mono text-slate-800">
                  {topScore.toFixed(3)}
                </span>
              </span>
            </>
          ) : null}
        </>
      }
      subtle={<>Recommended {formatTimestamp(item.recommended_at)}</>}
      action={<>Review →</>}
    />
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
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
            Match queue
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Pending recommendations awaiting review.
          </p>
        </div>
        <button
          type="button"
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 shadow-sm hover:bg-slate-50 disabled:opacity-50"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {err ? (
        <div
          role="alert"
          className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900"
        >
          {err}
        </div>
      ) : null}

      {loading && !data ? (
        <DataTable
          caption="Loading"
          rows={null}
          empty={<LoadingRows />}
        />
      ) : data ? (
        <DataTable
          caption={
            <>
              {data.total} pending
              {data.items.length !== data.total
                ? ` · showing ${data.items.length}`
                : null}
            </>
          }
          rows={
            data.items.length > 0
              ? data.items.map((item) => (
                  <MatchRow key={item.id} item={item} />
                ))
              : null
          }
          empty={
            <EmptyState
              title="Queue is clear."
              description="There are no pending recommendations right now."
            />
          }
        />
      ) : null}
    </div>
  );
}
