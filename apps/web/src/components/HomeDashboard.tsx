"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, apiFetch, getMatchQueue } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

type CampaignsResponse =
  paths["/v1/drips/campaigns"]["get"]["responses"]["200"]["content"]["application/json"];

interface Counts {
  pendingMatches: number | null;
  activeCampaigns: number | null;
}

/**
 * Staff landing page. Three quick-action cards (Matches / Add contact /
 * Facilitator) with live counts pulled from the API. The page intentionally
 * carries no engineering vocabulary; everything reads as staff-facing copy.
 */
export function HomeDashboard() {
  const ctx = useApiContext();
  const [counts, setCounts] = useState<Counts>({
    pendingMatches: null,
    activeCampaigns: null,
  });
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    const next: Counts = { pendingMatches: null, activeCampaigns: null };
    try {
      const q = await getMatchQueue(ctx);
      next.pendingMatches = q.total;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        // Counts only render for signed-in staff. Soft-fail.
      } else if (e instanceof Error) {
        setErr(e.message);
      }
    }
    try {
      const c = await apiFetch<CampaignsResponse>(ctx, "/v1/drips/campaigns");
      if (c) {
        next.activeCampaigns = c.items.filter(
          (it) => it.status === "active",
        ).length;
      }
    } catch {
      // Same soft-fail policy — landing should never look broken because
      // counts couldn't load.
    }
    setCounts(next);
  }, [ctx]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-10">
      <header className="space-y-3">
        <p className="text-xs font-semibold uppercase tracking-[0.22em] text-jp-accent">
          Adoption program
        </p>
        <h1 className="font-heading text-5xl font-semibold leading-tight tracking-tight text-slate-900 sm:text-[56px]">
          Staff console.
        </h1>
        <p className="max-w-2xl text-base text-slate-600">
          Review pending matches, add walk-in contacts, and check on
          facilitator activity — all in one place.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <QuickActionCard
          href="/matches"
          label="Review match queue"
          description="Pending recommendations awaiting a decision."
          metric={counts.pendingMatches}
          metricSuffix="pending"
          accent="orange"
        />
        <QuickActionCard
          href="/adopters"
          label="Adopter pipeline"
          description="Filter by stage: new, contacted, matched, active, sent back."
          metric={null}
          metricSuffix=""
          accent="teal"
        />
        <QuickActionCard
          href="/facilitators"
          label="Facilitator pipeline"
          description="Partner-org contacts by readiness. Plus active drip campaigns."
          metric={counts.activeCampaigns}
          metricSuffix={
            counts.activeCampaigns === 1 ? "active campaign" : "active campaigns"
          }
          accent="slate"
        />
      </div>

      <section className="rounded-lg border border-slate-200 bg-gradient-to-br from-jp-cream via-white to-white p-6 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-xl">
            <h2 className="font-heading text-lg font-semibold text-slate-900">
              Need to look something up?
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              The adopter and facilitator pipelines show every contact the
              program has touched, grouped by stage. Filter by status to
              find someone or to see where the bottleneck is.
            </p>
          </div>
          <Link
            href="/adopters"
            className="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 shadow-sm transition-colors hover:border-jp-accent hover:text-jp-accent"
          >
            Open adopters
            <span aria-hidden>→</span>
          </Link>
        </div>
        {err ? (
          <p className="mt-3 text-xs text-slate-500">
            Some metrics couldn&apos;t load right now.
          </p>
        ) : null}
      </section>
    </div>
  );
}

function QuickActionCard({
  href,
  label,
  description,
  metric,
  metricSuffix,
  accent,
}: {
  href: string;
  label: string;
  description: string;
  metric: number | null;
  metricSuffix: string;
  accent: "orange" | "teal" | "slate";
}) {
  const accentClasses: Record<typeof accent, string> = {
    orange:
      "border-l-orange-500 hover:border-orange-600 hover:bg-orange-50/40",
    teal: "border-l-teal-600 hover:border-teal-700 hover:bg-teal-50/40",
    slate: "border-l-slate-500 hover:border-slate-700 hover:bg-slate-50",
  };
  const metricColor: Record<typeof accent, string> = {
    orange: "text-orange-600",
    teal: "text-teal-700",
    slate: "text-slate-700",
  };
  return (
    <Link
      href={href}
      className={`group block rounded-lg border border-slate-200 border-l-4 bg-white p-5 shadow-sm transition-colors ${accentClasses[accent]}`}
    >
      <div className="flex items-start justify-between gap-3">
        <h2 className="font-heading text-lg font-semibold text-slate-900">
          {label}
        </h2>
        <span className="text-slate-400 transition-transform group-hover:translate-x-0.5">
          →
        </span>
      </div>
      <p className="mt-1 text-sm text-slate-600">{description}</p>
      <div className="mt-4">
        {metric === null ? (
          <p className="text-xs text-slate-400">
            {metricSuffix ? `Live count: ${metricSuffix}` : ""}
          </p>
        ) : (
          <p className="flex items-baseline gap-2">
            <span
              className={`font-heading text-3xl font-semibold tracking-tight ${metricColor[accent]}`}
            >
              {metric}
            </span>
            <span className="text-xs uppercase tracking-wide text-slate-500">
              {metricSuffix}
            </span>
          </p>
        )}
      </div>
    </Link>
  );
}
