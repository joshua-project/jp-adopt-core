import { IntakeKeysAdmin } from "../../../src/components/IntakeKeysAdmin";

export default function AdminIntakeKeysPage() {
  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <h1 className="font-heading text-2xl font-semibold text-slate-900">
        Intake API keys
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Bearer credentials for server-to-server intake submissions
        (jp-adopt-forms, n8n, ETL). The plaintext is shown once on mint —
        capture it then; it&apos;s not recoverable later.
      </p>
      <div className="mt-6">
        <IntakeKeysAdmin />
      </div>
    </main>
  );
}
