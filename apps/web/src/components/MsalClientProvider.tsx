"use client";

import { useEffect, useState } from "react";

import { PublicClientApplication } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";

import { buildMsalConfiguration, isB2cClientConfigured } from "../lib/b2c/msalConfig";

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
        redirectUri: typeof window !== "undefined" ? window.location.origin : config.auth?.redirectUri,
        postLogoutRedirectUri:
          typeof window !== "undefined" ? window.location.origin : config.auth?.postLogoutRedirectUri,
      },
    });
    pca
      .initialize()
      .then(() => {
        if (!pca.getActiveAccount() && pca.getAllAccounts().length > 0) {
          pca.setActiveAccount(pca.getAllAccounts()[0]!);
        }
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
    return <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">{initError}</div>;
  }
  if (!instance) {
    return <>{children}</>;
  }
  return <MsalProvider instance={instance}>{children}</MsalProvider>;
}
