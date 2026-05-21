"use client";

import { useCallback, useEffect, useState } from "react";
import { InteractionRequiredAuthError } from "@azure/msal-browser";
import { useMsal } from "@azure/msal-react";

import type { paths } from "@jp-adopt/contracts";

import { getApiScopeList, isDevTokenUiEnabled } from "../lib/b2c/msalConfig";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";
import { StatusBadge } from "./StatusBadge";
import { humanizePartyKind } from "../lib/vocab";

type ListResponse = paths["/v1/contacts"]["get"]["responses"]["200"]["content"]["application/json"];

const STORAGE_KEY = "jp_adopt_bearer";

function useManualTokenState() {
  const showDev = isDevTokenUiEnabled();
  const [token, setToken] = useState("");
  useEffect(() => {
    if (typeof window === "undefined" || !showDev) {
      return;
    }
    const t = window.localStorage.getItem(STORAGE_KEY);
    if (t) {
      setToken(t);
    } else {
      setToken("dev-local");
    }
  }, [showDev]);
  return { showDev, token, setToken };
}

export function ContactsB2C() {
  const { instance, accounts } = useMsal();
  const { showDev, token: devToken, setToken: setDevToken } = useManualTokenState();
  const [data, setData] = useState<ListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const base = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
  const scopes = getApiScopeList();

  const resolveAccessToken = useCallback(async () => {
    const account = instance.getActiveAccount() ?? accounts[0] ?? null;
    if (account) {
      if (scopes.length === 0) {
        throw new Error(
          "API scope is not configured. Ask an administrator to set the Azure AD B2C API scope.",
        );
      }
      try {
        const result = await instance.acquireTokenSilent({
          account,
          scopes,
          forceRefresh: false,
        });
        return result.accessToken;
      } catch (e) {
        if (e instanceof InteractionRequiredAuthError) {
          const result = await instance.acquireTokenPopup({ account, scopes });
          return result.accessToken;
        }
        throw e;
      }
    }
    if (showDev && devToken.trim()) {
      return devToken.trim();
    }
    throw new Error("Sign in to view contacts.");
  }, [accounts, devToken, instance, scopes, showDev]);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    const activeB2C = instance.getActiveAccount() ?? accounts[0] ?? null;
    if (showDev && typeof window !== "undefined" && !activeB2C) {
      window.localStorage.setItem(STORAGE_KEY, devToken);
    }
    try {
      const access = await resolveAccessToken();
      const res = await fetch(`${base}/v1/contacts?limit=50`, {
        headers: { Authorization: `Bearer ${access}` },
      });
      if (!res.ok) {
        setData(null);
        setErr(`HTTP ${res.status} ${res.statusText}`);
        return;
      }
      const json = (await res.json()) as ListResponse;
      setData(json);
    } catch (e) {
      setData(null);
      setErr(e instanceof Error ? e.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }, [base, accounts, devToken, instance, resolveAccessToken, showDev]);

  const signIn = useCallback(() => {
    if (scopes.length === 0) {
      setErr(
        "API scope is not configured. Ask an administrator to set the Azure AD B2C API scope.",
      );
      return;
    }
    void instance
      .loginPopup({ scopes })
      .then((r) => {
        if (r.account) {
          instance.setActiveAccount(r.account);
        }
        setErr(null);
      })
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : "Sign-in failed");
      });
  }, [instance, scopes]);

  const signOut = useCallback(() => {
    const account = instance.getActiveAccount() ?? accounts[0];
    if (account) {
      void instance.logoutPopup({ account });
    }
  }, [instance, accounts]);

  const active = instance.getActiveAccount() ?? accounts[0];
  const signedIn = Boolean(active) || (showDev && devToken.trim().length > 0);

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
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-slate-800">Sign-in</p>
            {active ? (
              <p className="mt-0.5 text-sm text-slate-600">
                Signed in as{" "}
                <span className="font-medium text-slate-900">
                  {active.username}
                </span>
              </p>
            ) : (
              <p className="mt-0.5 text-sm text-slate-600">Not signed in.</p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {active ? (
              <button
                type="button"
                className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 shadow-sm hover:bg-slate-50"
                onClick={() => void signOut()}
              >
                Sign out
              </button>
            ) : (
              <button
                type="button"
                className="rounded-md bg-orange-600 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-orange-700"
                onClick={() => void signIn()}
              >
                Sign in
              </button>
            )}
          </div>
        </div>

        {showDev ? (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-700">
              Developer access token (local only)
            </summary>
            <div className="mt-2 space-y-1">
              <label className="block text-xs text-slate-600" htmlFor="bearer">
                Bearer token
              </label>
              <input
                id="bearer"
                className="w-full rounded border border-slate-300 bg-white px-3 py-2 font-mono text-sm"
                value={devToken}
                onChange={(e) => setDevToken(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </div>
          </details>
        ) : null}
      </section>

      <div className="flex items-center justify-between">
        <button
          type="button"
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:opacity-50"
          onClick={() => void load()}
          disabled={loading || !signedIn}
        >
          {loading ? "Loading…" : data ? "Refresh" : "Load contacts"}
        </button>
        {data ? (
          <p className="text-xs uppercase tracking-wide text-slate-500">
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
      ) : data ? (
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
      ) : !signedIn ? (
        <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-10 text-center">
          <p className="text-sm font-medium text-slate-700">
            Sign in to view contacts.
          </p>
          <p className="mt-1 text-xs text-slate-500">
            Contacts are scoped to your organization.
          </p>
        </div>
      ) : null}
    </div>
  );
}
