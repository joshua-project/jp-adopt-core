import { PipelineView } from "../../src/components/PipelineView";

export const dynamic = "force-dynamic";

// Facilitator status enum, in pipeline order. Mirrors the CHECK
// constraint on contacts.facilitator_status.
const FACILITATOR_STATUSES = [
  "new",
  "not_ready",
  "ready",
  "do_not_engage",
] as const;

export default function FacilitatorsPage() {
  return (
    <PipelineView
      partyKind="facilitator"
      title="Facilitators"
      subtitle="Partner orgs and the people in them. Track who's ready to take adopters and who still needs follow-up."
      statuses={FACILITATOR_STATUSES}
      emptyTitle="No facilitators yet."
      emptyBody="Partner-org contacts show up here as they're added."
    />
  );
}
