import Link from "next/link";

export default function HomePage() {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold text-slate-900">Phase 0 spike</h1>
      <p className="text-slate-600">
        This app exercises the monorepo stack:{" "}
        <span className="font-medium">Next.js</span>, the FastAPI service at{" "}
        <code className="rounded bg-slate-100 px-1.5 py-0.5 text-sm">/v1</code>, the transactional outbox, and
        the ARQ worker that signs webhooks with{" "}
        <code className="rounded bg-slate-100 px-1.5 py-0.5 text-sm">X-JP-Signature</code>.
      </p>
      <Link
        href="/contacts"
        className="inline-flex rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
      >
        Open contacts
      </Link>
      <p className="text-sm text-slate-500">
        For local dev, the API accepts <code className="rounded bg-slate-100 px-1">Bearer dev-local</code> when{" "}
        <code className="rounded bg-slate-100 px-1">STRICT_AUTH=false</code> (see runbook). Production uses
        Azure AD B2C JWTs; configure tenant, audience, and issuer in the environment.
      </p>
    </div>
  );
}
