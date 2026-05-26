# Multi-IdP authentication (B2C + Entra direct + magic-link)

> **Update 2026-05-26:** B2C was closed by Microsoft to new customers
> (2025-05-01). The launch staff auth is **Entra direct** (Phase 2 /
> jp-adopt-core#60). The B2C decoder code stays in place but is no longer
> exercised. The web-side UI lives at `/signin` and `/auth/callback`
> (MSAL v5 PKCE, single-tenant). Staff Entra OIDs are seeded in
> `user_roles` via Alembic — see migration `0014_seed_staff_user_roles`.
> Adding a new staff member is an `az ad user show` lookup + a new
> Alembic revision inserting one row; deferred admin UI is in Part F of
> the Entra direct plan (`docs/superpowers/plans/2026-05-26-entra-direct-staff-auth.md`).
> `partner_tenants` is seeded with the JP Entra tenant
> (`761e2c5f-34bd-4872-b86c-3a9f3b29d63a`) by migration `0013`.

This runbook covers the operator-facing tasks for the JP ADOPT multi-IdP
authentication stack introduced in U3.

## Architecture summary

There are three independent token decoders in `apps/api/src/jp_adopt_api/`:

| Decoder         | File              | Issuer pattern                                                | Signing alg | Backed by                                 |
| --------------- | ----------------- | ------------------------------------------------------------- | ----------- | ----------------------------------------- |
| Azure AD B2C    | `auth.py`         | `https://<tenant>.b2clogin.com/<tid>/v2.0/`                   | RS256       | B2C user flow + JWKS                      |
| Entra direct    | `auth_entra.py`   | `https://login.microsoftonline.com/<tid>/v2.0`                | RS256       | Multi-tenant Entra + `partner_tenants` DB |
| Magic-link      | `auth_magic.py`   | `https://api.joshuaproject.net/magic-link/v1` (configurable)  | HS256       | API-issued (no upstream IdP)              |

`authenticate_bearer_async` reads the unverified `iss` claim and dispatches
to the correct decoder. Dev-local bearer (`dev-local`) remains supported
when `STRICT_AUTH=false` and `APP_ENV` is not production.

## B2C portal configuration

For each consumer IdP (Google, Facebook, MSA) you want to support via the
B2C tenant, follow Microsoft's guides:

* Google: https://learn.microsoft.com/azure/active-directory-b2c/identity-provider-google
* Facebook: https://learn.microsoft.com/azure/active-directory-b2c/identity-provider-facebook
* MSA (consumer Microsoft accounts): https://learn.microsoft.com/azure/active-directory-b2c/identity-provider-microsoft-account

For *single-tenant* Entra partners (one organization, not multi-tenant), add
that tenant as an OpenID Connect IdP in the same B2C user flow.

> **Collapse-vs-fork test (Day 1 task):** sign in to the same B2C user flow
> with two different IdPs that share an email address. Observe whether B2C
> collapses them onto a single `sub` or forks them. The plan reserves a TODO
> to run this in the dev tenant on Day 1; results inform the
> `account_resolution_conflict` runbook section below.
>
> Status: **TODO — run test in dev tenant on Day 1.**

## Adding a partner tenant (Entra direct)

When a partner organization wants to authenticate users from *their* Entra
tenant directly (without going through B2C), an operator must provision the
tenant in `partner_tenants`:

```sql
INSERT INTO partner_tenants (id, microsoft_tenant_id, partner_id, partner_name)
VALUES (
  gen_random_uuid(),
  '<microsoft-tenant-guid>',
  '<our-internal-partner-id>',  -- nullable; populated when ETL-linked
  'Example Partner Org'
);
```

The next API request bearing a token from that tid will be accepted (assuming
the token's `aud` matches `ENTRA_DIRECT_AUDIENCE` and the signature
verifies). Until the row exists, `decode_entra_direct_token` raises
`TenantNotProvisionedError` and the deps layer returns HTTP 403 with
`{"code": "tenant_not_provisioned"}`.

To revoke access:

```sql
DELETE FROM partner_tenants WHERE microsoft_tenant_id = '<tid>';
```

Cached JWKS clients (`auth_entra.get_entra_jwks_client`) will continue
working for the lifetime of the API process, but newly-issued tokens for
the revoked tid will be rejected on the `partner_tenants` lookup.

## Magic-link signing-key rotation

The HS256 signing key for magic-link JWTs lives in `MAGIC_LINK_SIGNING_KEY`.
Rotation procedure:

1. Generate a new key (>= 32 bytes): `openssl rand -base64 48`.
2. Deploy the new value to API + worker simultaneously (worker uses it for
   the email body; API uses it for both minting and verifying).
3. Existing JWTs minted with the old key will fail verification — users will
   need to sign in again. Magic-link tokens *in flight* (issued but not yet
   claimed) become un-claimable since the token-hash pepper changes.

If you need a zero-downtime rotation (rare), the side-car would need a
key-list rather than a single key — out of scope for week 1.

## `account_resolution_conflict` resolution

Triggered when a user signs in via magic-link for an email that is already
linked to a B2C identity (`identity_link.b2c_subject_id IS NOT NULL`). The
side-car refuses to silently bridge — the user gets HTTP 403 with
`{"code": "account_resolution_conflict"}`.

Resolution workflow:

1. **Verify identity.** Have the user prove they own the email AND have
   access to the B2C account (e.g. answer a security question, or confirm
   recent activity). Email ownership alone is insufficient because the B2C
   linkage may predate this user's control of the inbox.
2. **Decide which IdP wins.** Default: keep the B2C identity (more
   securely-anchored). Magic-link is a recovery path, not a primary IdP.
3. **Apply the resolution:**
   * If the user wants to *keep* the B2C identity, no DB change is needed.
     Tell the user to sign in via B2C instead.
   * If the user genuinely cannot recover the B2C account and wants to
     reset to magic-link, an operator can clear the conflicting B2C link
     after manual verification:
     ```sql
     UPDATE identity_link
     SET b2c_subject_id = NULL, idp_name = 'magic_link'
     WHERE email_normalized = '<lowercased-email>';
     ```
   * Then the user retries the magic-link claim.

Audit: every resolution should be logged in the operator's incident
tracker. The DB does not currently keep a per-resolution audit row;
`transition_audit` is for contact-state changes only.

## Strict-auth in production

`STRICT_AUTH=true` is enforced when `APP_ENV=production`. This rejects
the `dev-local` bearer token outright. There is no override.

`MAGIC_LINK_SIGNING_KEY` is validated to be >= 32 bytes when `APP_ENV=production`
(see `Settings.magic_link_key_must_be_strong_in_production`).
