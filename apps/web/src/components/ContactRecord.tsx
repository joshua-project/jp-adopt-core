"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import type { ReactNode } from "react";

import type { paths } from "@jp-adopt/contracts";

import {
  apiFetch,
  enrollInCampaign,
  formatApiError,
  listCampaigns,
  sendContactEmail,
} from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import {
  formatTimestamp,
  humanize,
  humanizeOrigin,
  humanizePartyKind,
  humanizeReasonCode,
  humanizeStatus,
} from "../lib/vocab";

/** Format a date-only value (YYYY-MM-DD) without the new Date() UTC→local
 * day-shift that bites users in negative time zones. */
function fmtDateOnly(value: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!m) return value;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).toLocaleDateString(undefined, {
    timeZone: "UTC",
    dateStyle: "medium",
  });
}
import { CodeChip, StatusBadge } from "./StatusBadge";

type Contact =
  paths["/v1/contacts/{contact_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type Matches =
  paths["/v1/contacts/{contact_id}/matches"]["get"]["responses"]["200"]["content"]["application/json"];
type Transitions =
  paths["/v1/contacts/{contact_id}/transitions"]["get"]["responses"]["200"]["content"]["application/json"];
type Activity =
  paths["/v1/contacts/{contact_id}/activity"]["get"]["responses"]["200"]["content"]["application/json"];
type Enrollments =
  paths["/v1/contacts/{contact_id}/enrollments"]["get"]["responses"]["200"]["content"]["application/json"];
type Profile = NonNullable<Contact["profile"]>;

const BTN =
  "rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50";
const INPUT = "w-full rounded border border-slate-300 px-2 py-1 text-sm";

type Input = "text" | "textarea" | "enum" | "bool" | "date" | "number" | "list" | "readonly";
type FieldDef = readonly [keyof Profile, string, Input];
type TileDef = {
  title: string;
  kinds: ReadonlyArray<"adopter" | "facilitator">;
  fields: readonly FieldDef[];
};

const ENUM_OPTIONS: Partial<Record<keyof Profile, readonly string[]>> = {
  adopter_type: ["individual", "small_group", "church", "organization", "network"],
  entity_size: ["1", "lt_30", "31_100", "101_500", "501_2000", "2001_plus"],
  preferred_communication: ["email", "phone"],
  mou_status: ["signed", "not_required", "not_sent"],
};

const PROFILE_TILES: readonly TileDef[] = [
  {
    title: "Contact information",
    kinds: ["adopter", "facilitator"],
    fields: [
      ["primary_contact_name", "Primary contact", "text"],
      ["secondary_contact_name", "Secondary contact", "text"],
      ["secondary_contact_email", "Secondary email", "text"],
      ["secondary_contact_phone", "Secondary phone", "text"],
      ["website", "Website", "text"],
      ["preferred_communication", "Preferred contact", "enum"],
      ["form_country", "Country (as submitted)", "text"],
      ["form_state_region", "State / region", "text"],
    ],
  },
  {
    title: "Adoption profile",
    kinds: ["adopter"],
    fields: [
      ["adopter_type", "Adopter type", "enum"],
      ["entity_size", "Entity size", "enum"],
      ["commitment_types", "Commitment types", "list"],
      ["commitment_date", "Commitment date", "date"],
      ["ministry_areas", "Ministry areas", "list"],
    ],
  },
  {
    title: "Facilitation profile",
    kinds: ["facilitator"],
    fields: [
      ["works_with_fpgs", "Works with FPGs", "bool"],
      ["willing_to_facilitate", "Willing to facilitate", "bool"],
      ["facilitation_entity_types", "Facilitation entity types", "list"],
      ["facilitation_entity_sizes", "Facilitation entity sizes", "list"],
      ["mou_status", "MOU status", "enum"],
      ["mou_signature_name", "MOU signature", "text"],
    ],
  },
  {
    title: "Connection preferences",
    kinds: ["adopter"],
    fields: [
      ["want_facilitator_connection", "Wants facilitator connection", "bool"],
      ["facilitator_entity_types", "Facilitator entity types", "list"],
      ["desired_facilitator_info", "Desired facilitator activities", "list"],
    ],
  },
  {
    title: "Network & capacity",
    kinds: ["facilitator"],
    fields: [
      ["want_network_connection", "Wants network connection", "bool"],
      ["network_partner_info", "Network partnership", "list"],
    ],
  },
  {
    title: "Vetting & compliance",
    kinds: ["adopter", "facilitator"],
    fields: [
      ["has_doctrinal_distinctives", "Has doctrinal distinctives", "bool"],
      ["doctrinal_distinctives", "Doctrinal distinctives", "textarea"],
      ["has_accountability_membership", "Has accountability membership", "bool"],
      ["accountability_memberships", "Accountability memberships", "textarea"],
    ],
  },
  {
    title: "Engagement",
    kinds: ["adopter", "facilitator"],
    fields: [
      ["engagement_score", "Engagement score", "number"],
      ["last_contact_date", "Last contact", "date"],
      ["next_followup_date", "Next follow-up", "date"],
    ],
  },
  {
    title: "Form submission",
    kinds: ["adopter", "facilitator"],
    fields: [
      ["referral_source", "Referral source", "readonly"],
      ["campaign", "Campaign", "readonly"],
      ["partner", "Partner", "readonly"],
      ["additional_notes", "Additional notes", "textarea"],
    ],
  },
];

const EDITABLE = PROFILE_TILES.flatMap((t) =>
  t.fields.filter((f) => f[2] !== "readonly"),
);

function fmtRead(value: unknown, input: Input): ReactNode {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    return value.length ? value.map((v) => humanize(String(v))).join(", ") : "—";
  }
  if (input === "date") return fmtDateOnly(String(value));
  if (input === "enum") return humanize(String(value));
  return String(value);
}

