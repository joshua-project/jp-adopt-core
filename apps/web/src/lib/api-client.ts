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
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured && configured.startsWith("/")) {
    // A relative value ("/api") means "same origin, proxied by Next" — the
    // web Container App rewrites /api/* to the internal API. new URL(...) in
    // request() needs an absolute base. In the browser, resolve against the
    // current origin. All API calls run client-side today, so the branch
    // below is defensive: a server-side caller can't resolve a relative base
    // against an origin, so fall back to the internal API target (the same
    // host the /api proxy forwards to) and finally to the dev API.
    if (typeof window !== "undefined") {
      return `${window.location.origin}${configured}`;
    }
    return process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";
  }
  if (configured && configured.length > 0) {
    return configured;
  }
  // Dev default: web (`next dev`) talks to the API directly.
  return "http://127.0.0.1:8000";
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
    super(message ?? extractErrorMessage(body, status));
    this.status = status;
    this.body = body;
  }
}

/**
 * Pull a user-facing message off the error body. Handles both shapes the
 * /v1/ API currently returns:
 *   - intake-style `{error: {message}}`
 *   - HTTPException-style `{detail: <string>}` (contacts)
 *   - HTTPException-style `{detail: {code, message}}` (matches, workflow, magic-link, admin)
 * Falls back to `HTTP <status>` so the message never collapses to empty.
 */
function extractErrorMessage(body: unknown, status: number): string {
  if (typeof body !== "object" || body === null) return `HTTP ${status}`;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && detail !== null) {
    const detailMessage = (detail as { message?: unknown }).message;
    if (typeof detailMessage === "string" && detailMessage) return detailMessage;
    const detailCode = (detail as { code?: unknown }).code;
    if (typeof detailCode === "string" && detailCode) return detailCode;
  }
  const error = (body as { error?: unknown }).error;
  if (typeof error === "object" && error !== null) {
    const errorMessage = (error as { message?: unknown }).message;
    if (typeof errorMessage === "string" && errorMessage) return errorMessage;
  }
  return `HTTP ${status}`;
}

export interface ApiRequestInit {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** Will be JSON.stringify'd if not already a string. */
  body?: unknown;
  /** Additional headers merged on top of the default Authorization + Content-Type. */
  headers?: Record<string, string>;
  /** Caller-supplied query params; values are URL-encoded. */
  query?: Record<string, string | number | boolean | undefined>;
  /**
   * F18: optional AbortSignal so callers can cancel an in-flight request
   * on unmount or a fresh query. Forwarded to the underlying ``fetch`` so
   * the browser actually tears down the connection.
   */
  signal?: AbortSignal;
}

/**
 * F28: returns ``T | undefined`` so callers that hit a 204 endpoint don't
 * silently get a fake-typed undefined. The wrapper resolves to ``undefined``
 * only on 204; every other status either returns the parsed body or throws.
 */
export async function apiFetch<T = unknown>(
  ctx: ApiClientContext,
  path: string,
  init: ApiRequestInit = {},
): Promise<T | undefined> {
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
    signal: init.signal,
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
  if (res.status === 204) return undefined;
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

// F36: ``persistDevToken`` / ``readDevToken`` were exported but unused. The
// dev-token override is set entirely via ``ApiClientContext.devToken``
// today; if a UI surface ever needs to persist it, re-introduce these
// exports at that time.

/**
 * F28: typed wrappers below know their endpoints never return 204, so they
 * narrow the wrapper's ``T | undefined`` back to ``T`` for callers. If a
 * future wrapper hits a 204 endpoint, declare its return type as
 * ``Promise<T | undefined>`` and skip the assertion.
 */
function _assertPresent<T>(value: T | undefined, endpoint: string): T {
  if (value === undefined) {
    throw new Error(
      `apiFetch(${endpoint}) returned undefined; this endpoint should not 204`,
    );
  }
  return value;
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
  return _assertPresent(
    await apiFetch<QueueResponseBody>(ctx, "/v1/matches/queue"),
    "/v1/matches/queue",
  );
}

export async function getMatch(
  ctx: ApiClientContext,
  matchId: string,
): Promise<MatchResponseBody> {
  return _assertPresent(
    await apiFetch<MatchResponseBody>(ctx, `/v1/matches/${matchId}`),
    `/v1/matches/${matchId}`,
  );
}

export async function decideMatch(
  ctx: ApiClientContext,
  matchId: string,
  body: DecideRequestBody,
): Promise<DecideResponseBody> {
  return _assertPresent(
    await apiFetch<DecideResponseBody>(ctx, `/v1/matches/${matchId}/decide`, {
      method: "POST",
      body,
    }),
    `/v1/matches/${matchId}/decide`,
  );
}

export async function runMatch(
  ctx: ApiClientContext,
  contactId: string,
  body: RunMatchRequestBody = { force: false },
): Promise<RunMatchResponseBody> {
  return _assertPresent(
    await apiFetch<RunMatchResponseBody>(ctx, `/v1/matches/run/${contactId}`, {
      method: "POST",
      body,
    }),
    `/v1/matches/run/${contactId}`,
  );
}

export async function transitionContact(
  ctx: ApiClientContext,
  contactId: string,
  body: TransitionRequestBody,
): Promise<TransitionResponseBody> {
  return _assertPresent(
    await apiFetch<TransitionResponseBody>(
      ctx,
      `/v1/contacts/${contactId}/transition`,
      {
        method: "POST",
        body,
      },
    ),
    `/v1/contacts/${contactId}/transition`,
  );
}
