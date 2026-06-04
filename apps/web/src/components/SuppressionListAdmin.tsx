"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

import type { paths } from "@jp-adopt/contracts";

import {
  addSuppression,
  ApiError,
  formatApiError,
  listSuppression,
  removeSuppression,
} from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";
import { formatTimestamp } from "../lib/vocab";
import { DataRow, DataTable, EmptyState, LoadingRows } from "./DataTable";

type SuppressionRow =
  paths["/v1/suppression-list"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];

const PAGE_SIZE = 50;
const BTN =
  "rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50";
const BTN_PRIMARY =
  "rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50";

function shortHash(hash: string): string {
  return hash.length > 12 ? `${hash.slice(0, 12)}…` : hash;
}

export function SuppressionListAdmin() {
  const ctx = useApiContext();
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [items, setItems] = useState<SuppressionRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [reason, setReason] = useState("manual");
  const [removingHash, setRemovingHash] = useState<string | null>(null);
  const [isSubmitting, startSubmit] = useTransition();

  const load = useCallback(
    async (nextOffset: number) => {
      setLoading(true);
      setErr(null);
      setForbidden(false);
      try {
        const res = await listSuppression(ctx, {
          limit: PAGE_SIZE,
          offset: nextOffset,
        });
        setItems(res.items);
        setTotal(res.total);
        setOffset(nextOffset);
      } catch (e) {
        if (e instanceof ApiError && e.status === 403) {
          setForbidden(true);
          setItems([]);
          setTotal(0);
          return;
        }
        setErr(formatApiError(e));
      } finally {
        setLoading(false);
      }
    },
    [ctx],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  const onAdd = () => {
    const trimmed = email.trim();
    const trimmedReason = reason.trim() || "manual";
    if (!trimmed) {
      setErr("Enter an email address.");
      return;
    }
    setErr(null);
    startSubmit(() => {
      void (async () => {
        try {
          await addSuppression(ctx, { email: trimmed, reason: trimmedReason });
          setEmail("");
          setReason("manual");
          await load(0);
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  };

  const onRemove = (hash: string) => {
    if (!window.confirm("Remove this address from the suppression list?")) return;
    setRemovingHash(hash);
    setErr(null);
    void (async () => {
      try {
        await removeSuppression(ctx, hash);
        await load(offset);
      } catch (e) {
        setErr(formatApiError(e));
      } finally {
        setRemovingHash(null);
      }
    })();
  };

  if (forbidden) {
    return (
      <EmptyState
        title="You can't manage the suppression list"
        description="Suppression management is gated to staff_admin and adoption_manager roles."
      />
    );
  }

  const canPrev = offset > 0;
  const canNext = offset + items.length < total;

  return (
    <div className="space-y-4">
      <section className="space-y-3 rounded border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-medium text-slate-700">Add address</h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="sm:col-span-2 block text-xs text-slate-600">
            Email
            <input
              type="email"
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@example.com"
            />
          </label>
          <label className="block text-xs text-slate-600">
            Reason
            <input
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
              value={reason}
              maxLength={64}
              onChange={(e) => setReason(e.target.value)}
              placeholder="manual"
            />
          </label>
        </div>
        <div className="flex justify-end">
          <button
            type="button"
            className={BTN_PRIMARY}
            disabled={isSubmitting || !email.trim()}
            onClick={onAdd}
          >
            {isSubmitting ? "Adding…" : "Add to suppression list"}
          </button>
        </div>
      </section>

      {err ? (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
          {err}
        </div>
      ) : null}

      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-600">
          {loading
            ? "Loading…"
            : `${total} address${total === 1 ? "" : "es"} suppressed`}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            className={BTN}
            disabled={loading || !canPrev}
            onClick={() => load(Math.max(0, offset - PAGE_SIZE))}
          >
            ← Prev
          </button>
          <button
            type="button"
            className={BTN}
            disabled={loading || !canNext}
            onClick={() => load(offset + PAGE_SIZE)}
          >
            Next →
          </button>
        </div>
      </div>

      <DataTable
        rows={
          loading ? (
            <LoadingRows />
          ) : items.length === 0 ? null : (
            items.map((r) => (
              <DataRow
                key={r.email_hash}
                id={r.email_hash}
                title={
                  <span
                    className="font-mono text-sm"
                    title={r.email_hash}
                  >
                    {shortHash(r.email_hash)}
                  </span>
                }
                meta={
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
                    {r.reason}
                  </span>
                }
                subtle={`Suppressed ${formatTimestamp(r.suppressed_at)}`}
                action={
                  <button
                    type="button"
                    disabled={removingHash === r.email_hash}
                    onClick={() => onRemove(r.email_hash)}
                    className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50"
                  >
                    {removingHash === r.email_hash ? "Removing…" : "Remove"}
                  </button>
                }
              />
            ))
          )
        }
        empty={
          <EmptyState
            title="No suppressed addresses."
            description="Add an address above or wait for hard-bounce auto-suppression to land."
          />
        }
      />
    </div>
  );
}
