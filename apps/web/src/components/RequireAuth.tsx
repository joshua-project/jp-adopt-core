"use client";

import { useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { isEntraClientConfigured } from "../lib/msalConfig";

/**
 * Client-side auth gate. Renders a stable loading shell until MSAL is
 * initialized; once initialized, redirects unauthenticated users to /signin.
 * The gate exempts /signin and /auth/callback by pathname (those pages must
 * render regardless of auth state).
 *
 * Local-dev mode: if Entra is not configured (`!isEntraClientConfigured()`),
 * the gate is a no-op so `pnpm dev` works without a real Entra tenant. In
 * production, the Dockerfile bakes both env vars, so the gate is always
 * active.
 *
 * No-flicker contract: never render `{children}` while MSAL is initializing
 * or while the user is unauthenticated. The loading shell is intentionally
 * minimal and brand-neutral so it doesn't flash dashboard structure before
 * auth resolves.
 */
const EXEMPT_PATHS = new Set(["/signin", "/auth/callback"]);

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { instance, accounts, inProgress } = useMsal();
  const router = useRouter();

  const exempt = pathname ? EXEMPT_PATHS.has(pathname) : false;
  const entraConfigured = isEntraClientConfigured();
  const ready = inProgress === InteractionStatus.None;
  const authenticated = accounts.length > 0 || instance.getActiveAccount() !== null;

  // Local dev with Entra unconfigured: gate is a no-op (the dev-token UI
  // continues to handle auth).
  const gateActive = entraConfigured && !exempt;

  useEffect(() => {
    if (gateActive && ready && !authenticated) {
      router.replace("/signin");
    }
  }, [gateActive, ready, authenticated, router]);

  if (!gateActive) {
    return <>{children}</>;
  }
  if (!ready || !authenticated) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="mx-auto max-w-md py-10 text-center text-sm text-slate-600"
      >
        Loading…
      </div>
    );
  }
  return <>{children}</>;
}
