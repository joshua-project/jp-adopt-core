"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  apiFetch,
  decideMatch,
  transitionContact,
} from "../lib/api-client";
import { REASON_CODES, type ReasonCode } from "../lib/reason-codes";
import { useApiContext } from "../lib/useApiContext";

type Contact =
  paths["/v1/contacts/{contact_id}"]["get"]["responses"]["200"]["content"]["application/json"];

const ADOPTER_STATES = [
  "draft",
  "new",
  "potential_adopter",
  "contacted",
  "engaged",
  "matched",
  "sent_back",
  "active",
  "inactive",
  "do_not_engage",
];

const FACILITATOR_STATES = [
  "draft",
  "new",
  "not_ready",
  "ready",
  "do_not_engage",
];

// F8: include the empty option as a sentinel for "no reason yet selected".
// The shared REASON_CODES list is the source of truth for the actual codes.
const REASON_OPTIONS: readonly (ReasonCode | "")[] = ["", ...REASON_CODES];

export function WorkflowTransition({ contactId }: { contactId: string }) {
  const ctx = useApiContext();
  // F14: when the facilitator portal links to ?match=<id>, the user's
  // accept/send_back actions must go through the match-decide surface so
  // capacity counters bump, decided_at/decided_by stamp, and the canonical
  // outbox events fire. We expose explicit accept / send-back buttons in
  // that mode, and keep the generic transition flow available for other
  // moves (active → inactive, etc.).
  const searchParams = useSearchParams();
  const matchId = searchParams.get("match");
  const [contact, setContact] = useState<Contact | null>(null);
  const [kind, setKind] = useState<"adopter" | "facilitator">("adopter");
  // F38: leave the to-state empty until the user picks one; the previous
  // default of "contacted" silently committed Amy to a specific transition
  // if she hit Apply without thinking. Empty placeholder forces an explicit
  // choice (the submit button disables until non-empty).
  const [toState, setToState] = useState<string>("");
  // F11: type the state precisely so the request-body cast on submit is a
  // legal narrowing, not a `never` cast.
  const [reason, setReason] = useState<ReasonCode | "">("");
  const [reasonText, setReasonText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const load = useCallback(async () => {
    setErr(null);
    try {
      const c = await apiFetch<Contact>(ctx, `/v1/contacts/${contactId}`);
      if (!c) {
        setErr("Empty contact response");
        return;
      }
      setContact(c);
      // Default the form's kind based on which side the contact already has
      // a status on. Saves Amy a click.
      if (c.facilitator_status && !c.adopter_status) {
        setKind("facilitator");
      } else {
        setKind("adopter");
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load contact");
    }
  }, [ctx, contactId]);

  useEffect(() => {
    void load();
  }, [load]);

  function _handleError(e: unknown, fallback: string) {
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
      setErr(e instanceof Error ? e.message : fallback);
    }
  }

  const submit = useCallback(() => {
    setErr(null);
    setMsg(null);
    startTransition(() => {
      void (async () => {
        try {
          const r = await transitionContact(ctx, contactId, {
            kind,
            to_state: toState,
            reason_code: reason || undefined,
            reason_text: reasonText.trim() || undefined,
          });
          setMsg(`Transitioned to ${r.transitioned_to}`);
          setContact(r.contact);
        } catch (e) {
          _handleError(e, "Transition failed");
        }
      })();
    });
  }, [ctx, contactId, kind, toState, reason, reasonText]);

  // F14: match-aware accept/send-back. Only invoked when ``?match=<id>`` is
  // present in the URL (set by the facilitator portal link).
  const decideOnMatch = useCallback(
    (decision: "accept" | "send_back") => {
      if (!matchId) return;
      if (decision === "send_back" && !reason) {
        setErr("send_back requires a reason");
        return;
      }
      setErr(null);
      setMsg(null);
      startTransition(() => {
        void (async () => {
          try {
            const r = await decideMatch(ctx, matchId, {
              decision,
              reason_code: reason || undefined,
              reason_text: reasonText.trim() || undefined,
            });
            setMsg(
              decision === "accept"
                ? `Accepted: ${r.match.status}`
                : `Sent back: ${r.match.status}`,
            );
            // Refresh the contact view to surface the new adopter_status.
            await load();
          } catch (e) {
            _handleError(e, "Decision failed");
          }
        })();
      });
    },
    [ctx, matchId, reason, reasonText, load],
  );

  const states = kind === "adopter" ? ADOPTER_STATES : FACILITATOR_STATES;

  return (
    <div className="space-y-6">
      <Link href="/matches" className="text-sm text-slate-600 hover:text-slate-900">
        ← back to queue
      </Link>

      <div>
        <h1 className="text-2xl font-semibold text-slate-900">
          {contact?.display_name ?? "Loading…"}
        </h1>
        {contact ? (
          <p className="text-sm text-slate-600">
            adopter_status:{" "}
            <span className="font-mono">{contact.adopter_status ?? "—"}</span>{" "}
            · facilitator_status:{" "}
            <span className="font-mono">
              {contact.facilitator_status ?? "—"}
            </span>
          </p>
        ) : null}
      </div>

      {matchId ? (
        <section className="space-y-3 rounded border border-slate-200 bg-emerald-50/40 p-4">
          <h2 className="text-sm font-medium text-slate-700">
            Match decision
          </h2>
          <p className="text-xs text-slate-600">
            These buttons hit ``/v1/matches/{matchId}/decide`` so capacity is
            reserved and the canonical match.* outbox event fires. Use the
            generic transition section below only for non-match state moves.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={isPending}
              onClick={() => decideOnMatch("accept")}
              className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50"
            >
              Accept match
            </button>
            <button
              type="button"
              disabled={isPending}
              onClick={() => decideOnMatch("send_back")}
              className="rounded-md bg-amber-700 px-3 py-2 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-50"
            >
              Send back
            </button>
          </div>
        </section>
      ) : null}

      <section className="space-y-3 rounded border border-slate-200 bg-slate-50/80 p-4">
        <h2 className="text-sm font-medium text-slate-700">Transition</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className="text-xs text-slate-600">
            Kind
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={kind}
              onChange={(e) =>
                setKind(e.target.value as "adopter" | "facilitator")
              }
            >
              <option value="adopter">adopter</option>
              <option value="facilitator">facilitator</option>
            </select>
          </label>
          <label className="text-xs text-slate-600">
            To state
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={toState}
              onChange={(e) => setToState(e.target.value)}
            >
              <option value="" disabled>
                —
              </option>
              {states.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-600">
            Reason code
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={reason}
              onChange={(e) => setReason(e.target.value as ReasonCode | "")}
            >
              {REASON_OPTIONS.map((r) => (
                <option key={r || "_"} value={r}>
                  {r || "—"}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-600">
            Reason notes
            <input
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={reasonText}
              onChange={(e) => setReasonText(e.target.value)}
            />
          </label>
        </div>
        <button
          type="button"
          onClick={submit}
          disabled={isPending || !toState}
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {isPending ? "Working…" : "Apply transition"}
        </button>
        {err ? (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
            {err}
          </div>
        ) : null}
        {msg ? (
          <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
            {msg}
          </div>
        ) : null}
      </section>
    </div>
  );
}
