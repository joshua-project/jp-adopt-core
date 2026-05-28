"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, decideMatch, getMatch } from "../lib/api-client";
import { REASON_CODES, isReasonCode, type ReasonCode } from "../lib/reason-codes";
import { useApiContext } from "../lib/useApiContext";
import { humanizeReasonCode } from "../lib/vocab";

type Match =
  paths["/v1/matches/{match_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type Candidate = NonNullable<Match["candidates"]>[number];

function CandidateRow({
  c,
  highlighted,
  onPick,
}: {
  c: Candidate;
  highlighted: boolean;
  onPick?: () => void;
}) {
  const cls = highlighted
    ? "border-emerald-300 bg-emerald-50"
    : "border-slate-200 bg-white";
  return (
    <li className={`rounded border ${cls} px-3 py-2 text-sm`}>
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <div className="font-medium text-slate-900">{c.facilitator_name}</div>
          <div className="text-xs text-slate-500">
            rank: {c.rank ?? "—"} · score:{" "}
            <span className="font-mono">
              {c.score !== null && c.score !== undefined
                ? c.score.toFixed(3)
                : "—"}
            </span>
          </div>
        </div>
        {onPick ? (
          <button
            type="button"
            onClick={onPick}
            className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100"
          >
            Pick
          </button>
        ) : null}
      </div>
      {c.score_breakdown ? (
        <dl className="mt-2 grid grid-cols-5 gap-2 text-xs text-slate-600">
          {Object.entries(c.score_breakdown).map(([k, v]) => (
            <div key={k} className="rounded bg-slate-100 px-2 py-1">
              <dt className="truncate">{k}</dt>
              <dd className="font-mono">{Number(v).toFixed(2)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </li>
  );
}

export function MatchReview({ matchId }: { matchId: string }) {
  const ctx = useApiContext();
  const router = useRouter();
  const [data, setData] = useState<Match | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reason, setReason] = useState<ReasonCode | undefined>(undefined);
  const [reasonText, setReasonText] = useState("");
  const [isDeciding, startDecide] = useTransition();

  const load = useCallback(async () => {
    setErr(null);
    try {
      const m = await getMatch(ctx, matchId);
      setData(m);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load match");
    }
  }, [ctx, matchId]);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = useCallback(
    (
      decision: "accept" | "send_back" | "route_elsewhere",
      nextAttemptId?: string,
    ) => {
      if (decision === "send_back" && !reason) {
        setErr("send_back requires a reason");
        return;
      }
      startDecide(() => {
        void (async () => {
          setErr(null);
          try {
            await decideMatch(ctx, matchId, {
              decision,
              reason_code: reason ?? undefined,
              reason_text: reasonText.trim() || undefined,
              next_attempt_id: nextAttemptId,
            });
            // After a successful decision, route back to the queue.
            router.push("/matches");
          } catch (e) {
            if (e instanceof ApiError) {
              const body =
                typeof e.body === "object" &&
                e.body !== null &&
                "detail" in e.body
                  ? (e.body as { detail: unknown }).detail
                  : null;
              const code =
                typeof body === "object" && body !== null && "code" in body
                  ? (body as { code: string }).code
                  : null;
              setErr(`${code ?? "error"}: ${e.message}`);
            } else {
              setErr(e instanceof Error ? e.message : "Decide failed");
            }
          }
        })();
      });
    },
    [ctx, matchId, reason, reasonText, router],
  );

  if (!data) {
    return (
      <div className="text-sm text-slate-500">
        {err ?? "Loading match…"}{" "}
        <Link className="underline" href="/matches">
          ← back
        </Link>
      </div>
    );
  }

  const allCandidates = data.candidates ?? [];
  const primary = allCandidates.find(
    (c) => c.facilitator_org_id === data.facilitator_org_id,
  );
  const alternates = allCandidates.filter(
    (c) => c.facilitator_org_id !== data.facilitator_org_id,
  );

  return (
    <div className="space-y-6">
      <div>
        <Link
          className="text-sm text-slate-600 hover:text-slate-900"
          href="/matches"
        >
          ← back to queue
        </Link>
        <h1 className="mt-2 font-heading text-3xl font-semibold tracking-tight text-slate-900">
          {data.contact_display_name}
        </h1>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-slate-600">
          <span>
            <span className="text-slate-500">Status:</span>{" "}
            <span className="font-medium text-slate-800">
              {(data.contact_adopter_status ?? "—").replace(/_/g, " ")}
            </span>
          </span>
          <span className="text-slate-300">·</span>
          <span>
            <span className="text-slate-500">FPG:</span>{" "}
            <span className="font-mono text-slate-800">{data.people_id3 ?? "—"}</span>
          </span>
        </p>
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-medium text-slate-700">
          Primary recommendation
        </h2>
        {primary ? (
          <ul className="space-y-2">
            <CandidateRow c={primary} highlighted />
          </ul>
        ) : (
          <p className="text-sm text-slate-500">
            No primary candidate found (this should not happen).
          </p>
        )}
      </section>

      {alternates.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-slate-700">Alternates</h2>
          <ul className="space-y-2">
            {alternates.map((c) => (
              <CandidateRow
                key={c.attempt_id}
                c={c}
                highlighted={false}
                onPick={() => decide("route_elsewhere", c.attempt_id)}
              />
            ))}
          </ul>
        </section>
      ) : null}

      <section className="space-y-3 rounded border border-slate-200 bg-slate-50/80 p-4">
        <h2 className="text-sm font-medium text-slate-700">Decision</h2>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <label className="text-xs text-slate-600">
            Reason code (required for send-back)
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={reason ?? ""}
              onChange={(e) => {
                // F29: never trust the raw DOM value — narrow through a type
                // guard so an injected option (devtools, extensions) can't
                // smuggle an unknown reason code into the request body.
                const v = e.target.value;
                setReason(isReasonCode(v) ? v : undefined);
              }}
            >
              <option value="">—</option>
              {REASON_CODES.map((r) => (
                <option key={r} value={r}>
                  {humanizeReasonCode(r)}
                </option>
              ))}
            </select>
          </label>
          <label className="col-span-2 sm:col-span-3 text-xs text-slate-600">
            Reason notes (optional)
            <input
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={reasonText}
              onChange={(e) => setReasonText(e.target.value)}
              maxLength={2048}
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={isDeciding}
            className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50"
            onClick={() => decide("accept")}
          >
            Accept
          </button>
          <button
            type="button"
            disabled={isDeciding}
            className="rounded-md bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50"
            onClick={() => decide("send_back")}
          >
            Send back
          </button>
          {alternates.length > 0 ? (
            <button
              type="button"
              disabled={isDeciding}
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50"
              onClick={() => decide("route_elsewhere")}
            >
              Route to next alternate
            </button>
          ) : null}
        </div>
        {err ? (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
            {err}
          </div>
        ) : null}
      </section>
    </div>
  );
}
