"use client";

import { InteractionRequiredAuthError } from "@azure/msal-browser";
import type { IPublicClientApplication, AccountInfo } from "@azure/msal-browser";

import type { paths } from "@jp-adopt/contracts";

import { API_ACCESS_SCOPES, isEntraClientConfigured } from "./msalConfig";

/**
 * Shared fetch wrapper that injects the MSAL bearer (or dev-local token when
 * Entra isn't configured). Used by every Client Component that calls /v1/*.
 *
 * Designed to be tree-shakeable: keeps the typed-paths surface in one file so
 * generated routes don't leak into individual components.
 */

const DEV_TOKEN_STORAGE_KEY = "jp_adopt_bearer";

export type ApiPaths = paths;

export interface ApiClientContext {
  instance: IPublicClientApplication | null;
  accounts: readonly AccountInfo[];
  /** Optional override; consumed when Entra is not configured (dev-local). */
  devToken?: string;
}

export function getBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  // A relative value ("/api") means "same origin, proxied by Next" — the web
  // Container App rewrites /api/* to the internal API. new URL(...) in
  // request() needs an absolute base, so resolve against the browser origin.
  // All API calls run client-side; server-side has no origin, so fall back to
  // the dev API (keeps new URL() valid — there are no SSR callers today).
  if (configured?.startsWith("/")) {
    return typeof window !== "undefined"
      ? `${window.location.origin}${configured}`
      : "http://127.0.0.1:8000";
  }
  // Absolute override (e.g. dev pointing straight at the API), else dev default.
  return configured && configured.length > 0
    ? configured
    : "http://127.0.0.1:8000";
}

/** True when the dev-token textbox should be shown in the UI. */
export function isDevTokenAvailable(): boolean {
  if (typeof window === "undefined") return false;
  return !isEntraClientConfigured();
}