/** profile value → string for an edit-mode input. */
function toDraft(value: unknown, input: Input): string {
  if (value === null || value === undefined) return "";
  if (input === "list" && Array.isArray(value)) return value.join(", ");
  if (input === "bool") return value === true ? "true" : value === false ? "false" : "";
  return String(value);
}

/** draft string → typed value for the PATCH (null clears the field). */
function fromDraft(raw: string, input: Input): unknown {
  const t = raw.trim();
  if (input === "list") {
    const arr = t.split(",").map((s) => s.trim()).filter(Boolean);
    return arr.length ? arr : null;
  }
  if (input === "bool") return t === "" ? null : t === "true";
  if (input === "number") return t === "" ? null : Number(t);
  return t === "" ? null : t;
}

function Tile({ title, count, action, children }: {
  title: string; count?: number; action?: ReactNode; children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700">
        {title}
        {count !== undefined ? (
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
            {count}
          </span>
        ) : null}
        {action ? <span className="ml-auto">{action}</span> : null}
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
  const [enrollments, setEnrollments] = useState<Enrollments | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const [noteBody, setNoteBody] = useState("");
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);

  const [editingProfile, setEditingProfile] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});

  // F3: send-email modal
  const [emailOpen, setEmailOpen] = useState(false);
  const [emailSubject, setEmailSubject] = useState("");
  const [emailBody, setEmailBody] = useState("");
  const [emailSecondary, setEmailSecondary] = useState(false);
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailErr, setEmailErr] = useState<string | null>(null);

  // U7: manual-enroll affordance
  const [enrollOpen, setEnrollOpen] = useState(false);
  const [activeCampaigns, setActiveCampaigns] = useState<
    Array<{ id: string; name: string }>
  >([]);
  const [enrollCampaignId, setEnrollCampaignId] = useState("");
  const [enrollBusy, setEnrollBusy] = useState(false);
  const [enrollErr, setEnrollErr] = useState<string | null>(null);
  const [enrollMsg, setEnrollMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [c, m, t, a, en] = await Promise.all([
        apiFetch<Contact>(ctx, `/v1/contacts/${contactId}`),
        apiFetch<Matches>(ctx, `/v1/contacts/${contactId}/matches`),
        apiFetch<Transitions>(ctx, `/v1/contacts/${contactId}/transitions`),
        apiFetch<Activity>(ctx, `/v1/contacts/${contactId}/activity`),
        apiFetch<Enrollments>(ctx, `/v1/contacts/${contactId}/enrollments`),
      ]);
      if (!c) {
        setErr("Contact not found");
        return;
      }
      setContact(c);
      setMatches(m ?? null);
      setTransitions(t ?? null);
      setActivity(a ?? null);
      setEnrollments(en ?? null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load contact");
    }
  }, [ctx, contactId]);

  useEffect(() => {
    void load();
  }, [load]);

  const addNote = useCallback(async () => {
    const text = noteBody.trim();
    if (!text) return;
    setBusy(true);
    setActionErr(null);
    try {
      await apiFetch(ctx, `/v1/contacts/${contactId}/activity`, {
        method: "POST",
        body: { body: text },
      });
      setNoteBody("");
      await load();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : "Failed to add note");
    } finally {
      setBusy(false);
    }
  }, [ctx, contactId, noteBody, load]);

  const openEnroll = useCallback(async () => {
    setEnrollErr(null);
    setEnrollMsg(null);
    setEnrollOpen(true);
    try {
      const res = await listCampaigns(ctx);
      const active = res.items
        .filter((c) => c.status === "active")
        .map((c) => ({ id: c.id, name: c.name }));
      setActiveCampaigns(active);
      if (active.length > 0 && !enrollCampaignId) {
        setEnrollCampaignId(active[0].id);
      }
    } catch (e) {
      setEnrollErr(formatApiError(e));
    }
  }, [ctx, enrollCampaignId]);

  const submitEnroll = useCallback(async () => {
    if (!enrollCampaignId) return;
    setEnrollBusy(true);
    setEnrollErr(null);
    setEnrollMsg(null);
    try {
      const res = await enrollInCampaign(ctx, enrollCampaignId, contactId);
      if (res.reason === "created") {
        setEnrollMsg("Enrolled.");
        setEnrollOpen(false);
        await load();
      } else {
        const phrase =
          res.reason === "already_enrolled"
            ? "Contact is already enrolled in this campaign."
            : res.reason === "suppressed"
              ? "Contact email is on the suppression list."
              : res.reason === "do_not_engage"
                ? "Contact is opted out — cannot enroll."
                : `Not enrolled: ${humanize(res.reason)}`;
        setEnrollErr(phrase);
      }
    } catch (e) {
      setEnrollErr(formatApiError(e));
    } finally {
      setEnrollBusy(false);
    }
  }, [ctx, enrollCampaignId, contactId, load]);

  const sendEmail = useCallback(async () => {
    const subject = emailSubject.trim();
    const body = emailBody.trim();
    if (!subject || !body) return;
    setEmailBusy(true);
    setEmailErr(null);
    try {
      await sendContactEmail(ctx, contactId, {
        subject,
        body,
        include_secondary: emailSecondary,
      });
      setEmailOpen(false);
      setEmailSubject("");
      setEmailBody("");
      setEmailSecondary(false);
      // Refresh so the sent email appears as an `email` note on the timeline.
      await load();
    } catch (e) {
      setEmailErr(e instanceof Error ? e.message : "Failed to send email");
    } finally {
      setEmailBusy(false);
    }
  }, [ctx, contactId, emailSubject, emailBody, emailSecondary, load]);

  const saveName = useCallback(async () => {
    const name = nameDraft.trim();
    if (!name) return;
    setBusy(true);
    setActionErr(null);
    try {
      await apiFetch(ctx, `/v1/contacts/${contactId}`, {
        method: "PATCH",
        body: { display_name: name },
      });
      setEditingName(false);
      await load();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : "Failed to save name");
    } finally {
      setBusy(false);
    }
  }, [ctx, contactId, nameDraft, load]);

  const assignToMe = useCallback(async () => {
    setBusy(true);
    setActionErr(null);
    try {
      await apiFetch(ctx, `/v1/contacts/${contactId}/assignment`, {
        method: "PUT",
        body: {},
      });
      await load();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : "Failed to assign");
    } finally {
      setBusy(false);
    }
  }, [ctx, contactId, load]);

  const unassign = useCallback(async () => {
    setBusy(true);
    setActionErr(null);
    try {
      await apiFetch(ctx, `/v1/contacts/${contactId}/assignment`, {
        method: "DELETE",
      });
      await load();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : "Failed to unassign");
    } finally {
      setBusy(false);
    }
  }, [ctx, contactId, load]);

  const startEditProfile = useCallback(() => {
    const p = (contact?.profile ?? {}) as Partial<Profile>;
    const d: Record<string, string> = {};
    for (const [key, , input] of EDITABLE) {
      d[key as string] = toDraft(p[key], input);
    }
    setDraft(d);
    setActionErr(null);
    setEditingProfile(true);
  }, [contact]);

  const saveProfile = useCallback(async () => {
    const original = (contact?.profile ?? {}) as Partial<Profile>;
    const patch: Record<string, unknown> = {};
    for (const [key, label, input] of EDITABLE) {
      const rawDraft = draft[key as string] ?? "";
      // Block save on a non-numeric entry rather than silently clearing the
      // field (NaN → JSON null would otherwise wipe it).
      if (
        input === "number" &&
        rawDraft.trim() !== "" &&
        !Number.isFinite(Number(rawDraft))
      ) {
        setActionErr(`${label} must be a number.`);
        return;
      }
      const next = fromDraft(rawDraft, input);
      const prev = original[key] ?? null;
      if (JSON.stringify(next) !== JSON.stringify(prev)) {
        patch[key as string] = next;
      }
    }
    if (Object.keys(patch).length === 0) {
      setEditingProfile(false);
      return;
    }
    setBusy(true);
    setActionErr(null);
    try {
      await apiFetch(ctx, `/v1/contacts/${contactId}`, {
        method: "PATCH",
        body: { profile: patch },
      });
      setEditingProfile(false);
      await load();
    } catch (e) {
      setActionErr(e instanceof Error ? e.message : "Failed to save profile");
    } finally {
      setBusy(false);
    }
  }, [ctx, contactId, contact, draft, load]);

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
  const profile = contact.profile;
  const partyKind = isAdopter ? "adopter" : "facilitator";

  const interestMap = new Map<string, { name: string | null; country: string | null }>();
  for (const m of matches?.items ?? []) {
    if (m.people_id3 && !interestMap.has(m.people_id3)) {
      interestMap.set(m.people_id3, { name: m.people_id3_name ?? null, country: m.people_id3_country ?? null });
    }
  }
  const interests = [...interestMap.entries()];

  function renderInput([key, , input]: FieldDef) {
    const k = key as string;
    const val = draft[k] ?? "";
    const set = (v: string) => setDraft((d) => ({ ...d, [k]: v }));
    if (input === "enum") {
      return (
        <select className={INPUT} value={val} onChange={(e) => set(e.target.value)}>
          <option value="">—</option>
          {(ENUM_OPTIONS[key] ?? []).map((o) => (
            <option key={o} value={o}>{humanize(o)}</option>
          ))}
        </select>
      );
    }
    if (input === "bool") {
      return (
        <select className={INPUT} value={val} onChange={(e) => set(e.target.value)}>
          <option value="">—</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      );
    }
    if (input === "textarea") {
      return <textarea className={INPUT} rows={2} value={val} onChange={(e) => set(e.target.value)} />;
    }
    return (
      <input
        className={INPUT}
        type={input === "date" ? "date" : input === "number" ? "number" : "text"}
        step={input === "number" ? 1 : undefined}
        value={val}
        placeholder={input === "list" ? "comma, separated" : undefined}
        onChange={(e) => set(e.target.value)}
      />
    );
  }

  return (
    <div className="space-y-6">
      <Link href="/contacts" className="text-sm text-slate-600 hover:text-slate-900">
        ← back to contacts
      </Link>

      {/* Header */}
      <header className="flex flex-wrap items-start justify-between gap-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <div className="space-y-2">
          {editingName ? (
            <div className="flex items-center gap-2">
              <input
                className="rounded border border-slate-300 px-2 py-1 text-lg"
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                autoFocus
              />
              <button type="button" className={BTN} disabled={busy} onClick={saveName}>Save</button>
              <button type="button" className={BTN} disabled={busy} onClick={() => setEditingName(false)}>
                Cancel
              </button>
            </div>
          ) : (
            <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
              {contact.display_name}
            </h1>
          )}
          <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
              {humanizePartyKind(contact.party_kind)}
            </span>
            <StatusBadge status={status ?? undefined} kind={statusKind} />
            {contact.email_normalized ? <span>{contact.email_normalized}</span> : null}
            {contact.country_code ? <CodeChip>{contact.country_code}</CodeChip> : null}
            {(contact.language_codes ?? []).map((l) => <CodeChip key={l}>{l}</CodeChip>)}
          </div>
          <p className="text-xs text-slate-400">
            Origin: {humanizeOrigin(contact.origin)} · updated {formatTimestamp(contact.updated_at)}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/workflow/${contactId}`} className={BTN}>Transition</Link>
            <button
              type="button"
              className={BTN}
              disabled={busy}
              onClick={() => { setNameDraft(contact.display_name); setEditingName(true); }}
            >
              Edit name
            </button>
            {contact.assigned_to ? (
              <button type="button" className={BTN} disabled={busy} onClick={unassign}>
                Unassign
              </button>
            ) : (
              <button type="button" className={BTN} disabled={busy} onClick={assignToMe}>
                Assign to me
              </button>
            )}
          </div>
          <p className="text-xs text-slate-500">
            {contact.assigned_to ? (
              <>Assigned to <span className="font-medium text-slate-700">{contact.assigned_to}</span></>
            ) : (
              "Unassigned"
            )}
          </p>
        </div>
      </header>

      {actionErr ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {actionErr}
        </div>
      ) : null}

      {/* Read tiles */}
      <div className="grid gap-4 lg:grid-cols-2">
        {isAdopter ? (
          <Tile title="People-group interests" count={interests.length}>
            {interests.length ? (
              <ul className="space-y-1.5 text-sm">
                {interests.map(([peopleId3, info]) => (
                  <li key={peopleId3} className="flex items-center gap-2">
                    <span className="text-slate-800">{info.name ?? "Unknown people group"}</span>
                    {info.country ? <CodeChip>{info.country}</CodeChip> : null}
                    <CodeChip>{peopleId3}</CodeChip>
                  </li>
                ))}
              </ul>
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
                  <span className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={m.status} kind="match" />
                    <span className="text-slate-700">{m.facilitator_name}</span>
                    {m.people_id3_name ? <span className="text-slate-500">· {m.people_id3_name}</span> : null}
                    {m.people_id3_country ? <CodeChip>{m.people_id3_country}</CodeChip> : null}
                    {m.people_id3 ? <CodeChip>{m.people_id3}</CodeChip> : null}
                  </span>
                  <span className="shrink-0 text-xs text-slate-400">
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
          <div className="mb-3 space-y-2">
            <textarea
              className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
              rows={2}
              placeholder="Add a note…"
              value={noteBody}
              onChange={(e) => setNoteBody(e.target.value)}
            />
            <div className="flex gap-2">
              <button type="button" className={BTN} disabled={busy || !noteBody.trim()} onClick={addNote}>
                {busy ? "Saving…" : "Add note"}
              </button>
              <button
                type="button"
                className={BTN}
                disabled={!contact?.email_normalized}
                title={
                  contact?.email_normalized
                    ? undefined
                    : "Contact has no email address"
                }
                onClick={() => {
                  setEmailErr(null);
                  setEmailOpen(true);
                }}
              >
                Send email
              </button>
            </div>
          </div>
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

        <Tile
          title="Drip enrollments"
          count={enrollments?.total ?? 0}
          action={
            <button type="button" className={BTN} onClick={openEnroll}>
              Manual enroll
            </button>
          }
        >
          {enrollMsg ? (
            <div className="mb-2 rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs text-emerald-900">
              {enrollMsg}
            </div>
          ) : null}
          {enrollments?.items.length ? (
            <ul className="space-y-2 text-sm">
              {enrollments.items.map((en) => (
                <li key={en.id} className="space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
                        {humanize(en.state)}
                      </span>
                      <span className="text-slate-700">{en.campaign_name}</span>
                      {en.exit_reason ? (
                        <span className="text-slate-400">· {humanize(en.exit_reason)}</span>
                      ) : null}
                    </span>
                    <span className="shrink-0 text-xs text-slate-400">
                      step {en.current_step_position}
                      {en.last_step_sent_at
                        ? ` · last sent ${formatTimestamp(en.last_step_sent_at)}`
                        : " · not sent yet"}
                    </span>
                  </div>
                  {en.events && en.events.length > 0 ? (
                    <details className="ml-1 text-xs text-slate-500">
                      <summary className="cursor-pointer hover:text-slate-700">
                        {en.events.length} event{en.events.length === 1 ? "" : "s"}
                      </summary>
                      <ul className="mt-1 space-y-0.5 pl-3">
                        {en.events.map((ev, i) => (
                          <li key={i} className="flex items-baseline justify-between gap-2">
                            <span className="font-mono">{ev.event_type}</span>
                            <span className="text-slate-400">
                              {formatTimestamp(ev.created_at)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No drip enrollments.</Empty>
          )}
        </Tile>
      </div>

      {/* Adoption-profile tiles (U11) with inline edit. */}
      <div className="flex items-center justify-between">
        <h2 className="font-heading text-lg font-semibold text-slate-800">Adoption profile</h2>
        {editingProfile ? (
          <div className="flex gap-2">
            <button type="button" className={BTN} disabled={busy} onClick={saveProfile}>
              {busy ? "Saving…" : "Save profile"}
            </button>
            <button type="button" className={BTN} disabled={busy} onClick={() => setEditingProfile(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <button type="button" className={BTN} disabled={busy} onClick={startEditProfile}>
            Edit profile
          </button>
        )}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {PROFILE_TILES.filter((t) => t.kinds.includes(partyKind)).map((tile) => (
          <Tile key={tile.title} title={tile.title}>
            {editingProfile ? (
              <div className="space-y-2">
                {tile.fields.map((f) => (
                  <label key={f[0] as string} className="block text-xs text-slate-500">
                    {f[1]}
                    {f[2] === "readonly" ? (
                      <div className="mt-0.5 text-sm text-slate-700">
                        {fmtRead(profile?.[f[0]], f[2])} <span className="text-slate-400">(set at intake)</span>
                      </div>
                    ) : (
                      <div className="mt-0.5">{renderInput(f)}</div>
                    )}
                  </label>
                ))}
              </div>
            ) : profile ? (
              <div className="divide-y divide-slate-100">
                {tile.fields.map((f) => (
                  <div key={f[0] as string} className="flex items-baseline justify-between gap-3 py-1 text-sm">
                    <span className="shrink-0 text-slate-500">{f[1]}</span>
                    <span className="text-right text-slate-800">{fmtRead(profile[f[0]], f[2])}</span>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>No profile data captured yet.</Empty>
            )}
          </Tile>
        ))}
      </div>

      {/* U7: manual-enroll modal */}
      {enrollOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-md space-y-3 rounded-lg bg-white p-5 shadow-xl">
            <h3 className="text-base font-semibold text-slate-900">
              Enroll {contact?.display_name} in a campaign
            </h3>
            {activeCampaigns.length === 0 ? (
              <p className="text-sm text-slate-600">
                No active campaigns. Activate one on{" "}
                <Link href="/campaigns" className="underline">
                  Campaigns
                </Link>{" "}
                first.
              </p>
            ) : (
              <label className="block text-xs text-slate-600">
                Campaign
                <select
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  value={enrollCampaignId}
                  onChange={(e) => setEnrollCampaignId(e.target.value)}
                >
                  {activeCampaigns.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {enrollErr ? (
              <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
                {enrollErr}
              </div>
            ) : null}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className={BTN}
                disabled={enrollBusy}
                onClick={() => setEnrollOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
                disabled={enrollBusy || !enrollCampaignId}
                onClick={submitEnroll}
              >
                {enrollBusy ? "Enrolling…" : "Enroll"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* F3: send-email modal */}
      {emailOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
          <div className="w-full max-w-lg space-y-3 rounded-lg bg-white p-5 shadow-xl">
            <h3 className="text-base font-semibold text-slate-900">
              Email {contact?.display_name}
            </h3>
            <p className="text-xs text-slate-500">
              To: {contact?.email_normalized}
            </p>
            <label className="block text-xs text-slate-600">
              Subject
              <input
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
                value={emailSubject}
                maxLength={512}
                onChange={(e) => setEmailSubject(e.target.value)}
              />
            </label>
            <label className="block text-xs text-slate-600">
              Message
              <textarea
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
                rows={6}
                value={emailBody}
                onChange={(e) => setEmailBody(e.target.value)}
              />
            </label>
            {partyKind === "facilitator" ? (
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={emailSecondary}
                  onChange={(e) => setEmailSecondary(e.target.checked)}
                />
                Also send to secondary contact
              </label>
            ) : null}
            {emailErr ? (
              <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
                {emailErr}
              </div>
            ) : null}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className={BTN}
                disabled={emailBusy}
                onClick={() => setEmailOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
                disabled={emailBusy || !emailSubject.trim() || !emailBody.trim()}
                onClick={sendEmail}
              >
                {emailBusy ? "Sending…" : "Send email"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
