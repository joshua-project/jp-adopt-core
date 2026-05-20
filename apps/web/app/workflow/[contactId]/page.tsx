import { WorkflowTransition } from "../../../src/components/WorkflowTransition";

export const dynamic = "force-dynamic";

export default async function WorkflowPage({
  params,
}: {
  params: Promise<{ contactId: string }>;
}) {
  const { contactId } = await params;
  return <WorkflowTransition contactId={contactId} />;
}
