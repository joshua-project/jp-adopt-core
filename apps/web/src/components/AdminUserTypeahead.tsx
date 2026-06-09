"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import type { paths } from "@jp-adopt/contracts";

import { formatApiError, searchAdminUsers } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

type Hit =
  paths["/v1/admin/users/search"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number];

const DEBOUNCE_MS = 250;
const _ENTRA_OID_PATTERN =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/**
 * Admin user-picker. Two paths:
 *
 *  - When Graph is configured server-side (`graph_configured: true`),
 *    the operator can search by name / email. Selecting a result
 *    pins the OID into the parent's grant form.
 *  - When Graph is not configured (dev) or the search returns no
 *    results, the operator falls back to typing the raw OID. A
 *    light client-side check validates UUID shape on submit.
 *
 * Selection model: the parent owns the chosen ``user_subject_id``
 * (an OID). This component reports it via ``onChange`` whenever the
 * user types a literal OID or picks a search result.
 */
export function AdminUserTypeahead({
  value,
  onChange,
  onDisplayChange,
  disabled,
}: {
  value: string;
  onChange: (oid: string) => void;
  /** Reported back so the grant button can render "Grant to Amy Adopter". */
  onDisplayChange?: (display: { name: string | null; upn: string | null } | null) => void;
  disabled?: boolean;
}) {
  const ctx = useApiContext();
  const listboxId = useId();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [graphConfigured, setGraphConfigured] = useState<boolean | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Picked = a selection that "owns" the value. Differs from a raw OID
  // the user typed: when picked, we render the display name and skip
  // search on every keystroke.
  const [picked, setPicked] = useState<Hit | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reportDisplay = useCallback(
    (hit: Hit | null) => {
      onDisplayChange?.(
        hit ? { name: hit.display_name, upn: hit.user_principal_name } : null,
      );
    },
    [onDisplayChange],
  );

  // Debounced search. Cancels any in-flight request on a fresh keystroke
  // so stale results never overwrite a newer query.
  useEffect(() => {
    if (picked) return; // picked-mode short-circuits search
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setHits([]);
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ctrl;
    const t = window.setTimeout(() => {
      setLoading(true);
      setErr(null);
      searchAdminUsers(ctx, trimmed, { signal: ctrl.signal })
        .then((res) => {
          if (ctrl.signal.aborted) return;
          setHits(res.items);
          setGraphConfigured(res.graph_configured);
          setOpen(true);
        })
        .catch((e) => {
          if (ctrl.signal.aborted) return;
          setErr(formatApiError(e));
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [query, ctx, picked]);

  const onPick = (hit: Hit) => {
    setPicked(hit);
    setQuery(hit.display_name ?? hit.user_principal_name ?? hit.user_subject_id);
    setOpen(false);
    setHits([]);
    onChange(hit.user_subject_id);
    reportDisplay(hit);
  };

  const onClear = () => {
    setPicked(null);
    setQuery("");
    setHits([]);
    setOpen(false);
    onChange("");
    reportDisplay(null);
  };

  // When the user types raw text, we propagate the value up only if it
  // looks like a UUID. That way the parent's "Grant" button can disable
  // until a valid OID is captured (either typed or picked).
  const onTypeQuery = (next: string) => {
    setPicked(null);
    setQuery(next);
    reportDisplay(null);
    if (_ENTRA_OID_PATTERN.test(next.trim())) {
      onChange(next.trim());
    } else {
      onChange("");
    }
  };

  const showFallbackNotice = graphConfigured === false;

  return (
    <div className="relative">
      <span className="text-slate-600">User (name, email, or OID)</span>
      <div className="mt-1 flex gap-2">
        <input
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          value={query}
          onChange={(e) => onTypeQuery(e.target.value)}
          onFocus={() => hits.length > 0 && setOpen(true)}
          // Listbox is closed by:
          //   - the option's onMouseDown (e.preventDefault keeps focus
          //     here; onPick clears `open` synchronously)
          //   - a fresh value typed (search effect will re-open)
          //   - the parent's Clear button (calls onClear)
          // No setTimeout in onBlur — that pattern leaks pending timers
          // through component unmount and hung the vitest worker.
          onBlur={() => setOpen(false)}
          placeholder={
            graphConfigured === false
              ? "Paste Entra OID (UUID)"
              : "Type a name or email"
          }
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
        />
        {picked ? (
          <button
            type="button"
            onClick={onClear}
            disabled={disabled}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            Clear
          </button>
        ) : null}
      </div>
      {picked ? (
        <p className="mt-1 text-xs text-slate-500">
          <span className="font-mono">{picked.user_subject_id}</span>
          {picked.user_principal_name ? ` · ${picked.user_principal_name}` : ""}
        </p>
      ) : value && _ENTRA_OID_PATTERN.test(value) ? (
        <p className="mt-1 text-xs text-emerald-700">
          OID captured ({value.slice(0, 8)}…)
        </p>
      ) : null}
      {showFallbackNotice ? (
        <p className="mt-1 text-xs text-amber-700">
          Graph user search isn&apos;t wired in this environment. Paste the
          user&apos;s Entra Object ID manually.
        </p>
      ) : null}
      {err ? (
        <p className="mt-1 text-xs text-red-700">{err}</p>
      ) : null}
      {open && hits.length > 0 ? (
        <ul
          role="listbox"
          id={listboxId}
          className="absolute left-0 right-0 z-10 mt-1 max-h-72 overflow-auto rounded-md border border-slate-200 bg-white shadow-lg"
        >
          {hits.map((hit) => (
            <li
              role="option"
              aria-selected={picked?.user_subject_id === hit.user_subject_id}
              key={hit.user_subject_id}
              onMouseDown={(e) => {
                e.preventDefault();
                onPick(hit);
              }}
              className="cursor-pointer px-3 py-2 text-sm hover:bg-slate-50"
            >
              <div className="font-medium text-slate-900">
                {hit.display_name ?? hit.user_principal_name ?? hit.user_subject_id}
              </div>
              {hit.user_principal_name || hit.mail ? (
                <div className="text-xs text-slate-500">
                  {hit.user_principal_name ?? hit.mail}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {loading ? (
        <p className="mt-1 text-xs text-slate-500">Searching…</p>
      ) : null}
    </div>
  );
}
