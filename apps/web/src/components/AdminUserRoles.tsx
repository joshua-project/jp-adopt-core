"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

import type { paths } from "@jp-adopt/contracts";

import { ApiError, apiFetch } from "../lib/api-client";
import { useApiContext } from "../lib/useApiContext";

type UserRoleListResponse =
  paths["/v1/admin/user-roles"]["get"]["responses"]["200"]["content"]["application/json"];
type RoleListResponse =
  paths["/v1/admin/roles"]["get"]["responses"]["200"]["content"]["application/json"];
type UserRoleRead =
  paths["/v1/admin/user-roles"]["post"]["responses"]["200"]["content"]["application/json"];

function formatApiError(e: unknown): string {
  if (e instanceof ApiError) {
    const body =
      typeof e.body === "object" && e.body !== null && "detail" in e.body
        ? (e.body as { detail: unknown }).detail
        : null;
    if (typeof body === "object" && body !== null && "code" in body) {
      return `${(body as { code: string }).code}: ${
        (body as { message?: string }).message ?? e.message
      }`;
    }
    return e.message;
  }
  return e instanceof Error ? e.message : "Request failed";
}

export function AdminUserRoles() {
  const ctx = useApiContext();
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [items, setItems] = useState<UserRoleListResponse["items"]>([]);
  const [roles, setRoles] = useState<RoleListResponse["items"]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [userSubjectId, setUserSubjectId] = useState("");
  const [roleId, setRoleId] = useState("");
  const [isSubmitting, startSubmit] = useTransition();

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setForbidden(false);
    try {
      const [list, roleList] = await Promise.all([
        apiFetch<UserRoleListResponse>(ctx, "/v1/admin/user-roles"),
        apiFetch<RoleListResponse>(ctx, "/v1/admin/roles"),
      ]);
      setItems(list?.items ?? []);
      const roleItems = roleList?.items ?? [];
      setRoles(roleItems);
      setRoleId((prev) => prev || (roleItems[0]?.id ?? ""));
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

  const grant = useCallback(() => {
    setErr(null);
    setMsg(null);
    const oid = userSubjectId.trim();
    if (!oid || !roleId) {
      setErr("Enter an Entra user OID and select a role.");
      return;
    }
    startSubmit(() => {
      void (async () => {
        try {
          const granted = await apiFetch<UserRoleRead>(ctx, "/v1/admin/user-roles", {
            method: "POST",
            body: { user_subject_id: oid, role_id: roleId },
          });
          setMsg(
            granted
              ? `Granted ${granted.role_name} to ${granted.user_subject_id}.`
              : "Role granted.",
          );
          setUserSubjectId("");
          await load();
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    });
  }, [ctx, load, roleId, userSubjectId]);

  const revoke = useCallback(
    (row: UserRoleListResponse["items"][number]) => {
      if (
        !window.confirm(
          `Revoke ${row.role_name} from ${row.user_subject_id}?`,
        )
      ) {
        return;
      }
      setErr(null);
      setMsg(null);
      void (async () => {
        try {
          await apiFetch(ctx, `/v1/admin/user-roles/${encodeURIComponent(row.user_subject_id)}/${row.role_id}`, {
            method: "DELETE",
          });
          setMsg(`Revoked ${row.role_name} from ${row.user_subject_id}.`);
          await load();
        } catch (e) {
          setErr(formatApiError(e));
        }
      })();
    },
    [ctx, load],
  );

  if (forbidden) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <p className="text-sm text-slate-700">
          You don&apos;t have admin access. This page requires the{" "}
          <code className="text-xs">staff_admin</code> role.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {err ? (
        <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {err}
        </p>
      ) : null}
      {msg ? (
        <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
          {msg}
        </p>
      ) : null}

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-900">Grant role</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="text-slate-600">Entra user OID (UUID)</span>
            <input
              type="text"
              value={userSubjectId}
              onChange={(e) => setUserSubjectId(e.target.value)}
              placeholder="e.g. 546dce1f-9e3b-422d-a938-f5a9437f164e"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <label className="block text-sm">
            <span className="text-slate-600">Role</span>
            <select
              value={roleId}
              onChange={(e) => setRoleId(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              {roles.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <button
          type="button"
          onClick={grant}
          disabled={isSubmitting || loading}
          className="mt-4 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:opacity-50"
        >
          {isSubmitting ? "Granting…" : "Grant role"}
        </button>
      </section>

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-200 bg-slate-50/70 px-4 py-2 text-xs font-medium uppercase tracking-wide text-slate-500">
          Current grants
        </div>
        {loading ? (
          <p className="px-4 py-6 text-sm text-slate-500">Loading…</p>
        ) : items.length === 0 ? (
          <p className="px-4 py-6 text-sm text-slate-500">No role grants yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="border-b border-slate-200 bg-slate-50/50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-medium">User subject ID</th>
                  <th className="px-4 py-2 font-medium">Role</th>
                  <th className="px-4 py-2 font-medium">Granted at</th>
                  <th className="px-4 py-2 font-medium">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((row) => (
                  <tr key={`${row.user_subject_id}-${row.role_id}`}>
                    <td className="max-w-xs truncate px-4 py-3 font-mono text-xs text-slate-800">
                      {row.user_subject_id}
                    </td>
                    <td className="px-4 py-3 text-slate-800">{row.role_name}</td>
                    <td className="px-4 py-3 text-slate-600">
                      {new Date(row.granted_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        type="button"
                        onClick={() => revoke(row)}
                        className="text-sm font-medium text-red-700 hover:text-red-900"
                      >
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