export async function resolveAccessToken(
  ctx: ApiClientContext,
): Promise<string> {
  if (!isEntraClientConfigured()) {
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
  const account = ctx.instance.getActiveAccount() ?? ctx.accounts[0] ?? null;
  if (!account) {
    throw new Error("No active MSAL account; sign in first");
  }
  try {
    const result = await ctx.instance.acquireTokenSilent({
      account,
      scopes: API_ACCESS_SCOPES,
      forceRefresh: false,
    });
    return result.accessToken;
  } catch (e) {
    if (e instanceof InteractionRequiredAuthError) {
      // Redirect, not popup: SPA-PKCE token refresh is triggered from a
      // background fetch (no user gesture), so popups are blocked by modern
      // browsers. Redirect navigates away — user re-auths and lands at
      // /auth/callback → /. Form state is lost (documented risk).
      await ctx.instance.acquireTokenRedirect({
        account,
        scopes: API_ACCESS_SCOPES,
      });
      // acquireTokenRedirect navigates the page; this throw is unreachable
      // in practice but keeps the function's return type honest.
      throw new Error("Redirect to Entra in progress");
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

/**
 * Format an unknown thrown value into a user-facing error string. For
 * ApiError responses carrying an HTTPException-style `{detail: {code,
 * message}}` body, prefixes the message with the code (e.g.,
 * `role_not_found: No role with id ...`) so admin surfaces can show the
 * underlying error class to operators. Falls back to the ApiError message
 * (already extracted from intake/contacts shapes by extractErrorMessage)
 * or the bare Error.message otherwise.
 */
export function formatApiError(e: unknown): string {
  if (e instanceof ApiError) {
    const detail =
      typeof e.body === "object" && e.body !== null && "detail" in e.body
        ? (e.body as { detail: unknown }).detail
        : null;
    if (typeof detail === "object" && detail !== null && "code" in detail) {
      const code = (detail as { code: string }).code;
      const message = (detail as { message?: string }).message ?? e.message;
      return `${code}: ${message}`;
    }
    return e.message;
  }
  return e instanceof Error ? e.message : "Request failed";
}

export interface ApiRequestInit {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
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

type AssignableOrgsResponseBody =
  paths["/v1/matches/{match_id}/assignable-orgs"]["get"]["responses"]["200"]["content"]["application/json"];

export async function getAssignableOrgs(
  ctx: ApiClientContext,
  matchId: string,
): Promise<AssignableOrgsResponseBody> {
  return _assertPresent(
    await apiFetch<AssignableOrgsResponseBody>(
      ctx,
      `/v1/matches/${matchId}/assignable-orgs`,
    ),
    `/v1/matches/${matchId}/assignable-orgs`,
  );
}

type ContactEmailRequestBody =
  paths["/v1/contacts/{contact_id}/emails"]["post"]["requestBody"]["content"]["application/json"];
type ContactEmailResponseBody =
  paths["/v1/contacts/{contact_id}/emails"]["post"]["responses"]["202"]["content"]["application/json"];

// Targets 202 with a body; _assertPresent is correct because apiFetch only short-circuits on 204.
export async function sendContactEmail(
  ctx: ApiClientContext,
  contactId: string,
  body: ContactEmailRequestBody,
): Promise<ContactEmailResponseBody> {
  return _assertPresent(
    await apiFetch<ContactEmailResponseBody>(
      ctx,
      `/v1/contacts/${contactId}/emails`,
      { method: "POST", body },
    ),
    `/v1/contacts/${contactId}/emails`,
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

// ── Drip campaigns (#55) ─────────────────────────────────────────────────

type CampaignListResponseBody =
  paths["/v1/drips/campaigns"]["get"]["responses"]["200"]["content"]["application/json"];
type CampaignReadBody =
  paths["/v1/drips/campaigns/{campaign_id}"]["get"]["responses"]["200"]["content"]["application/json"];
type CampaignCreateBody =
  paths["/v1/drips/campaigns"]["post"]["requestBody"]["content"]["application/json"];

export async function listCampaigns(
  ctx: ApiClientContext,
): Promise<CampaignListResponseBody> {
  return _assertPresent(
    await apiFetch<CampaignListResponseBody>(ctx, "/v1/drips/campaigns"),
    "/v1/drips/campaigns",
  );
}

export async function createCampaign(
  ctx: ApiClientContext,
  body: CampaignCreateBody,
): Promise<CampaignReadBody> {
  return _assertPresent(
    await apiFetch<CampaignReadBody>(ctx, "/v1/drips/campaigns", {
      method: "POST",
      body,
    }),
    "/v1/drips/campaigns",
  );
}

export async function activateCampaign(
  ctx: ApiClientContext,
  campaignId: string,
): Promise<CampaignReadBody> {
  return _assertPresent(
    await apiFetch<CampaignReadBody>(
      ctx,
      `/v1/drips/campaigns/${campaignId}/activate`,
      { method: "POST" },
    ),
    `/v1/drips/campaigns/${campaignId}/activate`,
  );
}

export async function pauseCampaign(
  ctx: ApiClientContext,
  campaignId: string,
): Promise<CampaignReadBody> {
  return _assertPresent(
    await apiFetch<CampaignReadBody>(
      ctx,
      `/v1/drips/campaigns/${campaignId}/pause`,
      { method: "POST" },
    ),
    `/v1/drips/campaigns/${campaignId}/pause`,
  );
}

type CampaignPatchBody =
  paths["/v1/drips/campaigns/{campaign_id}"]["patch"]["requestBody"]["content"]["application/json"];
type CampaignStepCreateBody =
  paths["/v1/drips/campaigns/{campaign_id}/steps"]["post"]["requestBody"]["content"]["application/json"];
type CampaignStepReadBody =
  paths["/v1/drips/campaigns/{campaign_id}/steps"]["post"]["responses"]["201"]["content"]["application/json"];
type TemplateListResponseBody =
  paths["/v1/drips/templates"]["get"]["responses"]["200"]["content"]["application/json"];

export async function getCampaign(
  ctx: ApiClientContext,
  campaignId: string,
): Promise<CampaignReadBody> {
  return _assertPresent(
    await apiFetch<CampaignReadBody>(ctx, `/v1/drips/campaigns/${campaignId}`),
    `/v1/drips/campaigns/${campaignId}`,
  );
}

export async function patchCampaign(
  ctx: ApiClientContext,
  campaignId: string,
  body: CampaignPatchBody,
): Promise<CampaignReadBody> {
  return _assertPresent(
    await apiFetch<CampaignReadBody>(ctx, `/v1/drips/campaigns/${campaignId}`, {
      method: "PATCH",
      body,
    }),
    `/v1/drips/campaigns/${campaignId}`,
  );
}

export async function addCampaignStep(
  ctx: ApiClientContext,
  campaignId: string,
  body: CampaignStepCreateBody,
): Promise<CampaignStepReadBody> {
  return _assertPresent(
    await apiFetch<CampaignStepReadBody>(
      ctx,
      `/v1/drips/campaigns/${campaignId}/steps`,
      { method: "POST", body },
    ),
    `/v1/drips/campaigns/${campaignId}/steps`,
  );
}

export async function deleteCampaignStep(
  ctx: ApiClientContext,
  campaignId: string,
  position: number,
): Promise<void> {
  await apiFetch<void>(
    ctx,
    `/v1/drips/campaigns/${campaignId}/steps/${position}`,
    { method: "DELETE" },
  );
}

type StepPreviewResponseBody =
  paths["/v1/drips/campaigns/{campaign_id}/steps/{position}/preview"]["post"]["responses"]["200"]["content"]["application/json"];

export async function previewCampaignStep(
  ctx: ApiClientContext,
  campaignId: string,
  position: number,
): Promise<StepPreviewResponseBody> {
  return _assertPresent(
    await apiFetch<StepPreviewResponseBody>(
      ctx,
      `/v1/drips/campaigns/${campaignId}/steps/${position}/preview`,
      { method: "POST" },
    ),
    `/v1/drips/campaigns/${campaignId}/steps/${position}/preview`,
  );
}

export async function archiveCampaign(
  ctx: ApiClientContext,
  campaignId: string,
): Promise<void> {
  await apiFetch<void>(ctx, `/v1/drips/campaigns/${campaignId}`, {
    method: "DELETE",
  });
}

export async function listDripTemplates(
  ctx: ApiClientContext,
): Promise<TemplateListResponseBody> {
  return _assertPresent(
    await apiFetch<TemplateListResponseBody>(ctx, "/v1/drips/templates"),
    "/v1/drips/templates",
  );
}

// ── Admin: Graph user lookup (#97) ───────────────────────────────────────

type UserSearchResponseBody =
  paths["/v1/admin/users/search"]["get"]["responses"]["200"]["content"]["application/json"];

export async function searchAdminUsers(
  ctx: ApiClientContext,
  q: string,
  opts: { signal?: AbortSignal } = {},
): Promise<UserSearchResponseBody> {
  return _assertPresent(
    await apiFetch<UserSearchResponseBody>(ctx, "/v1/admin/users/search", {
      query: { q },
      signal: opts.signal,
    }),
    "/v1/admin/users/search",
  );
}

type ContactEnrollmentsResponseBody =
  paths["/v1/contacts/{contact_id}/enrollments"]["get"]["responses"]["200"]["content"]["application/json"];
type ManualEnrollResponseBody =
  paths["/v1/drips/campaigns/{campaign_id}/enroll"]["post"]["responses"]["200"]["content"]["application/json"];

export async function getContactEnrollments(
  ctx: ApiClientContext,
  contactId: string,
): Promise<ContactEnrollmentsResponseBody> {
  return _assertPresent(
    await apiFetch<ContactEnrollmentsResponseBody>(
      ctx,
      `/v1/contacts/${contactId}/enrollments`,
    ),
    `/v1/contacts/${contactId}/enrollments`,
  );
}

export async function enrollInCampaign(
  ctx: ApiClientContext,
  campaignId: string,
  contactId: string,
): Promise<ManualEnrollResponseBody> {
  return _assertPresent(
    await apiFetch<ManualEnrollResponseBody>(
      ctx,
      `/v1/drips/campaigns/${campaignId}/enroll`,
      { method: "POST", body: { contact_id: contactId } },
    ),
    `/v1/drips/campaigns/${campaignId}/enroll`,
  );
}

// ── Suppression (#55) ────────────────────────────────────────────────────

type SuppressionListResponseBody =
  paths["/v1/suppression-list"]["get"]["responses"]["200"]["content"]["application/json"];
type SuppressionCreateBody =
  paths["/v1/suppression-list"]["post"]["requestBody"]["content"]["application/json"];
type SuppressionReadBody =
  paths["/v1/suppression-list"]["post"]["responses"]["200"]["content"]["application/json"];

export async function listSuppression(
  ctx: ApiClientContext,
  opts: { limit?: number; offset?: number } = {},
): Promise<SuppressionListResponseBody> {
  return _assertPresent(
    await apiFetch<SuppressionListResponseBody>(ctx, "/v1/suppression-list", {
      query: { limit: opts.limit, offset: opts.offset },
    }),
    "/v1/suppression-list",
  );
}

export async function addSuppression(
  ctx: ApiClientContext,
  body: SuppressionCreateBody,
): Promise<SuppressionReadBody> {
  return _assertPresent(
    await apiFetch<SuppressionReadBody>(ctx, "/v1/suppression-list", {
      method: "POST",
      body,
    }),
    "/v1/suppression-list",
  );
}

export async function removeSuppression(
  ctx: ApiClientContext,
  emailHash: string,
): Promise<void> {
  await apiFetch<void>(ctx, `/v1/suppression-list/${emailHash}`, {
    method: "DELETE",
  });
}
