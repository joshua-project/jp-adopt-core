"use client";

import { InteractionRequiredAuthError } from "@azure/msal-browser";
import type { IPublicClientApplication, AccountInfo } from "@azure/msal-browser";

import type { paths } from "@jp-adopt/contracts";

import { getApiScopeList, isB2cClientConfigured } from "./b2c/msalConfig";

/**
 * Shared fetch wrapper that injects the MSAL bearer (or dev-local token when
 * B2C isn't configured). Used by every Client Component that calls /v1/*.
 *
 * Designed to be tree-shakeable: keeps the typed-paths surface in one file so
 * generated routes don't leak into individual components.
 */

const DEV_TOKEN_STORAGE_KEY = "jp_adopt_bearer";

export type ApiPaths = paths;

export interface ApiClientContext {
  instance: IPublicClientApplication | null;
  accounts: readonly AccountInfo[];
  /** Optional override; consumed when B2C is not configured (dev-local). */
  devToken?: string;
}

export function getBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
}

/** True when the dev-token textbox should be shown in the UI. */
export function isDevTokenAvailable(): boolean {
  if (typeof window === "undefined") return false;
  return !isB2cClientConfigured();
}

export async function resolveAccessToken(
  ctx: ApiClientContext,
): Promise<string> {
  if (!isB2cClientConfigured()) {
    const stored =
      typeof window !== "undefined"
        ? window.localStorage.getItem(DEV_TOKEN_STORAGE_KEY)
        : null;
    const candidate = ctx.devToken?.trim() || stored?.trim() || "dev-local";
    return candidate;
  }

  if (!ctx.instance) {
    throw new Error("MSAL instance is not initialized");
  }
  const scopes = getApiScopeList();
  if (scopes.length === 0) {
    throw new Error(
      "NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES is required for B2C-authenticated requests",
    );
  }
  const account = ctx.instance.getActiveAccount() ?? ctx.accounts[0] ?? null;
  if (!account) {
    throw new Error("No active MSAL account; sign in first");
  }
  try {
    const result = await ctx.instance.acquireTokenSilent({
      account,
      scopes,
      forceRefresh: false,
    });
    return result.accessToken;
  } catch (e) {
    if (e instanceof InteractionRequiredAuthError) {
      const result = await ctx.instance.acquireTokenPopup({ account, scopes });
      return result.accessToken;
    }
    throw e;
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(
      message ??
        (typeof body === "object" &&
        body !== null &&
        "detail" in body &&
        typeof (body as { detail?: unknown }).detail === "string"
          ? (body as { detail: string }).detail
          : `HTTP ${status}`),
    );
    this.status = status;
    this.body = body;
  }
}

export interface ApiRequestInit {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** Will be JSON.stringify'd if not already a string. */
  body?: unknown;
  /** Additional headers merged on top of the default Authorization + Content-Type. */
  headers?: Record<string, string>;
  /** Caller-supplied query params; values are URL-encoded. */
  query?: Record<string, string | number | boolean | undefined>;
}

export async function apiFetch<T = unknown>(
  ctx: ApiClientContext,
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  const token = await resolveAccessToken(ctx);
  const url = new URL(`${getBaseUrl()}${path}`);
  if (init.query) {
    for (const [k, v] of Object.entries(init.query)) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
  }
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    ...(init.headers ?? {}),
  };
  let body: BodyInit | undefined;
  if (init.body !== undefined && init.body !== null) {
    if (typeof init.body === "string") {
      body = init.body;
    } else {
      body = JSON.stringify(init.body);
      headers["Content-Type"] = headers["Content-Type"] ?? "application/json";
    }
  }
  const res = await fetch(url.toString(), {
    method: init.method ?? "GET",
    headers,
    body,
    credentials: "omit",
  });
  if (!res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      payload = await res.text();
    }
    throw new ApiError(res.status, payload);
  }
  if (res.status === 204) return undefined as unknown as T;
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

/** Persist (or clear) the dev-local bearer override for non-B2C local dev. */
export function persistDevToken(value: string): void {
  if (typeof window === "undefined") return;
  if (value.trim()) {
    window.localStorage.setItem(DEV_TOKEN_STORAGE_KEY, value.trim());
  } else {
    window.localStorage.removeItem(DEV_TOKEN_STORAGE_KEY);
  }
}

export function readDevToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(DEV_TOKEN_STORAGE_KEY) ?? "";
}

// ── Typed convenience wrappers around the generated paths ────────────────

type QueueResponseBody =
  paths["/v1/matches/queue"]["get"]["responses"]["200"]["content"]["application/json"];
type MatchResponseBody =
  paths["/v1/matches/{match_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type DecideRequestBody =
  paths["/v1/matches/{match_id}/decide"]["post"]["requestBody"]["content"]["application/json"];
type DecideResponseBody =
  paths["/v1/matches/{match_id}/decide"]["post"]["responses"]["200"]["content"]["application/json"];
type RunMatchRequestBody =
  paths["/v1/matches/run/{contact_id}"]["post"]["requestBody"]["content"]["application/json"];
type RunMatchResponseBody =
  paths["/v1/matches/run/{contact_id}"]["post"]["responses"]["200"]["content"]["application/json"];
type TransitionRequestBody =
  paths["/v1/contacts/{contact_id}/transition"]["post"]["requestBody"]["content"]["application/json"];
type TransitionResponseBody =
  paths["/v1/contacts/{contact_id}/transition"]["post"]["responses"]["200"]["content"]["application/json"];

export async function getMatchQueue(
  ctx: ApiClientContext,
): Promise<QueueResponseBody> {
  return apiFetch<QueueResponseBody>(ctx, "/v1/matches/queue");
}

export async function getMatch(
  ctx: ApiClientContext,
  matchId: string,
): Promise<MatchResponseBody> {
  return apiFetch<MatchResponseBody>(ctx, `/v1/matches/${matchId}`);
}

export async function decideMatch(
  ctx: ApiClientContext,
  matchId: string,
  body: DecideRequestBody,
): Promise<DecideResponseBody> {
  return apiFetch<DecideResponseBody>(ctx, `/v1/matches/${matchId}/decide`, {
    method: "POST",
    body,
  });
}

export async function runMatch(
  ctx: ApiClientContext,
  contactId: string,
  body: RunMatchRequestBody = { force: false },
): Promise<RunMatchResponseBody> {
  return apiFetch<RunMatchResponseBody>(ctx, `/v1/matches/run/${contactId}`, {
    method: "POST",
    body,
  });
}

export async function transitionContact(
  ctx: ApiClientContext,
  contactId: string,
  body: TransitionRequestBody,
): Promise<TransitionResponseBody> {
  return apiFetch<TransitionResponseBody>(
    ctx,
    `/v1/contacts/${contactId}/transition`,
    {
      method: "POST",
      body,
    },
  );
}
