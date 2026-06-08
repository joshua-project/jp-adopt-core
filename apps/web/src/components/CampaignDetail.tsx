"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import type { paths } from "@jp-adopt/contracts";

import {
  activateCampaign,
  archiveCampaign,
  deleteCampaignStep,
  formatApiError,
  getCampaign,
  patchCampaign,
  pauseCampaign,
  previewCampaignStep,
} from "../lib/api-client";
import { BTN_DANGER, BTN_PRIMARY, BTN_SECONDARY } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";
import { formatTimestamp } from "../lib/vocab";
import { AddCampaignStepForm } from "./AddCampaignStepForm";
import { StatusBadge } from "./StatusBadge";

type CampaignRead =
  paths["/v1/drips/campaigns/{campaign_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type CampaignStep = NonNullable<CampaignRead["steps"]>[number];
type StepPreview =
  paths["/v1/drips/campaigns/{campaign_id}/steps/{position}/preview"]["post"]["responses"]["200"]["content"]["application/json"];

function pad2(n: number): string {
  return n.toString().padStart(2, "0");
}

function MetaPanel({
  campaign,
  onSaved,
}: {
  campaign: CampaignRead;
  onSaved: (c: CampaignRead) => void;
}) {
  const ctx = useApiContext();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(campaign.name);
  const [description, setDescription] = useState(campaign.description ?? "");
  const [triggerEventType, setTriggerEventType] = useState(
    campaign.trigger_event_type ?? "",
  );
  const [precedence, setPrecedence] = useState(campaign.precedence);
  const [err, setErr] = useState<string | null>(null);
  const [busy, startSave] = useTransition();

  const startEdit = () => {
    setName(campaign.name);
    setDescription(campaign.description ?? "");
    setTriggerEventType(campaign.trigger_event_type ?? "");
    setPrecedence(campaign.precedence);
    setErr(null);
    setEditing(true);
  };

  const onSave = () => {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setErr("Name is required.");
      return;
    }
    if (campaign.trigger_type === "event" && !triggerEventType.trim()) {
      setErr("Event-triggered campaigns require a trigger event type.");
      return;
    }
    setErr(null);
    startSave(() => {
      void (async () => {
        try {
          const updated = await patchCampaign(ctx, campaign.id, {
            name: trimmedName,
            description: description.trim() || null,
            trigger_event_type:
              campaign.trigger_type === "event"
                ? triggerEventType.trim()
                : null,
            precedence,
          });
          onSaved(updated);
          setEditing(false);
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  if (!editing) {
    return (
      <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="font-heading text-xl font-semibold text-slate-900">
              {campaign.name}
            </h2>
            {campaign.description ? (
              <p className="mt-1 text-sm text-slate-600">
                {campaign.description}
              </p>
            ) : null}
          </div>
          <StatusBadge status={campaign.status} kind="campaign" />
        </div>
        <dl className="grid grid-cols-2 gap-3 text-xs text-slate-600 sm:grid-cols-4">
          <div>
            <dt className="uppercase tracking-wide text-slate-400">Trigger</dt>
            <dd className="mt-0.5 font-mono text-slate-800">
              {campaign.trigger_type}
            </dd>
          </div>
          <div>
            <dt className="uppercase tracking-wide text-slate-400">
              Trigger event
            </dt>
            <dd className="mt-0.5 truncate font-mono text-slate-800">
              {campaign.trigger_event_type ?? "—"}
            </dd>
          </div>
          <div>
            <dt className="uppercase tracking-wide text-slate-400">
              Precedence
            </dt>
            <dd className="mt-0.5 text-slate-800">{campaign.precedence}</dd>
          </div>
          <div>
            <dt className="uppercase tracking-wide text-slate-400">Version</dt>
            <dd className="mt-0.5 text-slate-800">{campaign.version}</dd>
          </div>
        </dl>
        <button type="button" className={BTN_SECONDARY} onClick={startEdit}>
          Edit
        </button>
      </section>
    );
  }

  return (
    <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-medium text-slate-700">Edit campaign</h2>
      <label className="block text-xs text-slate-600">
        Name
        <input
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          value={name}
          maxLength={512}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="block text-xs text-slate-600">
        Description
        <textarea
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          rows={2}
          value={description}
          maxLength={4096}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <div className="grid grid-cols-2 gap-3">
        {campaign.trigger_type === "event" ? (
          <label className="block text-xs text-slate-600">
            Trigger event type
            <input
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm font-mono"
              value={triggerEventType}
              maxLength={256}
              onChange={(e) => setTriggerEventType(e.target.value)}
              placeholder="jp.adopt.v1.match.accepted_by_facilitator"
            />
          </label>
        ) : null}
        <label className="block text-xs text-slate-600">
          Precedence
          <input
            type="number"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={precedence}
            onChange={(e) => setPrecedence(Number(e.target.value) || 0)}
          />
        </label>
      </div>
      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}
      <div className="flex gap-2">
        <button
          type="button"
          className={BTN_PRIMARY}
          disabled={busy || !name.trim()}
          onClick={onSave}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className={BTN_SECONDARY}
          disabled={busy}
          onClick={() => setEditing(false)}
        >
          Cancel
        </button>
      </div>
    </section>
  );
}

function StepRow({
  step,
  busy,
  onDelete,
  onPreview,
}: {
  step: CampaignStep;
  busy: boolean;
  onDelete: (position: number) => void;
  onPreview: (position: number) => void;
}) {
  return (
    <li className="flex items-start justify-between gap-3 px-4 py-3 text-sm">
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-xs text-slate-500">
            #{step.position}
          </span>
          <span className="font-medium text-slate-900">{step.subject}</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-slate-500">
          <span className="font-mono">{step.mjml_template_name}</span>
          <span>delay {step.delay_days}d</span>
          <span>
            send at {pad2(step.send_at_hour)}:{pad2(step.send_at_minute)}
          </span>
        </div>
      </div>
      <div className="flex shrink-0 gap-2">
        <button
          type="button"
          disabled={busy}
          className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          onClick={() => onPreview(step.position)}
        >
          Preview
        </button>
        <button
          type="button"
          disabled={busy}
          className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          onClick={() => onDelete(step.position)}
        >
          {busy ? "Removing…" : "Remove"}
        </button>
      </div>
    </li>
  );
}

function PreviewModal({
  preview,
  loading,
  err,
  onClose,
}: {
  preview: StepPreview | null;
  loading: boolean;
  err: string | null;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex h-[90vh] w-full max-w-3xl flex-col rounded-lg bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-slate-900">
              {preview ? `Step #${preview.position} preview` : "Step preview"}
            </h3>
            {preview ? (
              <p className="mt-0.5 truncate text-xs text-slate-500">
                <span className="font-mono">{preview.mjml_template_name}</span>
                {" · "}
                Sample contact:{" "}
                <span className="font-medium">
                  {preview.sample_context.contact_display_name}
                </span>
              </p>
            ) : null}
          </div>
          <button
            type="button"
            className={BTN_SECONDARY}
            onClick={onClose}
          >
            Close
          </button>
        </div>
        {preview ? (
          <div className="border-b border-slate-200 bg-slate-50 px-5 py-3">
            <p className="text-xs text-slate-500">Subject</p>
            <p className="font-medium text-slate-900">{preview.subject}</p>
          </div>
        ) : null}
        <div className="flex-1 overflow-hidden bg-slate-100 p-3">
          {loading ? (
            <p className="text-sm text-slate-500">Rendering preview…</p>
          ) : err ? (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
              {err}
            </div>
          ) : preview ? (
            <iframe
              title="Step preview"
              srcDoc={preview.html}
              sandbox=""
              className="h-full w-full rounded border border-slate-300 bg-white"
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function CampaignDetail({ campaignId }: { campaignId: string }) {
  const ctx = useApiContext();
  const router = useRouter();
  const [campaign, setCampaign] = useState<CampaignRead | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [stepBusyPos, setStepBusyPos] = useState<number | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [preview, setPreview] = useState<StepPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const c = await getCampaign(ctx, campaignId);
      setCampaign(c);
    } catch (e) {
      setErr(formatApiError(e));
    }
  }, [ctx, campaignId]);

  useEffect(() => {
    void load();
  }, [load]);

  const onActivate = async () => {
    if (!campaign) return;
    setActionBusy(true);
    setErr(null);
    try {
      const updated = await activateCampaign(ctx, campaign.id);
      setCampaign(updated);
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setActionBusy(false);
    }
  };

  const onPause = async () => {
    if (!campaign) return;
    setActionBusy(true);
    setErr(null);
    try {
      const updated = await pauseCampaign(ctx, campaign.id);
      setCampaign(updated);
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setActionBusy(false);
    }
  };

  const onArchive = async () => {
    if (!campaign) return;
    if (
      !window.confirm(
        `Archive "${campaign.name}"? This stops enrollment and removes it from the active list.`,
      )
    )
      return;
    setActionBusy(true);
    setErr(null);
    try {
      await archiveCampaign(ctx, campaign.id);
      router.push("/campaigns");
    } catch (e) {
      setErr(formatApiError(e));
      setActionBusy(false);
    }
  };

  const onDeleteStep = async (position: number) => {
    if (!campaign) return;
    if (!window.confirm(`Remove step #${position}?`)) return;
    setStepBusyPos(position);
    setErr(null);
    try {
      await deleteCampaignStep(ctx, campaign.id, position);
      await load();
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setStepBusyPos(null);
    }
  };

  const onPreviewStep = async (position: number) => {
    if (!campaign) return;
    setPreviewOpen(true);
    setPreview(null);
    setPreviewErr(null);
    setPreviewLoading(true);
    try {
      const res = await previewCampaignStep(ctx, campaign.id, position);
      setPreview(res);
    } catch (e) {
      setPreviewErr(formatApiError(e));
    } finally {
      setPreviewLoading(false);
    }
  };

  const closePreview = () => {
    setPreviewOpen(false);
    setPreview(null);
    setPreviewErr(null);
  };

  if (!campaign) {
    return (
      <div className="text-sm text-slate-500">
        {err ?? "Loading campaign…"}{" "}
        <Link className="underline" href="/campaigns">
          ← back
        </Link>
      </div>
    );
  }

  const steps = [...(campaign.steps ?? [])].sort(
    (a, b) => a.position - b.position,
  );
  const suggestedPosition =
    steps.length > 0 ? Math.max(...steps.map((s) => s.position)) + 1 : 0;
  const canActivate =
    campaign.status === "draft" || campaign.status === "paused";
  const canPause = campaign.status === "active";
  const canArchive =
    campaign.status === "draft" ||
    campaign.status === "paused" ||
    campaign.status === "active";

  return (
    <div className="space-y-6">
      <div>
        <Link
          className="text-sm text-slate-600 hover:text-slate-900"
          href="/campaigns"
        >
          ← back to campaigns
        </Link>
      </div>

      <MetaPanel campaign={campaign} onSaved={setCampaign} />

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
        <div className="flex items-center justify-between">
          <h2 className="font-heading text-base font-semibold text-slate-900">
            Steps
          </h2>
          <span className="text-xs text-slate-500">
            {steps.length === 0
              ? "No steps yet"
              : `${steps.length} step${steps.length === 1 ? "" : "s"}`}
          </span>
        </div>
        {steps.length > 0 ? (
          <ul className="divide-y divide-slate-100 rounded border border-slate-200">
            {steps.map((s) => (
              <StepRow
                key={s.id}
                step={s}
                busy={stepBusyPos === s.position}
                onDelete={onDeleteStep}
                onPreview={onPreviewStep}
              />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">
            Add a step below to start authoring this campaign.
          </p>
        )}
        <AddCampaignStepForm
          campaignId={campaign.id}
          suggestedPosition={suggestedPosition}
          onAdded={load}
        />
      </section>

      <section className="flex flex-wrap gap-2 rounded border border-slate-200 bg-slate-50 p-4">
        {canActivate ? (
          <button
            type="button"
            className={BTN_PRIMARY}
            disabled={actionBusy || steps.length === 0}
            onClick={onActivate}
            title={
              steps.length === 0 ? "Add at least one step before activating" : undefined
            }
          >
            {actionBusy ? "Working…" : "Activate"}
          </button>
        ) : null}
        {canPause ? (
          <button
            type="button"
            className={BTN_SECONDARY}
            disabled={actionBusy}
            onClick={onPause}
          >
            {actionBusy ? "Working…" : "Pause"}
          </button>
        ) : null}
        {canArchive ? (
          <button
            type="button"
            className={BTN_DANGER}
            disabled={actionBusy}
            onClick={onArchive}
          >
            {actionBusy ? "Working…" : "Archive"}
          </button>
        ) : null}
      </section>

      {previewOpen ? (
        <PreviewModal
          preview={preview}
          loading={previewLoading}
          err={previewErr}
          onClose={closePreview}
        />
      ) : null}
    </div>
  );
}
