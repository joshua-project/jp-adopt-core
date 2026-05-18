"use client";

import { useEffect, useState } from "react";

import { EventType, PublicClientApplication } from "@azure/msal-browser";
import type { AuthenticationResult, EventMessage } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";

import { buildMsalConfiguration, isB2cClientConfigured } from "../lib/b2c/msalConfig";

/**
 * MsalProvider wrapper, migrated to MSAL v5 (Day 1 of the amy-return build).
 *
 * v5 differences from v3 we care about here:
 *   - `instance.initialize()` returns a Promise we MUST await before any
 *     other API; v5 enforces this with a runtime check (v3 was lax).
 *   - The `addEventCallback` payload now uses the `EventMessage` shape; the
 *     v3 `setActiveAccount`-on-login callback we use to keep the active
 *     account in sync still works, but the union of EventType values has
 *     changed names (we use the constant rather than a string literal so
 *     a future rename surfaces at the type-check site).
 */
export function MsalClientProvider({ children }: { children: React.ReactNode }) {
  const [instance, setInstance] = useState<PublicClientApplication | null>(null);
  /** If B2C is not configured, we do not need to wait for MSAL init. */
  const [ready, setReady] = useState(() => !isB2cClientConfigured());
  const [initError, setInitError] = useState<string | null>(null);

  useEffect(() => {
    if (!isB2cClientConfigured()) {
      setReady(true);
      return;
    }
    const config = buildMsalConfiguration();
    if (!config) {
      setReady(true);
      return;
    }
    const pca = new PublicClientApplication({
      ...config,
      auth: {
        ...config.auth,
        redirectUri:
          typeof window !== "undefined" ? window.location.origin : config.auth?.redirectUri,
        postLogoutRedirectUri:
          typeof window !== "undefined"
            ? window.location.origin
            : config.auth?.postLogoutRedirectUri,
      },
    });

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
        setInitError(e instanceof Error ? e.message : "MSAL initialize failed");
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
