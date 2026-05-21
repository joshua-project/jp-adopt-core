import { PipelineView } from "../../src/components/PipelineView";

export const dynamic = "force-dynamic";

// Adopter status enum, in pipeline funnel order. Mirrors the CHECK
// constraint on contacts.adopter_status (see apps/api migration 0001 +
// schemas in apps/api/src/jp_adopt_api/routers/contacts.py).
const ADOPTER_STATUSES = [
  "new",
  "potential_adopter",
  "contacted",
  "engaged",
  "matched",
  "sent_back",
  "active",
  "inactive",
  "do_not_engage",
] as const;

export default function AdoptersPage() {
  return (
    <PipelineView
      partyKind="adopter"
      title="Adopters"
      subtitle="People who've signed up to support a people group. Filter by stage to see where things sit."
      statuses={ADOPTER_STATUSES}
      emptyTitle="No adopters yet."
      emptyBody="Once people come in through the public form (or you add one by hand), they show up here."
    />
  );
}
