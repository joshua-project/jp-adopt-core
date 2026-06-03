"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  decideMatch,
  getAssignableOrgs,
  getMatch,
} from "../lib/api-client";
import { REASON_CODES, isReasonCode, type ReasonCode } from "../lib/reason-codes";
import { useApiContext } from "../lib/useApiContext";
import { humanizeReasonCode } from "../lib/vocab";

type Match =
  paths["/v1/matches/{match_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type Candidate = NonNullable<Match["candidates"]>[number];
type AssignableOrg =
  paths["/v1/matches/{match_id}/assignable-orgs"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];

function warningLabel(w: AssignableOrg["warning"]): string | null {
  if (w === "no_coverage") return "No coverage";
  if (w === "at_capacity") return "At capacity";
  return null;
}

function CandidateRow({ c, highlighted }: { c: Candidate; highlighted: boolean }) {
  const cls = highlighted
    ? "border-emerald-300 bg-emerald-50"
    : "border-slate-200 bg-white";
  return (
    <li className={`rounded border ${cls} px-3 py-2 text-sm`}>
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
  const [assignable, setAssignable] = useState<AssignableOrg[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [declining, setDeclining] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [reason, setReason] = useState<ReasonCode | undefined>(undefined);
  const [reasonText, setReasonText] = useState("");
  const [isDeciding, startDecide] = useTransition();

  const load = useCallback(async () => {
    setErr(null);
    try {
      const m = await getMatch(ctx, matchId);
      setData(m);
      try {
        const a = await getAssignableOrgs(ctx, matchId);
        setAssignable(a.items);
      } catch {
        setAssignable([]);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load match");
    }
  }, [ctx, matchId]);

  useEffect(() => {
    void load();
  }, [load]);

  const reportError = useCallback((e: unknown) => {
    if (e instanceof ApiError) {
      const body =
        typeof e.body === "object" && e.body !== null && "detail" in e.body
          ? (e.body as { detail: unknown }).detail
          : null;
      const code =
        typeof body === "object" && body !== null && "code" in body
          ? (body as { code: string }).code
          : null;
      setErr(`${code ?? "error"}: ${e.message}`);
    } else {
      setErr(e instanceof Error ? e.message : "Request failed");
    }
  }, []);

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
  // Read-only alternates context: scored candidates only (exclude current org
  // and null-rank override audit rows).
  const alternates = allCandidates.filter(
    (c) =>
      c.facilitator_org_id !== data.facilitator_org_id &&
      c.rank !== null &&
      c.rank !== undefined,
  );

  // The single facilitator selector: the recommendation (default) plus every
  // assignable org. An org that was a scored alternate carries its attempt_id
  // so accepting it routes via next_attempt_id (not flagged as an override);
  // any other org routes via facilitator_org_id (manual override).
  const currentName = primary?.facilitator_name ?? "Current recommendation";
  type Option = {
    orgId: string;
    label: string;
    warning: AssignableOrg["warning"];
    attemptId?: string;
  };
  const options: Option[] = [
    {
      orgId: data.facilitator_org_id,
      label: `${currentName} — recommended`,
      warning: null,
    },
    ...assignable.map((o): Option => {
      const scored = alternates.find(
        (c) => c.facilitator_org_id === o.facilitator_org_id,
      );
      const w = warningLabel(o.warning);
      return {
        orgId: o.facilitator_org_id,
        label: `${o.name}${scored ? " — alternate" : ""}${w ? ` — ${w}` : ""}`,
        warning: o.warning,
        attemptId: scored?.attempt_id,
      };
    }),
  ];
  const selected = selectedOrgId || data.facilitator_org_id;
  const selectedOpt = options.find((o) => o.orgId === selected);
  const reassigning = selected !== data.facilitator_org_id;

  const submitAccept = () => {
    startDecide(() => {
      void (async () => {
        setErr(null);
        try {
          if (!reassigning) {
            // Accept the current recommendation.
            await decideMatch(ctx, matchId, { decision: "accept" });
          } else {
            // Reassign to the selected facilitator, then accept it. A scored
            // alternate routes via next_attempt_id; anything else is a manual
            // override via facilitator_org_id.
            const routeBody = selectedOpt?.attemptId
              ? { decision: "route_elsewhere" as const, next_attempt_id: selectedOpt.attemptId }
              : { decision: "route_elsewhere" as const, facilitator_org_id: selected };
            const res = await decideMatch(ctx, matchId, routeBody);
            if (!res.new_match_id) {
              setErr("Reassignment did not return a new match id");
              return;
            }
            await decideMatch(ctx, res.new_match_id, { decision: "accept" });
          }
          router.push("/matches");
        } catch (e) {
          reportError(e);
        }
      })();
    });
  };

  const submitSendBack = () => {
    startDecide(() => {
      void (async () => {
        setErr(null);
        try {
          await decideMatch(ctx, matchId, {
            decision: "send_back",
            reason_code: reason ?? undefined,
            reason_text: reasonText.trim() || undefined,
          });
          router.push("/matches");
        } catch (e) {
          reportError(e);
        }
      })();
    });
  };

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
            No primary candidate (triage / no-coverage). Pick a facilitator
            below to assign.
          </p>
        )}
      </section>

      {alternates.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-slate-700">
            Scored alternates
          </h2>
          <ul className="space-y-2">
            {alternates.map((c) => (
              <CandidateRow key={c.attempt_id} c={c} highlighted={false} />
            ))}
          </ul>
        </section>
      ) : null}

      <section className="space-y-3 rounded border border-slate-200 bg-slate-50/80 p-4">
        <h2 className="text-sm font-medium text-slate-700">Decision</h2>

        {/* Pick the facilitator, then Accept commits it. Default is the
            recommendation; any other org is a reassignment (a scored
            alternate, or a manual override). */}
        <label className="block text-xs text-slate-600">
          Facilitator
          <select
            className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
            value={selected}
            onChange={(e) => setSelectedOrgId(e.target.value)}
          >
            {options.map((o) => (
              <option key={o.orgId} value={o.orgId}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {reassigning && selectedOpt && warningLabel(selectedOpt.warning) ? (
          <p className="text-xs text-amber-800">
            This org is{" "}
            <span className="font-medium">
              {warningLabel(selectedOpt.warning)?.toLowerCase()}
            </span>
            . Accepting it is a manual override and is recorded as such.
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={isDeciding}
            className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50"
            onClick={submitAccept}
          >
            {reassigning ? "Assign & accept" : "Accept"}
          </button>
          {!declining ? (
            <button
              type="button"
              disabled={isDeciding}
              className="rounded-md bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50"
              onClick={() => setDeclining(true)}
            >
              Send back
            </button>
          ) : null}
        </div>

        {/* F2: the reason is only prompted when declining, and is optional. */}
        {declining ? (
          <div className="space-y-3 rounded border border-amber-200 bg-amber-50/60 p-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <label className="text-xs text-slate-600">
                Reason (optional)
                <select
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
                  value={reason ?? ""}
                  onChange={(e) => {
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
                className="rounded-md bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50"
                onClick={submitSendBack}
              >
                Confirm send back
              </button>
              <button
                type="button"
                disabled={isDeciding}
                className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                onClick={() => {
                  setDeclining(false);
                  setReason(undefined);
                  setReasonText("");
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {err ? (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
            {err}
          </div>
        ) : null}
      </section>
    </div>
  );
}
