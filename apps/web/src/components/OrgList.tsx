"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  createAdminFacilitatingOrg,
  formatApiError,
  listAdminFacilitatingOrgs,
} from "../lib/api-client";
import { BTN, BTN_PRIMARY, BTN_SECONDARY } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";

type Org =
  paths["/v1/admin/facilitating-orgs"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];

export function OrgList() {
  const ctx = useApiContext();
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [items, setItems] = useState<Org[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setForbidden(false);
    try {
      const res = await listAdminFacilitatingOrgs(ctx);
      setItems(res.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
        setItems([]);
        return;
      }
      setErr(formatApiError(e));
    } finally {
      setLoading(false);
    }
  }, [ctx]);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = search.trim()
    ? items.filter((o) =>
        o.name.toLowerCase().includes(search.trim().toLowerCase()),
      )
    : items;

  if (forbidden) {
    return (
      <EmptyState
        title="You can't manage facilitating orgs"
        description="Org administration is gated to staff_admin."
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name"
          className="w-full max-w-sm rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <button
          type="button"
          className={BTN_PRIMARY}
          onClick={() => setShowNew(true)}
        >
          + New org
        </button>
      </div>

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      <DataTable
        rows={
          loading ? (
            <LoadingRows />
          ) : filtered.length === 0 ? null : (
            filtered.map((o) => {
              const meta = [
                o.country_code ? `Country: ${o.country_code}` : null,
                `Capacity: ${o.capacity_committed}/${o.capacity_total}`,
                o.is_triage_org ? "Triage" : null,
                o.accepting_potential_adopters ? "Accepting potential" : null,
              ].filter(Boolean);
              return (
                <DataRow
                  key={o.id}
                  id={o.id}
                  title={
                    <Link
                      href={`/admin/orgs/${o.id}`}
                      className="hover:underline"
                    >
                      {o.name}
                    </Link>
                  }
                  badge={
                    <span
                      className={
                        "rounded-full px-2 py-0.5 text-[11px] font-medium " +
                        (o.active
                          ? "bg-emerald-50 text-emerald-800 border border-emerald-200"
                          : "bg-slate-100 text-slate-600 border border-slate-200")
                      }
                    >
                      {o.active ? "Active" : "Inactive"}
                    </span>
                  }
                  meta={
                    <span className="text-xs text-slate-500">
                      {meta.join(" · ")}
                    </span>
                  }
                  subtle={`Updated ${new Date(o.updated_at).toLocaleString()}`}
                  action={
                    <Link
                      href={`/admin/orgs/${o.id}`}
                      className="text-xs font-medium text-jp-accent"
                    >
                      Edit
                    </Link>
                  }
                />
              );
            })
          )
        }
        empty={
          <EmptyState
            title="No facilitating orgs yet"
            description='Click "+ New org" to add the first one.'
          />
        }
      />

      {showNew ? (
        <NewOrgModal
          onClose={() => setShowNew(false)}
          onCreated={(id) => {
            setShowNew(false);
            void load();
            router.push(`/admin/orgs/${id}`);
          }}
        />
      ) : null}
    </div>
  );
}

function NewOrgModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const ctx = useApiContext();
  const [name, setName] = useState("");
  const [countryCode, setCountryCode] = useState("");
  const [capacityTotal, setCapacityTotal] = useState(0);
  const [accepting, setAccepting] = useState(false);
  const [isTriage, setIsTriage] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, startSubmit] = useTransition();

  const onSubmit = () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setErr("Name is required.");
      return;
    }
    setErr(null);
    startSubmit(() => {
      void (async () => {
        try {
          const org = await createAdminFacilitatingOrg(ctx, {
            name: trimmed,
            country_code: countryCode.trim() || null,
            capacity_total: capacityTotal,
            accepting_potential_adopters: accepting,
            is_triage_org: isTriage,
          });
          onCreated(org.id);
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-lg space-y-3 rounded-lg bg-white p-5 shadow-xl">
        <h3 className="text-base font-semibold text-slate-900">
          New facilitating org
        </h3>
        <label className="block text-xs text-slate-600">
          Name
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={name}
            maxLength={512}
            onChange={(e) => setName(e.target.value)}
            placeholder="Org name"
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
              placeholder="US"
            />
          </label>
          <label className="block text-xs text-slate-600">
            Capacity total
            <input
              type="number"
              min={0}
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
        <div className="flex justify-end gap-2">
          <button type="button" className={BTN} disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={BTN_PRIMARY}
            disabled={busy || !name.trim()}
            onClick={onSubmit}
          >
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Re-export BTN_SECONDARY for downstream OrgDetail consistency.
export { BTN_SECONDARY };
