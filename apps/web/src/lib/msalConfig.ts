import type { Configuration } from "@azure/msal-browser";
import { LogLevel } from "@azure/msal-browser";

/**
 * Public env for Azure Entra ID (single-tenant) MSAL v5 SPA + PKCE. Replaces
 * the deprecated B2C config (B2C was closed to new customers 2025-05-01). Aligns
 * with FastAPI's `entra_direct_audience` (default `api://jp-adopt-core`) and
 * `_ENTRA_ISSUER_RE` in `apps/api/src/jp_adopt_api/auth.py`.
 *
 * v5 idioms preserved from the dead B2C config:
 *   - `instance.initialize()` is awaited inside `MsalClientProvider` before any
 *     other MSAL call.
 *   - `LoggerOptions.correlationId` carries a per-session UUID for cross-system
 *     debugging.
 *   - `cacheLocation: "sessionStorage"` — same XSS posture as before.
 *
 * The `redirectUri` and `postLogoutRedirectUri` are set to full callback paths
 * (`/auth/callback`, `/signin`) — NOT the bare origin. Entra rejects redirect
 * URIs that don't exactly match a registered SPA redirect URI (`AADSTS50011`);
 * the Entra app registration in jp-infrastructure
 * (`stacks/azure/entra/jp-adopt-core-sso`) registers both
 * `https://<aca-fqdn>/auth/callback` and
 * `https://adoption.joshuaproject.net/auth/callback`.
 */

export const SIGNIN_SCOPES = ["openid", "profile", "email", "User.Read"];

// Scope claimed when acquiring an access token for the jp-adopt-core API.
// Resolves to `aud = api://jp-adopt-core` on the issued JWT, which is the
// audience the FastAPI app validates (settings.entra_direct_audience).
export const API_ACCESS_SCOPES = ["api://jp-adopt-core/api.access"];

export function isEntraClientConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_AZURE_AD_TENANT_ID?.trim() &&
      process.env.NEXT_PUBLIC_AZURE_AD_CLIENT_ID?.trim(),
  );
}

/**
 * Returns a stable per-tab correlation ID for the MSAL logger. We persist it in
 * sessionStorage so multi-step flows (sign-in → token-acquire → API call) carry
 * the same id; falls back to a fresh random id on SSR / private-mode browsers.
 */
function getSessionCorrelationId(): string {
  if (typeof window === "undefined" || !window.sessionStorage) {
    return `srv-${Math.random().toString(36).slice(2, 10)}`;
  }
  const key = "jp_adopt_msal_correlation_id";
  let id = window.sessionStorage.getItem(key);
  if (!id) {
    id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `tab-${Math.random().toString(36).slice(2, 10)}-${Date.now()}`;
    try {
      window.sessionStorage.setItem(key, id);
    } catch {
      /* sessionStorage may be disabled in private mode; the ephemeral id is fine. */
    }
  }
  return id;
}

export function buildEntraConfiguration(): Configuration | null {
  if (!isEntraClientConfigured()) {
    return null;
  }
  const clientId = process.env.NEXT_PUBLIC_AZURE_AD_CLIENT_ID!.trim();
  const tenantId = process.env.NEXT_PUBLIC_AZURE_AD_TENANT_ID!.trim();
  const authority = `https://login.microsoftonline.com/${tenantId}`;
  const correlationId = getSessionCorrelationId();
  const origin = typeof window !== "undefined" ? window.location.origin : "";

  return {
    auth: {
      clientId,
      authority,
      knownAuthorities: ["login.microsoftonline.com"],
      // Must exactly match one of the SPA app reg's registered redirect URIs.
      // See jp-infrastructure stacks/azure/entra/jp-adopt-core-sso/main.tf —
      // both the ACA FQDN and adoption.joshuaproject.net are registered.
      redirectUri: origin ? `${origin}/auth/callback` : undefined,
      postLogoutRedirectUri: origin ? `${origin}/signin` : undefined,
    },
    cache: {
      cacheLocation: "sessionStorage",
    },
    system: {
      loggerOptions: {
        correlationId,
        piiLoggingEnabled: false,
        logLevel: LogLevel.Warning,
        loggerCallback: (level, message, containsPii) => {
          if (containsPii) return;
          if (level === LogLevel.Error) {
            // eslint-disable-next-line no-console
            console.error(`[msal:${correlationId}] ${message}`);
          } else if (level === LogLevel.Warning) {
            // eslint-disable-next-line no-console
            console.warn(`[msal:${correlationId}] ${message}`);
          }
        },
      },
    },
  };
}

/**
 * If false, hide the dev bearer token textbox (production or explicit opt-out).
 * Gated on Next.js's build-time `NODE_ENV` constant so the entire dev-token UI
 * is dead-code-eliminated in production bundles.
 */
export function isDevTokenUiEnabled(): boolean {
  if (process.env.NODE_ENV === "production") {
    return false;
  }
  return process.env.NEXT_PUBLIC_DEV_TOKEN_UI !== "false";
}
