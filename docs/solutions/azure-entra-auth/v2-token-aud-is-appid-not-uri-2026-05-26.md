---
title: Entra v2.0 access tokens carry appId in `aud`, not the identifier URI
date: 2026-05-26
category: azure-entra-auth
module: api/auth_entra
problem_type: footgun
component: authentication
severity: high
applies_when:
  - Configuring an API to validate JWTs issued by Entra (Azure AD) via the v2.0 endpoint
  - Setting `ENTRA_DIRECT_AUDIENCE` or any `audience=` argument passed to `jwt.decode(...)` for an Entra-issued token
  - Creating a new API app registration with `api { requested_access_token_version = 2 }`
  - Diagnosing `401 {"detail":"Invalid or expired access token"}` after a successful MSAL sign-in
related_components:
  - jp-infrastructure/stacks/azure/entra/jp-adopt-core-sso
  - .github/workflows/deploy.yml (deploy-api job)
tags:
  - entra
  - azure-ad
  - jwt
  - msal
  - oauth2
  - aud
  - v2-tokens
  - authentication
---

# Entra v2.0 access tokens carry the resource appId in `aud`, not the identifier URI

## Context

During the Phase 2 launch of jp-adopt-core (Entra direct sign-in for JP
staff), all authenticated API requests returned
`401 {"detail":"Invalid or expired access token"}` immediately after a
successful MSAL sign-in. The API logs only showed uvicorn's access log,
not the JWT-validation reason (the `auth.py` failure path logs at
`DEBUG`, which is below the default INFO threshold).

Pasting the JWT from the browser's Network panel and decoding it
unverified revealed the culprit:

```json
{
  "aud": "75edd3b3-90c8-4982-a619-d038ebaa50ea",  // ← resource appId GUID
  "iss": "https://login.microsoftonline.com/<tid>/v2.0",
  "ver": "2.0",
  "scp": "api.access",
  "tid": "761e2c5f-34bd-4872-b86c-3a9f3b29d63a",
  "oid": "<user OID>",
  "preferred_username": "...@joshuaproject.net"
}
```

The API was validating against `aud = "api://jp-adopt-core"` (the
identifier URI), because that's the natural assumption when you read the
API app reg's `identifier_uris = ["api://jp-adopt-core"]` line in
Terraform. PyJWT raised `InvalidAudienceError`, the deps layer rewrote
it as `401 "Invalid or expired access token"`, and every protected
route 401'd.

## Guidance

**For Entra tokens issued via the v2.0 endpoint, the `aud` claim is the
resource app reg's `appId` (a GUID), NOT its identifier URI.** The URI
form only appears in v1 tokens.

The token version is controlled by **the resource's**
`requestedAccessTokenVersion`, not by the client:

| Resource setting                       | Resulting `aud` for a token requested via v2 endpoint |
| -------------------------------------- | ----------------------------------------------------- |
| `requestedAccessTokenVersion = 1`      | `api://your-app` (identifier URI)                     |
| `requestedAccessTokenVersion = 2`      | `<resource-appId-GUID>`                               |
| (unset / null)                         | `<resource-appId-GUID>` (v1 default behavior varies)  |

If you adopt v2 (recommended — required for some scenarios like
short-lived tokens, optional claims, etc.), the API must expect the
GUID form.

### Right — production setup

`apps/api/src/jp_adopt_api/config.py`:

```python
# In production, ENTRA_DIRECT_AUDIENCE MUST be set to the API app reg
# appId GUID via env var. The default below is the identifier URI for
# dev/test convenience.
entra_direct_audience: str = "api://jp-adopt-core"
```

`.github/workflows/deploy.yml` (deploy-api job):

```yaml
az containerapp update \
  --name "$ACA_API_APP_NAME" \
  --resource-group "$ACA_RESOURCE_GROUP" \
  --image "$ACR_LOGIN_SERVER/jp-adopt-api:${{ github.sha }}" \
  --set-env-vars \
    "DEPLOY_SHA=${{ github.sha }}" \
    "ENTRA_DIRECT_AUDIENCE=75edd3b3-90c8-4982-a619-d038ebaa50ea"
```

The GUID matches `azuread_application.jp_adopt_core_api.client_id` in
`jp-infrastructure/stacks/azure/entra/jp-adopt-core-sso/main.tf`. If you
rotate the API app reg (rare), bump the value in `deploy.yml` in the
same PR as the Terraform change so the API and the issuer stay aligned.

### Wrong — what I shipped in the plan first

The plan body asserted `API expected aud (incoming JWT) =
"api://jp-adopt-core"`. That would be correct for a v1 token, or for an
API that did not set `requestedAccessTokenVersion = 2`. The plan was
internally inconsistent: U2 set `requested_access_token_version = 2` on
the API app reg, but the routing/auth contract assumed v1 audience
semantics. No reviewer caught this because the contract table reads
naturally — the identifier URI sits next to the scope, and "the API
validates `aud = identifier_uri`" sounds right.

## How to diagnose this class of failure

When an Entra-direct API returns
`401 {"detail":"Invalid or expired access token"}`:

1. **Grab the actual token.** Browser DevTools → Network → click the
   failing request → Request Headers → copy after `Bearer `.
2. **Decode it unverified.** Paste into jwt.io, or run
   `python -c "import jwt,json; print(json.dumps(jwt.decode(t, options={'verify_signature': False}), indent=2))"`.
3. **Compare each claim against the API's expectations**:
   - `aud` against `settings.entra_direct_audience` (resource appId for
     v2, identifier URI for v1)
   - `iss` against `_expected_issuer(tid)` (`https://login.microsoftonline.com/<tid>/v2.0`)
   - `tid` against a `partner_tenants` row
   - `ver`: `"2.0"` for v2 tokens, absent for v1
   - `exp`: clock skew? usually not the issue on fresh sign-in
4. **Bump the API log level temporarily** if you need more than the
   token to confirm — `auth.py` logs the precise failure at `DEBUG`
   (`logger.debug("JWT validation failed: %s", e)`).

The fix in this incident was a one-line env-var change
(`az containerapp update --set-env-vars ENTRA_DIRECT_AUDIENCE=<GUID>`),
codified into `deploy.yml` so future deploys don't drift.

## References

- `docs/runbooks/multi-idp-b2c.md` § "v2-token `aud` quirk (Entra direct)"
- Microsoft docs: [Access tokens — v2.0 and v1.0 tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#v20-and-v10-tokens)
- Original incident: 2026-05-26, post-launch sign-in test (this commit)
- Adjacent plan: `docs/superpowers/plans/2026-05-26-entra-direct-staff-auth.md` (Errata block at top notes this correction)
