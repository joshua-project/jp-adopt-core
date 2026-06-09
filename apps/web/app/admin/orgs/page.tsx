import { OrgList } from "../../../src/components/OrgList";

export default function AdminOrgsPage() {
  return (
    <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      <h1 className="font-heading text-2xl font-semibold text-slate-900">
        Facilitating orgs
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Create and edit the partner orgs that receive matched adopters.
        Capacity and FPG coverage drive the matching algorithm.
      </p>
      <div className="mt-6">
        <OrgList />
      </div>
    </main>
  );
}
