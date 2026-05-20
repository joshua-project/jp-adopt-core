"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, apiFetch, transitionContact } from "../lib/api-client";
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

const REASON_CODES = [
  "",
  "capacity_full",
  "geography_mismatch",
  "language",
  "theological_concern",
  "not_ready",
  "other",
] as const;

export function WorkflowTransition({ contactId }: { contactId: string }) {
  const ctx = useApiContext();
  const [contact, setContact] = useState<Contact | null>(null);
  const [kind, setKind] = useState<"adopter" | "facilitator">("adopter");
  const [toState, setToState] = useState<string>("contacted");
  const [reason, setReason] = useState<string>("");
  const [reasonText, setReasonText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const load = useCallback(async () => {
    setErr(null);
    try {
      const c = await apiFetch<Contact>(ctx, `/v1/contacts/${contactId}`);
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

  const submit = useCallback(() => {
    setErr(null);
    setMsg(null);
    startTransition(() => {
      void (async () => {
        try {
          const r = await transitionContact(ctx, contactId, {
            kind,
            to_state: toState,
            reason_code: reason ? (reason as never) : undefined,
            reason_text: reasonText.trim() || undefined,
          });
          setMsg(`Transitioned to ${r.transitioned_to}`);
          setContact(r.contact);
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
            setErr(e instanceof Error ? e.message : "Transition failed");
          }
        }
      })();
    });
  }, [ctx, contactId, kind, toState, reason, reasonText]);

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
              onChange={(e) => setReason(e.target.value)}
            >
              {REASON_CODES.map((r) => (
                <option key={r} value={r}>
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
          disabled={isPending}
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
