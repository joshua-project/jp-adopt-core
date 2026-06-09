"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

import type { paths } from "@jp-adopt/contracts";

import {
  ApiError,
  formatApiError,
  listIntakeKeys,
  mintIntakeKey,
  revokeIntakeKey,
} from "../lib/api-client";
import { BTN, BTN_PRIMARY, BTN_SECONDARY } from "../lib/button-styles";
import { useApiContext } from "../lib/useApiContext";
import { EmptyState, LoadingRows } from "./DataTable";
import { formatTimestamp } from "../lib/vocab";

type Key =
  paths["/v1/admin/intake-keys"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];
type Minted =
  paths["/v1/admin/intake-keys"]["post"]["responses"]["201"]["content"]["application/json"];

export function IntakeKeysAdmin() {
  const ctx = useApiContext();
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [items, setItems] = useState<Key[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [minted, setMinted] = useState<Minted | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setForbidden(false);
    try {
      const res = await listIntakeKeys(ctx);
      setItems(res.items);
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

  const onRevoke = async (k: Key) => {
    if (
      !window.confirm(
        `Revoke "${k.consumer_label}"? Any consumer using this key will start getting 401 immediately.`,
      )
    )
      return;
    setRevokingId(k.id);
    setErr(null);
    try {
      await revokeIntakeKey(ctx, k.id);
      await load();
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setRevokingId(null);
    }
  };

  if (forbidden) {
    return (
      <EmptyState
        title="You can't manage intake API keys"
        description="This page is gated to staff_admin."
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-600">
          {loading
            ? "Loading…"
            : `${items.length} key${items.length === 1 ? "" : "s"}`}
        </span>
        <button
          type="button"
          className={BTN_PRIMARY}
          onClick={() => setShowNew(true)}
        >
          + Mint key
        </button>
      </div>

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      {loading ? (
        <LoadingRows />
      ) : items.length === 0 ? (
        <EmptyState
          title="No intake API keys yet"
          description='Mint one above for jp-adopt-forms or any other server-to-server intake consumer.'
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50/50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2 font-medium">Consumer</th>
                <th className="px-4 py-2 font-medium">Last used</th>
                <th className="px-4 py-2 font-medium">Created</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((k) => (
                <tr key={k.id}>
                  <td className="px-4 py-3 text-slate-800">
                    <div className="font-medium text-slate-900">
                      {k.consumer_label}
                    </div>
                    {k.note ? (
                      <div className="text-xs text-slate-500">{k.note}</div>
                    ) : null}
                  </td>
                  <td className="px-4 py-3 text-slate-600">
                    {k.last_used_at ? (
                      <>
                        <div>{formatTimestamp(k.last_used_at)}</div>
                        <div className="text-[11px] text-slate-400">
                          {k.last_used_ip ?? "—"}
                        </div>
                      </>
                    ) : (
                      <span className="text-slate-400">never</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-slate-600">
                    {formatTimestamp(k.created_at)}
                  </td>
                  <td className="px-4 py-3 text-slate-800">
                    {k.revoked_at ? (
                      <span className="rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
                        Revoked {formatTimestamp(k.revoked_at)}
                      </span>
                    ) : (
                      <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-800">
                        Active
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {!k.revoked_at ? (
                      <button
                        type="button"
                        onClick={() => onRevoke(k)}
                        disabled={revokingId === k.id}
                        className="text-sm font-medium text-red-700 hover:text-red-900 disabled:opacity-50"
                      >
                        {revokingId === k.id ? "Revoking…" : "Revoke"}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showNew ? (
        <MintModal
          onClose={() => setShowNew(false)}
          onMinted={(m) => {
            setShowNew(false);
            setMinted(m);
            void load();
          }}
        />
      ) : null}

      {minted ? (
        <MintedReveal minted={minted} onClose={() => setMinted(null)} />
      ) : null}
    </div>
  );
}

function MintModal({
  onClose,
  onMinted,
}: {
  onClose: () => void;
  onMinted: (m: Minted) => void;
}) {
  const ctx = useApiContext();
  const [consumerLabel, setConsumerLabel] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, startMint] = useTransition();

  const onSubmit = () => {
    const trimmed = consumerLabel.trim();
    if (!trimmed) {
      setErr("Consumer label is required.");
      return;
    }
    setErr(null);
    startMint(() => {
      void (async () => {
        try {
          const m = await mintIntakeKey(ctx, {
            consumer_label: trimmed,
            note: note.trim() || null,
          });
          onMinted(m);
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-md space-y-3 rounded-lg bg-white p-5 shadow-xl">
        <h3 className="text-base font-semibold text-slate-900">Mint intake key</h3>
        <label className="block text-xs text-slate-600">
          Consumer label
          <input
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            value={consumerLabel}
            maxLength={128}
            onChange={(e) => setConsumerLabel(e.target.value)}
            placeholder="jp-adopt-forms production"
          />
        </label>
        <label className="block text-xs text-slate-600">
          Note (optional)
          <textarea
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            rows={2}
            value={note}
            maxLength={2048}
            onChange={(e) => setNote(e.target.value)}
          />
        </label>
        {err ? (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
            {err}
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          <button type="button" className={BTN} disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={BTN_PRIMARY}
            disabled={busy || !consumerLabel.trim()}
            onClick={onSubmit}
          >
            {busy ? "Minting…" : "Mint"}
          </button>
        </div>
      </div>
    </div>
  );
}

function MintedReveal({
  minted,
  onClose,
}: {
  minted: Minted;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(minted.plaintext);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API isn't available on every browser/origin combo.
      // Fall back: user can select + copy manually from the textarea.
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-lg space-y-3 rounded-lg bg-white p-5 shadow-xl">
        <h3 className="text-base font-semibold text-slate-900">
          Copy this key now — it won&apos;t be shown again
        </h3>
        <p className="text-xs text-slate-600">
          Consumer: <span className="font-medium">{minted.consumer_label}</span>
        </p>
        <textarea
          readOnly
          rows={3}
          className="w-full break-all rounded border border-slate-300 bg-slate-50 px-2 py-1.5 font-mono text-xs text-slate-900"
          value={minted.plaintext}
          onFocus={(e) => e.currentTarget.select()}
        />
        <div className="flex justify-end gap-2">
          <button type="button" className={BTN_SECONDARY} onClick={copy}>
            {copied ? "Copied!" : "Copy to clipboard"}
          </button>
          <button type="button" className={BTN_PRIMARY} onClick={onClose}>
            I&apos;ve saved it
          </button>
        </div>
      </div>
    </div>
  );
}
