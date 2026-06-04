"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";

import type { paths } from "@jp-adopt/contracts";

import {
  activateCampaign,
  ApiError,
  formatApiError,
  listCampaigns,
  pauseCampaign,
} from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import { formatTimestamp } from "../lib/vocab";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";
import { NewCampaignModal } from "./NewCampaignModal";
import { StatusBadge } from "./StatusBadge";

type CampaignRead =
  paths["/v1/drips/campaigns"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];

export function CampaignList() {
  const ctx = useApiContext();
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [items, setItems] = useState<CampaignRead[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [isToggling, startToggle] = useTransition();
  const [showNew, setShowNew] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setForbidden(false);
    try {
      const list = await listCampaigns(ctx);
      setItems(list.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
        setItems([]);
        return;
      }
      setErr(formatApiError(e));
    } finally {
      setLoading(false);
    }
  }, [ctx]);

  useEffect(() => {
    void load();
  }, [load]);

  const onToggle = (campaign: CampaignRead) => {
    const fn = campaign.status === "active" ? pauseCampaign : activateCampaign;
    setBusyId(campaign.id);
    startToggle(() => {
      void (async () => {
        setErr(null);
        try {
          const updated = await fn(ctx, campaign.id);
          setItems((prev) =>
            prev.map((c) => (c.id === campaign.id ? updated : c)),
          );
        } catch (e) {
          setErr(formatApiError(e));
        } finally {
          setBusyId(null);
        }
      })();
    });
  };

  if (forbidden) {
    return (
      <EmptyState
        title="You can't manage drip campaigns"
        description="Drip campaign management is gated to staff_admin and adoption_manager roles."
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-600">
          {loading ? "Loading…" : `${items.length} campaign${items.length === 1 ? "" : "s"}`}
        </span>
        <button
          type="button"
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800"
          onClick={() => setShowNew(true)}
        >
          + New campaign
        </button>
      </div>

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      <DataTable
        rows={
          loading ? (
            <LoadingRows />
          ) : items.length === 0 ? null : (
            items.map((c) => {
              const canToggle =
                c.status === "active" || c.status === "draft" || c.status === "paused";
              const toggleLabel =
                c.status === "active" ? "Pause" : "Activate";
              return (
                <DataRow
                  key={c.id}
                  id={c.id}
                  title={
                    <Link
                      href={`/campaigns/${c.id}`}
                      className="hover:underline"
                    >
                      {c.name}
                    </Link>
                  }
                  badge={<StatusBadge status={c.status} kind="campaign" />}
                  meta={
                    c.trigger_event_type ? (
                      <span className="font-mono text-xs text-slate-500">
                        {c.trigger_event_type}
                      </span>
                    ) : null
                  }
                  subtle={`Updated ${formatTimestamp(c.updated_at)}`}
                  action={
                    canToggle ? (
                      <button
                        type="button"
                        disabled={isToggling && busyId === c.id}
                        onClick={() => onToggle(c)}
                        className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50"
                      >
                        {isToggling && busyId === c.id
                          ? "Working…"
                          : toggleLabel}
                      </button>
                    ) : null
                  }
                />
              );
            })
          )
        }
        empty={
          <EmptyState
            title="No drip campaigns yet"
            description='Click "+ New campaign" to start authoring your first one.'
          />
        }
      />

      {showNew ? (
        <NewCampaignModal
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
