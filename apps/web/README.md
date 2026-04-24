# `apps/web` — staff UI (Next.js)

## Azure AD B2C (interactive sign-in)

This app uses **`@azure/msal-browser`** and **`@azure/msal-react`** with the **authorization code flow + PKCE** (public client, no client secret in the browser). After sign-in, it acquires an **access token** for the FastAPI API's exposed scope and sends it as `Authorization: Bearer` to `GET /v1/contacts`.

The API validates JWTs with `AZURE_AD_B2C_AUDIENCE`, issuer, and JWKS (`apps/api` / `jp_adopt_api.auth`). The **audience** on the token must match `AZURE_AD_B2C_AUDIENCE`; the **issuer** must match `AZURE_AD_B2C_ISSUER` or the default built from tenant + policy.

### Required environment variables (public)

| Variable | Purpose |
| -------- | ------- |
| `NEXT_PUBLIC_API_URL` | FastAPI base URL (e.g. `http://127.0.0.1:8000`) |
| `NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID` | **SPA** app registration (Application client ID) |
| `NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME` | B2C tenant name (first segment of `*.b2clogin.com`, e.g. `contosob2c`) |
| `NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID` | Directory (tenant) GUID |
| `NEXT_PUBLIC_AZURE_AD_B2C_POLICY` | User flow / policy name (e.g. `B2C_1_signupsignin1`) |
| `NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES` | Space- or comma-separated **API** scope(s) (must match a scope exposed on the **API** registration and granted to the SPA) |

Optional:

| Variable | Purpose |
| -------- | ------- |
| `NEXT_PUBLIC_AZURE_AD_B2C_KNOWN_AUTHORITIES` | Comma- or space-separated hostnames; default `{TENANT_NAME}.b2clogin.com` |
| `NEXT_PUBLIC_DEV_TOKEN_UI` | Set to `false` to hide the **manual bearer** field in non-production; production always hides it |

Copy `apps/web/.env.local.example` to `apps/web/.env.local` for local Next.js.

### Azure Portal — redirect URI (local)

1. In the **SPA** app registration, add a **Single-page application** redirect URI:
   - `http://localhost:3000/`
2. If you use another dev port, register that origin too (e.g. `http://localhost:3001/`).
3. Add staging/production web origins when deployed.

**Logout** uses the same origin as a post-logout redirect.

### Scopes and API audience

- Register the **API** in Azure AD B2C, expose a scope (for example `access_as_user`).
- Under the **SPA** app, grant that scope (API permissions).
- Set `NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES` to the full scope value (often `https://<tenant-id-or-domain>/<api-app-id>/access_as_user` or `api://<api-app-id>/access_as_user` depending on your **Application ID URI**).
- Set `AZURE_AD_B2C_AUDIENCE` in the **API** process to the value expected in the token's `aud` claim.

If any of the **required** public B2C variables are **unset**, the contacts page uses the **dev-only** path (paste token or `dev-local` with `STRICT_AUTH=false` on the API) when `NODE_ENV=development` and `NEXT_PUBLIC_DEV_TOKEN_UI` is not `false`.

## Why not NextAuth here?

**MSAL in the browser** matches B2C **user flows** and custom **API scopes** on a public SPA, with `acquireTokenSilent` / popup when interaction is required. A **BFF** (server session + proxy) can be added later to keep access tokens off the browser; this implementation keeps tokens in **session storage** (MSAL cache) per Microsoft's SPA pattern.
