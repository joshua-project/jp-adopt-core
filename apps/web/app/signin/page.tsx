"use client";

import { useMsal } from "@azure/msal-react";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { SIGNIN_SCOPES, isEntraClientConfigured } from "../../src/lib/msalConfig";

export const dynamic = "force-dynamic";

export default function SignInPage() {
  const { instance, accounts, inProgress } = useMsal();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const entraConfigured = isEntraClientConfigured();
  const ready = inProgress === "none";
  const alreadyAuthed = accounts.length > 0 || instance.getActiveAccount() !== null;

  // If the user lands on /signin while already authenticated, bounce them to /.
  useEffect(() => {
    if (ready && alreadyAuthed) {
      router.replace("/");
    }
  }, [ready, alreadyAuthed, router]);

  if (!entraConfigured) {
    return (
      <div className="mx-auto max-w-md rounded-lg border border-amber-200 bg-amber-50 p-6 text-sm text-amber-900">
        <h1 className="font-heading text-lg font-semibold">Entra sign-in is not configured</h1>
        <p className="mt-2">
          The web image was built without <code>NEXT_PUBLIC_AZURE_AD_TENANT_ID</code> or{" "}
          <code>NEXT_PUBLIC_AZURE_AD_CLIENT_ID</code>. Set both via the deploy workflow&apos;s{" "}
          <code>build-web</code> job and redeploy.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 rounded-lg border border-slate-200 bg-white p-8 shadow-sm">
      <h1 className="font-heading text-xl font-semibold text-slate-900">Sign in to JP Adopt</h1>
      <p className="text-sm text-slate-600">Use your @joshuaproject.net Microsoft account.</p>
      <button
        type="button"
        disabled={!ready}
        onClick={() => {
          setError(null);
          instance
            .loginRedirect({ scopes: SIGNIN_SCOPES })
            .catch((e: unknown) =>
              setError(e instanceof Error ? e.message : "Sign-in failed to start"),
            );
        }}
        className="mt-2 inline-flex items-center justify-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-slate-700 disabled:cursor-not-allowed disabled:bg-slate-400"
        aria-disabled={!ready}
      >
        {ready ? "Sign in with Microsoft" : "Loading…"}
      </button>
      {error ? (
        <p role="alert" className="text-sm text-rose-700">
          {error}
        </p>
      ) : null}
    </div>
  );
}
