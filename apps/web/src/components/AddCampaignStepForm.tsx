"use client";

import { useEffect, useState, useTransition } from "react";
import dynamic from "next/dynamic";

import {
  addCampaignStep,
  formatApiError,
  listMergeTokens,
} from "../lib/api-client";
import { BTN_PRIMARY, BTN_SECONDARY } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";
import type { MergeTokenDef } from "./editor/MergeToken";

const RichTextEditor = dynamic(
  () => import("./editor/RichTextEditor").then((m) => m.RichTextEditor),
  { ssr: false },
);

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
  // null = tokens not loaded yet; the editor must mount with tokens present.
  const [tokens, setTokens] = useState<MergeTokenDef[] | null>(null);
  const [position, setPosition] = useState(suggestedPosition);
  const [delayDays, setDelayDays] = useState(0);
  const [bodyHtml, setBodyHtml] = useState("");
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
    let ignore = false;
    listMergeTokens(ctx)
      .then((res) => !ignore && setTokens(res.items))
      .catch(() => !ignore && setTokens([]));
    return () => {
      ignore = true;
    };
  }, [open, ctx]);

  const reset = () => {
    setOpen(false);
    setPosition(suggestedPosition);
    setDelayDays(0);
    setBodyHtml("");
    setSubject("");
    setSendAtHour(9);
    setSendAtMinute(0);
    setErr(null);
  };

  const onSubmit = () => {
    const trimmedSubject = subject.trim();
    if (!trimmedSubject) {
      setErr("Subject is required.");
      return;
    }
    if (!bodyHtml.trim()) {
      setErr("Body is required.");
      return;
    }
    setErr(null);
    startSubmit(() => {
      void (async () => {
        try {
          await addCampaignStep(ctx, campaignId, {
            position,
            delay_days: delayDays,
            body_html: bodyHtml,
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
        Subject
        <input
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          value={subject}
          maxLength={512}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Welcome to the cohort"
        />
      </label>
      <div className="block text-xs text-slate-600">
        Body
        <div className="mt-1">
          {tokens !== null ? (
            <RichTextEditor
              value={bodyHtml}
              onChange={setBodyHtml}
              tokens={tokens}
            />
          ) : (
            <div className="min-h-[10rem] rounded border border-slate-300 bg-slate-50" />
          )}
        </div>
      </div>
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
          disabled={busy || !subject.trim() || !bodyHtml.trim()}
          onClick={onSubmit}
        >
          {busy ? "Adding…" : "Add step"}
        </button>
      </div>
    </div>
  );
}
