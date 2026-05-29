import { AdminUserRoles } from "../../../src/components/AdminUserRoles";

export default function AdminUsersPage() {
  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <h1 className="font-heading text-2xl font-semibold text-slate-900">
        User roles
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Grant or revoke platform roles for Entra-authenticated staff. Enter the
        user&apos;s Entra object ID (UUID), not their email.
      </p>
      <div className="mt-6">
        <AdminUserRoles />
      </div>
    </main>
  );
}
