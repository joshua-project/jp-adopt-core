import { WorkflowTransition } from "../../../../src/components/WorkflowTransition";

export const dynamic = "force-dynamic";

/**
 * Facilitator contact detail. Reuses the same WorkflowTransition component
 * as Amy's workflow view — the API enforces org-scoped authz, so the UI
 * doesn't need to gate it client-side. Facilitator's typical actions are
 * matched→active (accept) and matched→sent_back (with reason).
 */
export default async function FacilitatorContactPage({
  params,
}: {
  params: Promise<{ contactId: string }>;
}) {
  const { contactId } = await params;
  return <WorkflowTransition contactId={contactId} />;
}
