import { OrgDetail } from "../../../../src/components/OrgDetail";

export default async function AdminOrgDetailPage({
  params,
}: {
  params: Promise<{ orgId: string }>;
}) {
  const { orgId } = await params;
  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <OrgDetail orgId={orgId} />
    </main>
  );
}
