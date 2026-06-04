"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

import { createCampaign, formatApiError } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

const BTN =
  "rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50";

const TRIGGER_TYPES = ["event", "manual"] as const;

export function NewCampaignModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const ctx = useApiContext();
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [triggerType, setTriggerType] =
    useState<(typeof TRIGGER_TYPES)[number]>("event");
  const [triggerEventType, setTriggerEventType] = useState("");
  const [precedence, setPrecedence] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [busy, startSubmit] = useTransition();

  const onSubmit = () => {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setErr("Name is required.");
      return;
    }
    if (triggerType === "event" && !triggerEventType.trim()) {
      setErr("Event trigger requires a trigger_event_type.");
      return;
    }
    setErr(null);
    startSubmit(() => {
      void (async () => {
        try {
          const created = await createCampaign(ctx, {
            name: trimmedName,
            description: description.trim() || null,
            trigger_type: triggerType,
            trigger_event_type:
              triggerType === "event"
                ? triggerEventType.trim()
                : null,
            auto_enroll_existing: false,
            precedence,
          });
          onCreated();
          // Land on the new draft's detail page for step authoring.
          router.push(`/campaigns/${created.id}`);
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-lg space-y-3 rounded-lg bg-white p-5 shadow-xl">
        <h3 className="text-base font-semibold text-slate-900">
          New drip campaign
        </h3>
        <label className="block text-xs text-slate-600">
          Name
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={name}
            maxLength={256}
            onChange={(e) => setName(e.target.value)}
            placeholder="Facilitator welcome"
          />
        </label>
        <label className="block text-xs text-slate-600">
          Description (optional)
          <textarea
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            rows={2}
            value={description}
            maxLength={1024}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs text-slate-600">
            Trigger type
            <select
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
              value={triggerType}
              onChange={(e) =>
                setTriggerType(
                  e.target.value as (typeof TRIGGER_TYPES)[number],
                )
              }
            >
              {TRIGGER_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
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
        {triggerType === "event" ? (
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
        {err ? (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
            {err}
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className={BTN}
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="button"
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
            disabled={busy || !name.trim()}
            onClick={onSubmit}
          >
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
