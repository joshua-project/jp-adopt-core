import { DuplicateReviewAdmin } from "../../../src/components/DuplicateReviewAdmin";

export default function AdminDuplicatesPage() {
  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <h1 className="font-heading text-2xl font-semibold text-slate-900">
        Review duplicates
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        People who exist in both Disciple.Tools and the new system. Matching
        names merge automatically each hour; the ambiguous ones below need your
        call. <strong>Same person</strong> merges the DT record onto the
        contact that kept the email (applied on the next sync).{" "}
        <strong>Not a duplicate</strong> hides shared inboxes used by different
        people.
      </p>
      <div className="mt-6">
        <DuplicateReviewAdmin />
      </div>
    </main>
  );
}
