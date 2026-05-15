# Magic-link side-car

The magic-link side-car is the email-only authentication path. A user
enters an email address, the API mints a single-use URL, the worker
(eventually) sends an email, and the user clicks through to exchange the
URL for a 7-day bearer JWT.

## Endpoints

* `POST /v1/auth/magic-link/request` — body `{"email": "..."}`. Always
  returns HTTP 202 with `{"ok": true, "message": "If we have your email,
  we sent a link."}`. Identical response shape whether or not the email
  exists in any of our identity tables (anti-enumeration). HTTP 429 when
  rate-limited.
* `POST /v1/auth/magic-link/claim` — body `{"token": "..."}`. Returns
  HTTP 200 with `{access_token, token_type, expires_in}` on success.
  Error responses:
  | Status | Code                          | Meaning                                                       |
  | ------ | ----------------------------- | ------------------------------------------------------------- |
  | 400    | `invalid_token`               | Token did not match any stored hash                           |
  | 410    | `expired`                     | Token row exists but `expires_at` has passed                  |
  | 410    | `already_claimed`             | Token row exists but `claimed_at` is non-null                 |
  | 403    | `account_resolution_conflict` | Email is linked to a B2C identity (see multi-idp-b2c.md)      |

> **Important:** `/claim` is `POST` only, not `GET`. This prevents email
> client URL prefetchers from consuming the single-use link before the
> user clicks. Frontends should render a button on the click target page
> rather than auto-submitting the form.

## Constants

| Setting                          | Value      | Rationale                                                                 |
| -------------------------------- | ---------- | ------------------------------------------------------------------------- |
| `MAGIC_LINK_TTL_SECONDS`         | 900 (15m)  | Magic links are bearer secrets in email; short TTL bounds exposure        |
| `MAGIC_LINK_RATE_LIMIT_PER_HOUR` | 6 per email| Prevents enumeration / mailbox-spam denial-of-service                     |
| `MAGIC_LINK_JWT_TTL_SECONDS`     | 7 days     | Long enough for typical "sign in once, work for a week" flows             |

## Storage

* `magic_link_token` — one row per request. Stores `token_hash` (SHA-256 of
  raw token + `MAGIC_LINK_SIGNING_KEY`), `expires_at`, audit columns.
  Raw token is never persisted; only the hash is.
* `magic_link_rate_limit` — one row per request, used to enforce the
  6/hour throttle. Rows are not garbage-collected in U3 (cheap; cron-prune
  is a future improvement).

## Anti-enumeration response

Always 202 + identical message body on `/request`, regardless of whether
the email is in our system. This prevents a probing attacker from learning
the membership of `identity_link` or `contacts.email_normalized`.

Malformed emails (missing `@` or `.`) still return 202 — we do not surface
validation failures because they leak whether our normalizer accepts the
input.

## ACS-not-configured dev fallback

When `ACS_CONNECTION_STRING` is unset (the default in dev), the worker
task `send_magic_link_email` logs the click URL to stdout instead of
sending an email:

```
magic_link.email.dev_fallback recipient=user@example.test url=http://localhost:3000/auth/claim?token=...
```

The developer copies the URL into the browser to exercise the claim flow
without standing up real email infrastructure. The same fallback applies
when the SDK is installed but the connection string is empty.

## Signing-key rotation

See `docs/runbooks/multi-idp-b2c.md#magic-link-signing-key-rotation`. Both
the token-hash pepper and the HS256 JWT signature share the same key.
Rotation invalidates all in-flight magic-links and all outstanding JWTs.

## Common operator tasks

### Manually invalidate a magic-link token

```sql
UPDATE magic_link_token
SET expires_at = NOW() - INTERVAL '1 minute'
WHERE id = '<token-id>';
```

The token's next `/claim` request returns 410 `expired`.

### Reset a user's rate-limit window

```sql
DELETE FROM magic_link_rate_limit
WHERE email_normalized = '<lowercased-email>';
```

The next `/request` will succeed. Use sparingly — re-running this for an
abusive emailer defeats the throttle.
