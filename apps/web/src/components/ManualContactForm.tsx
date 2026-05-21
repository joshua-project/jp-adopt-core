"use client";

import { useCallback, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, apiFetch } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

type ManualCreateBody =
  paths["/v1/contacts/manual"]["post"]["requestBody"]["content"]["application/json"];
type ManualCreateResponse =
  paths["/v1/contacts/manual"]["post"]["responses"]["201"]["content"]["application/json"];

const ORIGIN_VALUES = [
  "manual_entry",
  "core_org",
  "website",
  "third_party_referral",
  "partner_event",
  "other",
] as const;

export function ManualContactForm() {
  const ctx = useApiContext();
  const router = useRouter();
  const [isSubmitting, startSubmit] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [partyKind, setPartyKind] = useState<"adopter" | "facilitator">(
    "adopter",
  );
  const [origin, setOrigin] = useState<string>("manual_entry");
  const [countryCode, setCountryCode] = useState("");
  const [fpgRop3sRaw, setFpgRop3sRaw] = useState("");
  const [notes, setNotes] = useState("");
  const [newsletterOptIn, setNewsletterOptIn] = useState(false);

  const submit = useCallback(() => {
    setErr(null);
    setMsg(null);
    startSubmit(() => {
      void (async () => {
        const fpg_rop3s = fpgRop3sRaw
          .split(/[,\s]+/)
          .map((s) => s.trim())
          .filter(Boolean);
        const body: ManualCreateBody = {
          display_name: displayName,
          email,
          party_kind: partyKind,
          origin: origin || null,
          country_code: countryCode || null,
          fpg_rop3s,
          notes: notes || null,
          newsletter_opt_in: newsletterOptIn,
        };
        try {
          const r = await apiFetch<ManualCreateResponse>(
            ctx,
            "/v1/contacts/manual",
            { method: "POST", body },
          );
          if (!r) {
            setErr("Server returned no body");
            return;
          }
          setMsg(
            `${r.created ? "Created" : "Reused existing"} contact ${
              r.contact_id
            }${r.match_id ? ` with match ${r.match_id}` : ""}.`,
          );
          if (r.created) {
            // Reset form so staff can enter another
            setDisplayName("");
            setEmail("");
            setFpgRop3sRaw("");
            setNotes("");
          }
        } catch (e) {
          if (e instanceof ApiError) {
            const body =
              typeof e.body === "object" &&
              e.body !== null &&
              "detail" in e.body
                ? (e.body as { detail: unknown }).detail
                : null;
            if (typeof body === "object" && body !== null && "code" in body) {
              setErr(
                `${(body as { code: string }).code}: ${
                  (body as { message?: string }).message ?? e.message
                }`,
              );
            } else {
              setErr(e.message);
            }
          } else {
            setErr(e instanceof Error ? e.message : "Create failed");
          }
        }
      })();
    });
  }, [
    ctx,
    displayName,
    email,
    partyKind,
    origin,
    countryCode,
    fpgRop3sRaw,
    notes,
    newsletterOptIn,
  ]);

  return (
    <div className="space-y-6">
      <Link href="/contacts" className="text-sm text-slate-600 hover:text-slate-900">
        ← back to contacts
      </Link>
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">
          Add a contact manually
        </h1>
        <p className="text-sm text-slate-600">
          Use this when a contact arrives outside the public form
          (phone walk-in, event, referral). Auto-tags origin as
          <code className="ml-1 rounded bg-slate-100 px-1">manual_entry</code>
          unless overridden.
        </p>
      </div>

      <section className="space-y-3 rounded border border-slate-200 bg-slate-50/80 p-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="text-xs text-slate-600">
            Display name
            <input
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </label>
          <label className="text-xs text-slate-600">
            Email
            <input
              type="email"
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="text-xs text-slate-600">
            Party kind
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={partyKind}
              onChange={(e) =>
                setPartyKind(e.target.value as "adopter" | "facilitator")
              }
            >
              <option value="adopter">adopter</option>
              <option value="facilitator">facilitator</option>
            </select>
          </label>
          <label className="text-xs text-slate-600">
            Origin
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
              value={origin}
              onChange={(e) => setOrigin(e.target.value)}
            >
              {ORIGIN_VALUES.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-600">
            Country code (ISO 2)
            <input
              maxLength={2}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm uppercase"
              value={countryCode}
              onChange={(e) => setCountryCode(e.target.value.toUpperCase())}
            />
          </label>
          <label className="text-xs text-slate-600">
            FPG rop3 codes (comma or space separated)
            <input
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-sm"
              value={fpgRop3sRaw}
              onChange={(e) => setFpgRop3sRaw(e.target.value)}
              placeholder="AAA01, AAA02"
            />
          </label>
        </div>

        <label className="block text-xs text-slate-600">
          Notes (optional, captured on the AdopterInterest)
          <textarea
            className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
          />
        </label>

        <label className="flex items-center gap-2 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={newsletterOptIn}
            onChange={(e) => setNewsletterOptIn(e.target.checked)}
          />
          Newsletter opt-in (record on the contact; sync to platform is v2)
        </label>

        <button
          type="button"
          onClick={submit}
          disabled={isSubmitting || !displayName || !email}
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {isSubmitting ? "Saving…" : "Create contact"}
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
