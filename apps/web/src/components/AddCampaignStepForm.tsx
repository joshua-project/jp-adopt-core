"use client";

import { useEffect, useState, useTransition } from "react";

import {
  addCampaignStep,
  formatApiError,
  listDripTemplates,
} from "../lib/api-client";
import { BTN_PRIMARY, BTN_SECONDARY } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";

export function AddCampaignStepForm({
  campaignId,
  suggestedPosition,
  onAdded,
}: {
  campaignId: string;
  suggestedPosition: number;
  onAdded: () => void;
}) {
  const ctx = useApiContext();
  const [open, setOpen] = useState(false);
  const [templates, setTemplates] = useState<string[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templatesErr, setTemplatesErr] = useState<string | null>(null);
  const [position, setPosition] = useState(suggestedPosition);
  const [delayDays, setDelayDays] = useState(0);
  const [template, setTemplate] = useState("");
  const [subject, setSubject] = useState("");
  const [sendAtHour, setSendAtHour] = useState(9);
  const [sendAtMinute, setSendAtMinute] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [busy, startSubmit] = useTransition();

  useEffect(() => {
    setPosition(suggestedPosition);
  }, [suggestedPosition]);

  useEffect(() => {
    if (!open) return;
    setTemplatesLoading(true);
    setTemplatesErr(null);
    listDripTemplates(ctx)
      .then((res) => {
        const names = res.items.map((t) => t.name);
        setTemplates(names);
        if (names.length > 0 && !template) setTemplate(names[0]);
      })
      .catch((e) => setTemplatesErr(formatApiError(e)))
      .finally(() => setTemplatesLoading(false));
    // We intentionally re-fetch on open only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ctx]);

  const reset = () => {
    setOpen(false);
    setPosition(suggestedPosition);
    setDelayDays(0);
    setTemplate(templates[0] ?? "");
    setSubject("");
    setSendAtHour(9);
    setSendAtMinute(0);
    setErr(null);
  };

  const onSubmit = () => {
    const trimmedSubject = subject.trim();
    if (!template) {
      setErr("Pick a template.");
      return;
    }
    if (!trimmedSubject) {
      setErr("Subject is required.");
      return;
    }
    setErr(null);
    startSubmit(() => {
      void (async () => {
        try {
          await addCampaignStep(ctx, campaignId, {
            position,
            delay_days: delayDays,
            mjml_template_name: template,
            subject: trimmedSubject,
            send_at_hour: sendAtHour,
            send_at_minute: sendAtMinute,
          });
          reset();
          onAdded();
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  if (!open) {
    return (
      <button type="button" className={BTN_PRIMARY} onClick={() => setOpen(true)}>
        + Add step
      </button>
    );
  }

  return (
    <div className="space-y-3 rounded border border-slate-200 bg-slate-50/80 p-4">
      <h3 className="text-sm font-medium text-slate-800">Add step</h3>
      {templatesErr ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900">
          {templatesErr}
        </div>
      ) : null}
      {!templatesLoading && templates.length === 0 ? (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          No MJML templates available — drop a file in{" "}
          <code className="font-mono">apps/api/email-templates/</code>.
        </div>
      ) : null}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <label className="block text-xs text-slate-600">
          Position
          <input
            type="number"
            min={0}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={position}
            onChange={(e) => setPosition(Number(e.target.value) || 0)}
          />
        </label>
        <label className="block text-xs text-slate-600">
          Delay (days)
          <input
            type="number"
            min={0}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={delayDays}
            onChange={(e) => setDelayDays(Number(e.target.value) || 0)}
          />
        </label>
        <label className="block text-xs text-slate-600">
          Hour
          <input
            type="number"
            min={0}
            max={23}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={sendAtHour}
            onChange={(e) =>
              setSendAtHour(Math.max(0, Math.min(23, Number(e.target.value) || 0)))
            }
          />
        </label>
        <label className="block text-xs text-slate-600">
          Minute
          <input
            type="number"
            min={0}
            max={59}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={sendAtMinute}
            onChange={(e) =>
              setSendAtMinute(
                Math.max(0, Math.min(59, Number(e.target.value) || 0)),
              )
            }
          />
        </label>
      </div>
      <label className="block text-xs text-slate-600">
        Template
        <select
          className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm font-mono"
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          disabled={templatesLoading || templates.length === 0}
        >
          {templatesLoading ? (
            <option value="">Loading…</option>
          ) : templates.length === 0 ? (
            <option value="">No templates</option>
          ) : (
            templates.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))
          )}
        </select>
      </label>
      <label className="block text-xs text-slate-600">
        Subject
        <input
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          value={subject}
          maxLength={512}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Welcome to the cohort"
        />
      </label>
      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          className={BTN_SECONDARY}
          disabled={busy}
          onClick={reset}
        >
          Cancel
        </button>
        <button
          type="button"
          className={BTN_PRIMARY}
          disabled={busy || !template || !subject.trim()}
          onClick={onSubmit}
        >
          {busy ? "Adding…" : "Add step"}
        </button>
      </div>
    </div>
  );
}
