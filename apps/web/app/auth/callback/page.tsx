"use client";

import { useMsal } from "@azure/msal-react";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

export const dynamic = "force-dynamic";

/**
 * MSAL redirect-return handler. PKCE code exchange happens inside
 * `instance.handleRedirectPromise()`; this page exists to call it, surface
 * errors, and route to `/` on success.
 *
 * `navigateToLoginRequestUrl: false` keeps MSAL from auto-navigating back to
 * the original request URL — we always `router.push("/")` so the navigation
 * contract is explicit. (MSAL v5 moved this from a config-level setting to a
 * per-call option.)
 *
 * Deliberately instrumentation-free: do NOT fire any analytics / error-
 * reporting event from `useEffect` before the URL hash is cleared. The auth
 * code lives in the URL fragment for the ~500ms exchange window; any
 * `posthog.capture`, `sentry.captureException`, or `gtag` call that reads
 * `window.location.href` during mount could log the code.
 */
export default function AuthCallbackPage() {
  const { instance } = useMsal();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    instance
      .handleRedirectPromise()
      .then((result) => {
        if (cancelled) return;
        if (result?.account) {
          instance.setActiveAccount(result.account);
        }
        router.replace("/");
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Sign-in callback failed");
      });
    return () => {
      cancelled = true;
    };
  }, [instance, router]);

  if (error) {
    return (
      <div
        role="alert"
        aria-live="polite"
        className="mx-auto max-w-md rounded-lg border border-rose-200 bg-rose-50 p-6 text-sm text-rose-900"
      >
        <h1 className="font-heading text-lg font-semibold">Sign-in failed</h1>
        <p className="mt-2 break-all">{error}</p>
        <a href="/signin" className="mt-3 inline-block text-rose-700 underline">
          Back to sign-in
        </a>
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="mx-auto max-w-md py-10 text-center text-sm text-slate-600"
    >
      Signing you in…
    </div>
  );
}
