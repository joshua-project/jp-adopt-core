"use client";

import { useCallback, useState, useTransition } from "react";
import Link from "next/link";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, apiFetch } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import { ORIGIN_OPTIONS, PARTY_KIND_OPTIONS } from "../lib/vocab";

type ManualCreateBody =
  paths["/v1/contacts/manual"]["post"]["requestBody"]["content"]["application/json"];
type ManualCreateResponse =
  paths["/v1/contacts/manual"]["post"]["responses"]["201"]["content"]["application/json"];

/**
 * Single-column manual contact entry form. Fields are grouped by purpose
 * (Identity / Classification / Location & matching / Consent) so staff can
 * scan a long form quickly. All copy is plain English; the underlying
 * enum codes (manual_entry, etc.) are mapped to human labels via
 * `lib/vocab`.
 */
export function ManualContactForm() {
  const ctx = useApiContext();
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
            `${r.created ? "Created" : "Updated existing"} contact${
              r.match_id ? " · match queued for review" : ""
            }.`,
          );
          if (r.created) {
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
      <Link
        href="/contacts"
        className="inline-flex items-center text-sm text-slate-600 hover:text-slate-900"
      >
        ← Back to contacts
      </Link>

      <div>
        <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
          Add a contact
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          Use this when an adopter or facilitator arrives outside the public
          form — a phone call, walk-in, partner event, or referral.
        </p>
      </div>

      <form
        className="space-y-6"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <FormSection
          title="Identity"
          description="Who is this person?"
        >
          <Field label="Display name" htmlFor="displayName" required>
            <input
              id="displayName"
              className="form-input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </Field>
          <Field label="Email" htmlFor="email" required>
            <input
              id="email"
              type="email"
              className="form-input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </Field>
        </FormSection>

        <FormSection
          title="Classification"
          description="How did they come in, and what's their role?"
        >
          <Field label="Party kind" htmlFor="partyKind">
            <select
              id="partyKind"
              className="form-input"
              value={partyKind}
              onChange={(e) =>
                setPartyKind(e.target.value as "adopter" | "facilitator")
              }
            >
              {PARTY_KIND_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Origin" htmlFor="origin">
            <select
              id="origin"
              className="form-input"
              value={origin}
              onChange={(e) => setOrigin(e.target.value)}
            >
              {ORIGIN_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
        </FormSection>

        <FormSection
          title="Location & matching"
          description="Country and people-group hints for the matcher (optional)."
        >
          <Field
            label="Country (ISO 2)"
            htmlFor="countryCode"
            help="Two-letter code, e.g. US, IN, KE."
          >
            <input
              id="countryCode"
              maxLength={2}
              className="form-input uppercase"
              value={countryCode}
              onChange={(e) => setCountryCode(e.target.value.toUpperCase())}
            />
          </Field>
          <Field
            label="FPG codes"
            htmlFor="fpgCodes"
            help="Joshua Project ROP3 people-group codes, comma or space separated."
          >
            <input
              id="fpgCodes"
              className="form-input font-mono"
              value={fpgRop3sRaw}
              onChange={(e) => setFpgRop3sRaw(e.target.value)}
              placeholder="AAA01, AAA02"
            />
          </Field>
          <Field
            label="Notes"
            htmlFor="notes"
            help="Captured on this contact for staff context."
          >
            <textarea
              id="notes"
              className="form-input min-h-[72px]"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
            />
          </Field>
        </FormSection>

        <FormSection
          title="Consent"
          description="Communication preferences."
        >
          <label className="flex items-start gap-3 text-sm text-slate-700">
            <input
              type="checkbox"
              className="mt-1"
              checked={newsletterOptIn}
              onChange={(e) => setNewsletterOptIn(e.target.checked)}
            />
            <span>
              Newsletter opt-in.
              <span className="block text-xs text-slate-500">
                Recorded on the contact now; sync to the mailing platform is
                scheduled for a later release.
              </span>
            </span>
          </label>
        </FormSection>

        <div className="space-y-3">
          <button
            type="submit"
            disabled={isSubmitting || !displayName || !email}
            className="rounded-md bg-orange-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-orange-700 disabled:opacity-50"
          >
            {isSubmitting ? "Saving…" : "Create contact"}
          </button>

          {err ? (
            <div
              role="alert"
              className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900"
            >
              {err}
            </div>
          ) : null}
          {msg ? (
            <div
              role="status"
              className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900"
            >
              {msg}
            </div>
          ) : null}
        </div>
      </form>
    </div>
  );
}

function FormSection({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <header className="mb-4">
        <h2 className="font-heading text-base font-semibold text-slate-900">
          {title}
        </h2>
        {description ? (
          <p className="mt-0.5 text-xs text-slate-500">{description}</p>
        ) : null}
      </header>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function Field({
  label,
  htmlFor,
  required,
  help,
  children,
}: {
  label: string;
  htmlFor: string;
  required?: boolean;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label
        htmlFor={htmlFor}
        className="block text-sm font-medium text-slate-700"
      >
        {label}
        {required ? <span className="ml-0.5 text-orange-600">*</span> : null}
      </label>
      {children}
      {help ? <p className="text-xs text-slate-500">{help}</p> : null}
    </div>
  );
}
