import { CampaignList } from "../../src/components/CampaignList";

export default function CampaignsPage() {
  return (
    <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="font-heading text-2xl font-semibold text-slate-900">
            Drip campaigns
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Manage email touchpoints — create a new campaign, add steps, and
            activate or pause delivery. The worker handles enrollment and
            sending; this UI is the authoring + control surface.
          </p>
        </div>
      </div>
      <div className="mt-6">
        <CampaignList />
      </div>
    </main>
  );
}
