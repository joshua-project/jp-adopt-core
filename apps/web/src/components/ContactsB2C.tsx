"use client";

import { useCallback, useEffect, useState } from "react";
import { InteractionRequiredAuthError } from "@azure/msal-browser";
import { useMsal } from "@azure/msal-react";

import type { paths } from "@jp-adopt/contracts";

import { getApiScopeList, isDevTokenUiEnabled } from "../lib/b2c/msalConfig";

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
        throw new Error("NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES is not set (API scope for access token).");
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
    throw new Error("Sign in with Azure AD B2C, or use the dev bearer token (local only).");
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
      setErr("Set NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES to your API scope (must match API audience/registration).");
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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Contacts</h1>
        <p className="mt-1 text-sm text-slate-600">
          Sign in with Azure AD B2C to obtain an access token for the FastAPI API scope. For local work without
          B2C, use <code className="rounded bg-slate-100 px-1">dev-local</code> in the dev-only token field
          with <code className="rounded bg-slate-100 px-1">STRICT_AUTH=false</code> on the API.
        </p>
      </div>

      <div className="space-y-2 rounded border border-slate-200 bg-slate-50/80 px-3 py-3">
        <p className="text-sm font-medium text-slate-800">Azure AD B2C</p>
        {active ? (
          <p className="text-sm text-slate-600">
            Signed in as <span className="font-mono text-slate-800">{active.username}</span>
          </p>
        ) : (
          <p className="text-sm text-slate-600">Not signed in</p>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
            onClick={() => void signIn()}
          >
            Sign in
          </button>
          <button
            type="button"
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50"
            onClick={() => void signOut()}
            disabled={!active}
          >
            Sign out
          </button>
        </div>
      </div>

      {showDev ? (
        <div className="space-y-2">
          <p className="text-sm font-medium text-slate-700">Dev-only bearer (local)</p>
          <label className="block text-sm text-slate-600" htmlFor="bearer">
            Pasted or <code>dev-local</code> when not using B2C; use after signing out to prefer this token.
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
      ) : null}

      <div className="flex gap-2">
        <button
          type="button"
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? "Loading…" : "Load contacts"}
        </button>
      </div>
      {err ? <p className="text-sm text-red-600">{err}</p> : null}

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
