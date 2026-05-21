"use client";

import { useCallback, useEffect, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

import { isDevTokenUiEnabled } from "../lib/b2c/msalConfig";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";
import { StatusBadge } from "./StatusBadge";
import { humanizePartyKind } from "../lib/vocab";

type ListResponse = paths["/v1/contacts"]["get"]["responses"]["200"]["content"]["application/json"];

const STORAGE_KEY = "jp_adopt_bearer";

/**
 * Local-dev contacts UI when B2C MSAL is not configured (no public client id,
 * etc.). The "dev bearer token" field is hidden behind a disclosure so the
 * page reads like the production contacts view; staff in this build still
 * see a Contacts table, not a setup checklist.
 */
export function ContactsDevOnly() {
  const [token, setToken] = useState("");
  const [data, setData] = useState<ListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const showTokenUi = isDevTokenUiEnabled();

  const base = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

  useEffect(() => {
    if (typeof window === "undefined" || !showTokenUi) {
      return;
    }
    const t = window.sessionStorage.getItem(STORAGE_KEY);
    if (t) {
      setToken(t);
    } else {
      setToken("dev-local");
    }
  }, [showTokenUi]);

  const load = useCallback(async () => {
    if (!showTokenUi) {
      return;
    }
    setLoading(true);
    setErr(null);
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem(STORAGE_KEY, token);
    }
    const res = await fetch(`${base}/v1/contacts?limit=50`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      setLoading(false);
      setData(null);
      setErr(`HTTP ${res.status} ${res.statusText}`);
      return;
    }
    const json = (await res.json()) as ListResponse;
    setData(json);
    setLoading(false);
  }, [base, token, showTokenUi]);

  if (!showTokenUi) {
    return (
      <div className="space-y-4">
        <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
          Contacts
        </h1>
        <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
          <p className="text-sm font-medium text-slate-700">
            Sign-in is not yet configured for this environment.
          </p>
          <p className="mt-1 text-xs text-slate-500">
            Ask an administrator to complete the Azure AD B2C setup before
            using the staff console.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-heading text-3xl font-semibold tracking-tight text-slate-900">
          Contacts
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          Every adopter and facilitator the program has touched.
        </p>
      </div>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <details>
          <summary className="cursor-pointer text-sm font-medium text-slate-800">
            Developer access token (local only)
          </summary>
          <div className="mt-3 space-y-2">
            <label className="block text-xs text-slate-600" htmlFor="bearer">
              Bearer token
            </label>
            <input
              id="bearer"
              className="w-full rounded border border-slate-300 bg-white px-3 py-2 font-mono text-sm"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
        </details>
      </section>

      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-slate-800 disabled:opacity-50"
          onClick={() => void load()}
          disabled={loading || !token.trim()}
        >
          {loading ? "Loading…" : data ? "Refresh" : "Load contacts"}
        </button>
        {data ? (
          <p className="text-xs uppercase tracking-[0.12em] text-slate-500">
            {data.total} total · showing {data.items.length}
          </p>
        ) : null}
      </div>
      {err ? (
        <div
          role="alert"
          className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900"
        >
          {err}
        </div>
      ) : null}

      {loading && !data ? (
        <DataTable rows={null} empty={<LoadingRows />} />
      ) : !data ? (
        <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
          <p className="font-heading text-lg font-semibold text-slate-800">
            Ready when you are.
          </p>
          <p className="mt-1 text-sm text-slate-500">
            Press <span className="font-semibold text-slate-700">Load contacts</span>{" "}
            to fetch the list.
          </p>
        </div>
      ) : (
        <DataTable
          rows={
            data.items.length > 0
              ? data.items.map((c) => (
                  <DataRow
                    key={c.id}
                    id={c.id}
                    title={c.display_name}
                    badge={
                      c.adopter_status ? (
                        <StatusBadge status={c.adopter_status} />
                      ) : undefined
                    }
                    meta={
                      <span>
                        <span className="text-slate-500">Kind:</span>{" "}
                        <span className="text-slate-800">
                          {humanizePartyKind(c.party_kind)}
                        </span>
                      </span>
                    }
                  />
                ))
              : null
          }
          empty={
            <EmptyState
              title="No contacts yet."
              description="Add a contact manually, or wait for the public form to receive its first submission."
            />
          }
        />
      )}
    </div>
  );
}
