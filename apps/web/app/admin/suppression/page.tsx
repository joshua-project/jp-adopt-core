import { SuppressionListAdmin } from "../../../src/components/SuppressionListAdmin";

export default function AdminSuppressionPage() {
  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <h1 className="font-heading text-2xl font-semibold text-slate-900">
        Email suppression
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Addresses on this list are skipped by the drip worker. Emails are
        hashed (SHA-256) on submit — the raw address is never persisted.
      </p>
      <div className="mt-6">
        <SuppressionListAdmin />
      </div>
    </main>
  );
}
