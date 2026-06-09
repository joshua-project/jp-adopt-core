"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import {
  activateAdminFacilitatingOrg,
  addAdminCoverage,
  deactivateAdminFacilitatingOrg,
  formatApiError,
  getAdminFacilitatingOrg,
  patchAdminFacilitatingOrg,
  removeAdminCoverage,
} from "../lib/api-client";
import { BTN_DANGER, BTN_PRIMARY, BTN_SECONDARY } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";

type Detail =
  paths["/v1/admin/facilitating-orgs/{org_id}"]["get"]["responses"]["200"]["content"]["application/json"];

export function OrgDetail({ orgId }: { orgId: string }) {
  const ctx = useApiContext();
  const router = useRouter();
  const [data, setData] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  const load = useCallback(async () => {
    setErr(null);
    try {
      setData(await getAdminFacilitatingOrg(ctx, orgId));
    } catch (e) {
      setErr(formatApiError(e));
    }
  }, [ctx, orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!data) {
    return (
      <div className="text-sm text-slate-500">
        {err ?? "Loading org…"}{" "}
        <Link className="underline" href="/admin/orgs">
          ← back
        </Link>
      </div>
    );
  }

  const onDeactivate = async () => {
    if (!window.confirm(`Deactivate "${data.org.name}"?`)) return;
    setActionBusy(true);
    setErr(null);
    try {
      const updated = await deactivateAdminFacilitatingOrg(ctx, orgId);
      setData({ ...data, org: updated });
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setActionBusy(false);
    }
  };

  const onActivate = async () => {
    setActionBusy(true);
    setErr(null);
    try {
      const updated = await activateAdminFacilitatingOrg(ctx, orgId);
      setData({ ...data, org: updated });
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setActionBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <Link
          className="text-sm text-slate-600 hover:text-slate-900"
          href="/admin/orgs"
        >
          ← back to orgs
        </Link>
      </div>

      <MetaPanel org={data.org} onSaved={(o) => setData({ ...data, org: o })} />

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      <CoverageSection
        orgId={orgId}
        coverage={data.coverage}
        onChanged={load}
      />

      <MembershipsSection memberships={data.memberships} />

      <section className="flex flex-wrap gap-2 rounded border border-slate-200 bg-slate-50 p-4">
        {data.org.active ? (
          <button
            type="button"
            className={BTN_DANGER}
            disabled={actionBusy}
            onClick={onDeactivate}
          >
            {actionBusy ? "Working…" : "Deactivate"}
          </button>
        ) : (
          <button
            type="button"
            className={BTN_PRIMARY}
            disabled={actionBusy}
            onClick={onActivate}
          >
            {actionBusy ? "Working…" : "Activate"}
          </button>
        )}
      </section>
    </div>
  );
}

function MetaPanel({
  org,
  onSaved,
}: {
  org: Detail["org"];
  onSaved: (org: Detail["org"]) => void;
}) {
  const ctx = useApiContext();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(org.name);
  const [countryCode, setCountryCode] = useState(org.country_code ?? "");
  const [capacityTotal, setCapacityTotal] = useState(org.capacity_total);
  const [accepting, setAccepting] = useState(org.accepting_potential_adopters);
  const [isTriage, setIsTriage] = useState(org.is_triage_org);
  const [err, setErr] = useState<string | null>(null);
  const [busy, startSave] = useTransition();

  const startEdit = () => {
    setName(org.name);
    setCountryCode(org.country_code ?? "");
    setCapacityTotal(org.capacity_total);
    setAccepting(org.accepting_potential_adopters);
    setIsTriage(org.is_triage_org);
    setErr(null);
    setEditing(true);
  };

  const onSave = () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setErr("Name is required.");
      return;
    }
    setErr(null);
    startSave(() => {
      void (async () => {
        try {
          const updated = await patchAdminFacilitatingOrg(ctx, org.id, {
            name: trimmed,
            country_code: countryCode.trim() || null,
            capacity_total: capacityTotal,
            accepting_potential_adopters: accepting,
            is_triage_org: isTriage,
          });
          onSaved(updated);
          setEditing(false);
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  if (!editing) {
    return (
      <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
        <div className="flex items-start justify-between gap-3">
          <h2 className="font-heading text-xl font-semibold text-slate-900">
            {org.name}
          </h2>
          <span
            className={
              "rounded-full px-2 py-0.5 text-[11px] font-medium " +
              (org.active
                ? "bg-emerald-50 text-emerald-800 border border-emerald-200"
                : "bg-slate-100 text-slate-600 border border-slate-200")
            }
          >
            {org.active ? "Active" : "Inactive"}
          </span>
        </div>
        <dl className="grid grid-cols-2 gap-3 text-xs text-slate-600 sm:grid-cols-4">
          <Field label="Country">{org.country_code ?? "—"}</Field>
          <Field label="Capacity total">{org.capacity_total}</Field>
          <Field label="Committed">{org.capacity_committed}</Field>
          <Field label="Remaining">{org.capacity_remaining}</Field>
          <Field label="Triage">{org.is_triage_org ? "Yes" : "No"}</Field>
          <Field label="Accepting potential">
            {org.accepting_potential_adopters ? "Yes" : "No"}
          </Field>
        </dl>
        <button type="button" className={BTN_SECONDARY} onClick={startEdit}>
          Edit
        </button>
      </section>
    );
  }

  return (
    <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-medium text-slate-700">Edit org</h2>
      <label className="block text-xs text-slate-600">
        Name
        <input
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          value={name}
          maxLength={512}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block text-xs text-slate-600">
          Country code
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={countryCode}
            maxLength={8}
            onChange={(e) => setCountryCode(e.target.value)}
          />
        </label>
        <label className="block text-xs text-slate-600">
          Capacity total
          <input
            type="number"
            min={org.capacity_committed}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={capacityTotal}
            onChange={(e) =>
              setCapacityTotal(Math.max(0, Number(e.target.value) || 0))
            }
          />
        </label>
      </div>
      <label className="flex items-center gap-2 text-xs text-slate-600">
        <input
          type="checkbox"
          checked={accepting}
          onChange={(e) => setAccepting(e.target.checked)}
        />
        Accepting potential (no-FPG) adopters
      </label>
      <label className="flex items-center gap-2 text-xs text-slate-600">
        <input
          type="checkbox"
          checked={isTriage}
          onChange={(e) => setIsTriage(e.target.checked)}
        />
        Triage org (only one allowed)
      </label>
      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}
      <div className="flex gap-2">
        <button
          type="button"
          className={BTN_PRIMARY}
          disabled={busy || !name.trim()}
          onClick={onSave}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className={BTN_SECONDARY}
          disabled={busy}
          onClick={() => setEditing(false)}
        >
          Cancel
        </button>
      </div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-slate-800">{children}</dd>
    </div>
  );
}

function CoverageSection({
  orgId,
  coverage,
  onChanged,
}: {
  orgId: string;
  coverage: Detail["coverage"];
  onChanged: () => void;
}) {
  const ctx = useApiContext();
  const [adding, setAdding] = useState(false);
  const [rop3, setRop3] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, startSubmit] = useTransition();
  const [removingRop3, setRemovingRop3] = useState<string | null>(null);

  const onAdd = () => {
    const trimmed = rop3.trim().toUpperCase();
    if (!trimmed) return;
    setErr(null);
    startSubmit(() => {
      void (async () => {
        try {
          await addAdminCoverage(ctx, orgId, trimmed);
          setRop3("");
          setAdding(false);
          onChanged();
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  const onRemove = async (peopleId3: string) => {
    if (!window.confirm(`Remove coverage for ${peopleId3}?`)) return;
    setRemovingRop3(peopleId3);
    setErr(null);
    try {
      await removeAdminCoverage(ctx, orgId, peopleId3);
      onChanged();
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setRemovingRop3(null);
    }
  };

  return (
    <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h2 className="font-heading text-base font-semibold text-slate-900">
          FPG coverage
        </h2>
        <span className="text-xs text-slate-500">
          {coverage.length === 0
            ? "No coverage"
            : `${coverage.length} FPG${coverage.length === 1 ? "" : "s"}`}
        </span>
      </div>
      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}
      {coverage.length > 0 ? (
        <ul className="divide-y divide-slate-100 rounded border border-slate-200">
          {coverage.map((c) => (
            <li
              key={c.people_id3}
              className="flex items-center justify-between gap-3 px-4 py-2 text-sm"
            >
              <div>
                <span className="font-mono text-xs text-slate-500">
                  {c.people_id3}
                </span>
                {c.name ? (
                  <span className="ml-2 text-slate-800">{c.name}</span>
                ) : null}
                {c.country_code ? (
                  <span className="ml-1 text-xs text-slate-500">
                    · {c.country_code}
                  </span>
                ) : null}
              </div>
              <button
                type="button"
                disabled={removingRop3 === c.people_id3}
                onClick={() => onRemove(c.people_id3)}
                className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {removingRop3 === c.people_id3 ? "Removing…" : "Remove"}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-500">
          Add an FPG below to start matching this org against adopters.
        </p>
      )}
      {adding ? (
        <div className="flex gap-2">
          <input
            value={rop3}
            onChange={(e) => setRop3(e.target.value)}
            placeholder="ROP3 (e.g. AAB)"
            className="flex-1 rounded border border-slate-300 px-2 py-1.5 text-sm font-mono"
            maxLength={16}
          />
          <button
            type="button"
            className={BTN_PRIMARY}
            disabled={busy || !rop3.trim()}
            onClick={onAdd}
          >
            {busy ? "Adding…" : "Add"}
          </button>
          <button
            type="button"
            className={BTN_SECONDARY}
            disabled={busy}
            onClick={() => {
              setAdding(false);
              setRop3("");
              setErr(null);
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          className={BTN_PRIMARY}
          onClick={() => setAdding(true)}
        >
          + Add FPG
        </button>
      )}
    </section>
  );
}

function MembershipsSection({
  memberships,
}: {
  memberships: Detail["memberships"];
}) {
  return (
    <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h2 className="font-heading text-base font-semibold text-slate-900">
          Memberships
        </h2>
        <span className="text-xs text-slate-500">
          {memberships.length === 0
            ? "No members"
            : `${memberships.length} member${memberships.length === 1 ? "" : "s"}`}
        </span>
      </div>
      {memberships.length > 0 ? (
        <ul className="divide-y divide-slate-100 rounded border border-slate-200">
          {memberships.map((m) => (
            <li
              key={`${m.user_subject_id}-${m.facilitator_org_id}`}
              className="flex items-center justify-between gap-3 px-4 py-2 text-sm"
            >
              <span className="font-mono text-xs text-slate-600">
                {m.user_subject_id}
              </span>
              <span className="text-xs text-slate-500">{m.role_in_org}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-500">
          Memberships are added via the existing facilitator-membership admin
          API; surfacing add/remove UI here is tracked separately.
        </p>
      )}
    </section>
  );
}
