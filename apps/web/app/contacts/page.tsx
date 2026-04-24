"use client";

import { useCallback, useEffect, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

type ListResponse = paths["/v1/contacts"]["get"]["responses"]["200"]["content"]["application/json"];

const STORAGE_KEY = "jp_adopt_bearer";

export default function ContactsPage() {
  const [token, setToken] = useState("");
  const [data, setData] = useState<ListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const base = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const t = window.localStorage.getItem(STORAGE_KEY);
    if (t) {
      setToken(t);
    } else {
      setToken("dev-local");
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, token);
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
  }, [base, token]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Contacts</h1>
        <p className="mt-1 text-sm text-slate-600">
          Paste an Azure AD B2C access token for the API scope, or use{" "}
          <code className="rounded bg-slate-100 px-1">dev-local</code> with{" "}
          <code className="rounded bg-slate-100 px-1">STRICT_AUTH=false</code>.
        </p>
      </div>
      <div className="space-y-2">
        <label className="block text-sm font-medium text-slate-700" htmlFor="bearer">
          Bearer access token
        </label>
        <input
          id="bearer"
          className="w-full rounded border border-slate-300 bg-white px-3 py-2 font-mono text-sm"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <div className="flex gap-2">
          <button
            type="button"
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={() => void load()}
            disabled={loading || !token.trim()}
          >
            {loading ? "Loading…" : "Load contacts"}
          </button>
        </div>
        {err ? <p className="text-sm text-red-600">{err}</p> : null}
      </div>
      {data ? (
        <div className="space-y-2">
          <p className="text-sm text-slate-500">
            Total: {data.total} (showing {data.items.length})
          </p>
          <ul className="divide-y divide-slate-200 overflow-hidden rounded border border-slate-200 bg-white">
            {data.items.map((c) => (
              <li key={c.id} className="px-4 py-3">
                <div className="font-medium text-slate-900">{c.display_name}</div>
                <div className="text-xs text-slate-500">
                  {c.party_kind}
                  {c.adopter_status ? ` · ${c.adopter_status}` : ""}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
