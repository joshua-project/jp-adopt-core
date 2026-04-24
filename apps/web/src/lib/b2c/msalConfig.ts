import type { Configuration } from "@azure/msal-browser";

/**
 * Public env for Azure AD B2C MSAL (SPA + PKCE). Must align with FastAPI
 * `AZURE_AD_B2C_AUDIENCE` / `AZURE_AD_B2C_ISSUER` in apps/api.
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
  return {
    auth: {
      clientId,
      authority,
      knownAuthorities,
      redirectUri: "/",
      postLogoutRedirectUri: "/",
      navigateToLoginRequestUrl: true,
    },
    cache: {
      cacheLocation: "sessionStorage",
      storeAuthStateInCookie: false,
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
