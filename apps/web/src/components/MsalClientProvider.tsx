"use client";

import { useEffect, useState } from "react";

import { EventType, PublicClientApplication } from "@azure/msal-browser";
import type { AuthenticationResult, EventMessage } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";

import { buildEntraConfiguration, isEntraClientConfigured } from "../lib/msalConfig";

/**
 * MsalProvider wrapper for Azure Entra ID single-tenant SSO. Migrated to MSAL
 * v5 (Day 1 of the amy-return build); B2C scaffolding stripped 2026-05-26 when
 * Microsoft closed B2C to new customers.
 *
 * v5 idioms preserved here:
 *   - `instance.initialize()` returns a Promise we MUST await before any other
 *     MSAL API; v5 enforces this with a runtime check (v3 was lax).
 *   - The `addEventCallback` payload uses the `EventMessage` shape; we use the
 *     `EventType.LOGIN_SUCCESS` constant rather than a string literal so a
 *     future rename surfaces at the type-check site.
 *   - `navigateToLoginRequestUrl` moved to a per-request option on
 *     `handleRedirectPromise` — see `app/auth/callback/page.tsx`, which passes
 *     `{ navigateToLoginRequestUrl: false }` so navigation is explicit.
 *
 * `redirectUri` is NOT overridden here — the Entra config builder sets it to
 * `${origin}/auth/callback` (matching the SPA app reg's registered URIs).
 * Overriding it back to the bare origin caused `AADSTS50011` in the round-1
 * doc review; we leave the config's value intact.
 */
export function MsalClientProvider({ children }: { children: React.ReactNode }) {
  const [instance, setInstance] = useState<PublicClientApplication | null>(null);
  /** If Entra is not configured, we do not need to wait for MSAL init. */
  const [ready, setReady] = useState(() => !isEntraClientConfigured());
  const [initError, setInitError] = useState<string | null>(null);

  useEffect(() => {
    if (!isEntraClientConfigured()) {
      setReady(true);
      return;
    }
    const config = buildEntraConfiguration();
    if (!config) {
      setReady(true);
      return;
    }
    const pca = new PublicClientApplication(config);

    pca
      .initialize()
      .then(() => {
        // Pick an active account if one is already cached (page refresh after sign-in).
        if (!pca.getActiveAccount() && pca.getAllAccounts().length > 0) {
          pca.setActiveAccount(pca.getAllAccounts()[0]!);
        }
        // Keep the active account in sync on subsequent successful sign-ins.
        // F46: payload duck-typing replaced with a real type guard. MSAL v5's
        // LOGIN_SUCCESS event always carries an ``AuthenticationResult``;
        // narrowing on it lets TypeScript verify ``payload.account`` exists
        // instead of relying on a runtime ``"account" in event.payload`` check.
        pca.addEventCallback((event: EventMessage) => {
          if (event.eventType !== EventType.LOGIN_SUCCESS) return;
          const payload = event.payload as AuthenticationResult | null;
          if (payload && payload.account) {
            pca.setActiveAccount(payload.account);
          }
        });
        setInstance(pca);
        setReady(true);
      })
      .catch((e: unknown) => {
        setInitError(e instanceof Error ? e.message : "Entra MSAL initialize failed");
        setReady(true);
      });
  }, []);

  if (!ready) {
    return null;
  }
  if (initError) {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
        {initError}
      </div>
    );
  }
  if (!instance) {
    return <>{children}</>;
  }
  return <MsalProvider instance={instance}>{children}</MsalProvider>;
}
