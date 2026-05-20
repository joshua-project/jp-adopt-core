import { MatchReview } from "../../../src/components/MatchReview";

export const dynamic = "force-dynamic";

export default async function MatchDetailPage({
  params,
}: {
  params: Promise<{ matchId: string }>;
}) {
  const { matchId } = await params;
  return <MatchReview matchId={matchId} />;
}
