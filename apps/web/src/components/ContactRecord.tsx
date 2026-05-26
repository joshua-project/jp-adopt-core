"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import type { ReactNode } from "react";

import type { paths } from "@jp-adopt/contracts";

import { apiFetch } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import {
  formatTimestamp,
  humanizeOrigin,
  humanizePartyKind,
  humanizeReasonCode,
  humanizeStatus,
} from "../lib/vocab";
import { CodeChip, StatusBadge } from "./StatusBadge";

type Contact =
  paths["/v1/contacts/{contact_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type Matches =
  paths["/v1/contacts/{contact_id}/matches"]["get"]["responses"]["200"]["content"]["application/json"];
type Transitions =
  paths["/v1/contacts/{contact_id}/transitions"]["get"]["responses"]["200"]["content"]["application/json"];
type Activity =
  paths["/v1/contacts/{contact_id}/activity"]["get"]["responses"]["200"]["content"]["application/json"];

function Tile({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700">
        {title}
        {count !== undefined ? (
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
            {count}
          </span>
        ) : null}
      </h2>
      {children}
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-slate-400">{children}</p>;
}

export function ContactRecord({ contactId }: { contactId: string }) {
  const ctx = useApiContext();
  const [contact, setContact] = useState<Contact | null>(null);
  const [matches, setMatches] = useState<Matches | null>(null);
  const [transitions, setTransitions] = useState<Transitions | null>(null);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      // Client-side fan-out (U5); a /timeline merge endpoint can replace this later.
      const [c, m, t, a] = await Promise.all([
        apiFetch<Contact>(ctx, `/v1/contacts/${contactId}`),
        apiFetch<Matches>(ctx, `/v1/contacts/${contactId}/matches`),
        apiFetch<Transitions>(ctx, `/v1/contacts/${contactId}/transitions`),
        apiFetch<Activity>(ctx, `/v1/contacts/${contactId}/activity`),
      ]);
      if (!c) {
        setErr("Contact not found");
        return;
      }
      setContact(c);
      setMatches(m ?? null);
      setTransitions(t ?? null);
      setActivity(a ?? null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load contact");
    }
  }, [ctx, contactId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (err) {
    return (
      <div className="space-y-4">
        <Link href="/contacts" className="text-sm text-slate-600 hover:text-slate-900">
          ← back to contacts
        </Link>
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      </div>
    );
  }

  if (!contact) {
    return <p className="text-sm text-slate-500">Loading…</p>;
  }

  const isAdopter = contact.party_kind === "adopter";
  const statusKind = isAdopter ? "adopter" : "facilitator";
  const status = isAdopter ? contact.adopter_status : contact.facilitator_status;
  // Distinct FPG interests derived from the contact's matches.
  const rop3s = Array.from(
    new Set((matches?.items ?? []).map((m) => m.rop3).filter((r): r is string => !!r)),
  );

  return (
    <div className="space-y-6">
      <Link href="/contacts" className="text-sm text-slate-600 hover:text-slate-900">
        ← back to contacts
      </Link>

      {/* Header */}
      <header className="flex flex-wrap items-start justify-between gap-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <div className="space-y-2">
          <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
            {contact.display_name}
          </h1>
          <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
              {humanizePartyKind(contact.party_kind)}
            </span>
            <StatusBadge status={status ?? undefined} kind={statusKind} />
            {contact.email_normalized ? <span>{contact.email_normalized}</span> : null}
            {contact.country_code ? <CodeChip>{contact.country_code}</CodeChip> : null}
            {(contact.language_codes ?? []).map((l) => (
              <CodeChip key={l}>{l}</CodeChip>
            ))}
          </div>
          <p className="text-xs text-slate-400">
            Origin: {humanizeOrigin(contact.origin)} · updated {formatTimestamp(contact.updated_at)}
          </p>
        </div>
      </header>

      {/* Read tiles */}
      <div className="grid gap-4 lg:grid-cols-2">
        {isAdopter ? (
          <Tile title="People-group interests" count={rop3s.length}>
            {rop3s.length ? (
              <div className="flex flex-wrap gap-1.5">
                {rop3s.map((r) => (
                  <CodeChip key={r}>{r}</CodeChip>
                ))}
              </div>
            ) : (
              <Empty>No FPG selections yet.</Empty>
            )}
          </Tile>
        ) : null}

        <Tile title="Matches" count={matches?.total ?? 0}>
          {matches?.items.length ? (
            <ul className="divide-y divide-slate-100 text-sm">
              {matches.items.map((m) => (
                <li key={m.id} className="flex items-center justify-between gap-2 py-2">
                  <span className="flex items-center gap-2">
                    <StatusBadge status={m.status} kind="match" />
                    <span className="text-slate-700">{m.facilitator_name}</span>
                    {m.rop3 ? <CodeChip>{m.rop3}</CodeChip> : null}
                  </span>
                  <span className="text-xs text-slate-400">
                    {formatTimestamp(m.recommended_at)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No matches yet.</Empty>
          )}
        </Tile>

        <Tile title="Workflow history" count={transitions?.total ?? 0}>
          {transitions?.items.length ? (
            <ul className="space-y-2 text-sm">
              {transitions.items.map((t) => (
                <li key={t.id} className="flex items-baseline justify-between gap-2">
                  <span className="text-slate-700">
                    {humanizeStatus(t.from_state, statusKind)} →{" "}
                    <span className="font-medium">{humanizeStatus(t.to_state, statusKind)}</span>
                    {t.reason_code ? (
                      <span className="text-slate-400"> · {humanizeReasonCode(t.reason_code)}</span>
                    ) : null}
                  </span>
                  <span className="text-xs text-slate-400">{formatTimestamp(t.occurred_at)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No transitions recorded.</Empty>
          )}
        </Tile>

        <Tile title="Activity" count={activity?.total ?? 0}>
          {activity?.items.length ? (
            <ul className="space-y-3 text-sm">
              {activity.items.map((a) => (
                <li key={a.id}>
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                      {a.kind ?? "note"}
                    </span>
                    <span className="text-xs text-slate-400">{formatTimestamp(a.occurred_at)}</span>
                  </div>
                  <p className="text-slate-700">{a.body}</p>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No activity yet.</Empty>
          )}
        </Tile>
      </div>
    </div>
  );
}
