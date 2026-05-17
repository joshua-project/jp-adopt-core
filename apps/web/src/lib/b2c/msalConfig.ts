import type { Configuration } from "@azure/msal-browser";
import { LogLevel } from "@azure/msal-browser";

/**
 * Public env for Azure AD B2C MSAL (SPA + PKCE). Must align with FastAPI
 * `AZURE_AD_B2C_AUDIENCE` / `AZURE_AD_B2C_ISSUER` in apps/api.
 *
 * Migrated from MSAL v3 → v5 (Day 1 of the amy-return build). v5 changes:
 *   - `navigateToLoginRequestUrl` moved out of the auth config (now a
 *     per-request option on `handleRedirectPromise`).
 *   - `LoggerOptions.correlationId` is a documented field; we set a
 *     per-session UUID so multi-IdP debugging can stitch the browser side
 *     to the API access log.
 */
export function isB2cClientConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID?.trim() &&
      process.env.NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME?.trim() &&
      process.env.NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID?.trim() &&
      process.env.NEXT_PUBLIC_AZURE_AD_B2C_POLICY?.trim(),
  );
}

export function getApiScopeList(): string[] {
  const raw = process.env.NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES ?? "";
  return raw
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Returns a stable per-tab correlation ID for MSAL logger. We persist it in
 * sessionStorage so multi-step flows (sign-in → token-acquire → API call)
 * carry the same id; falls back to a fresh random id on SSR / private mode.
 */
function getSessionCorrelationId(): string {
  if (typeof window === "undefined" || !window.sessionStorage) {
    return `srv-${Math.random().toString(36).slice(2, 10)}`;
  }
  const key = "jp_adopt_msal_correlation_id";
  let id = window.sessionStorage.getItem(key);
  if (!id) {
    id = (typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `tab-${Math.random().toString(36).slice(2, 10)}-${Date.now()}`);
    try {
      window.sessionStorage.setItem(key, id);
    } catch {
      /* sessionStorage may be disabled in private mode; the ephemeral id is fine. */
    }
  }
  return id;
}

export function buildMsalConfiguration(): Configuration | null {
  if (!isB2cClientConfigured()) {
    return null;
  }
  const clientId = process.env.NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID!.trim();
  const tenantName = process.env.NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME!.trim();
  const tenantId = process.env.NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID!.trim();
  const policy = process.env.NEXT_PUBLIC_AZURE_AD_B2C_POLICY!.trim();
  const knownRaw = process.env.NEXT_PUBLIC_AZURE_AD_B2C_KNOWN_AUTHORITIES;
  const knownAuthorities = knownRaw
    ? knownRaw
        .split(/[\s,]+/)
        .map((h) => h.trim())
        .filter(Boolean)
    : [`${tenantName}.b2clogin.com`];

  const authority = `https://${tenantName}.b2clogin.com/${tenantId}/${policy}/`;
  const correlationId = getSessionCorrelationId();
  return {
    auth: {
      clientId,
      authority,
      knownAuthorities,
      redirectUri: "/",
      postLogoutRedirectUri: "/",
    },
    cache: {
      // v5 dropped storeAuthStateInCookie — it was always best-effort for legacy
      // IE-on-iframe scenarios and is no longer needed.
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
 */
export function isDevTokenUiEnabled(): boolean {
  if (process.env.NODE_ENV === "production") {
    return false;
  }
  return process.env.NEXT_PUBLIC_DEV_TOKEN_UI !== "false";
}
