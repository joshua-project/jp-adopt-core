import { ContactRecord } from "../../../src/components/ContactRecord";

export const dynamic = "force-dynamic";

/**
 * Canonical staff contact-record page (#56). Renders the read tiles over data
 * jp-adopt-core already stores (matches, transition history, activity). Quick
 * actions (transition / edit / add-note) and the adoption-profile tiles layer
 * on in later units.
 */
export default async function ContactRecordPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ContactRecord contactId={id} />;
}
