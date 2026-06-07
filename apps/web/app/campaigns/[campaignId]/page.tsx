import { CampaignDetail } from "../../../src/components/CampaignDetail";

export default async function CampaignDetailPage({
  params,
}: {
  params: Promise<{ campaignId: string }>;
}) {
  const { campaignId } = await params;
  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <CampaignDetail campaignId={campaignId} />
    </main>
  );
}
