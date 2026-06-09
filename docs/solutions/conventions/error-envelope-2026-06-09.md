---
title: Unified `/v1/` error envelope convention
date: 2026-06-09
module: api
tags: [error-envelope, openapi, contracts, http]
problem_type: convention
status: convention
---

## Summary

`/v1/` API responses use one of three error envelope shapes today
depending on which router emitted the error. New endpoints **must**
adopt **shape B** going forward. Existing routers keep their current
shape — rewriting them would break consuming clients (web + forms +
n8n) for no real win. The web's `formatApiError` helper already
handles all three shapes, so end-to-end UX is consistent.

Closes the convention half of #33. The "rewrite every router to one
shape" half is intentionally deferred.

## The three shapes

### Shape A — Intake envelope

```json
{
  "apiVersion": "v1",
  "ok": false,
  "error": {
    "code": "validation_failed",
    "message": "Payload did not validate.",
    "fields": { "email": ["Invalid format."] },
    "requestId": "0d2b…"
  }
}
```

**Routers:** `intake.py` (`/v1/intake/*`).

**Why this exists:** mirrors the upstream `jp-adopt-forms` envelope
verbatim so forms can dual-write to its local DB and to this API
without code-path divergence.

**Don't replicate elsewhere.** The dual-write motivation is unique
to intake.

### Shape B — HTTPException `detail` as `{code, message, …}` (the convention)

```json
{
  "detail": {
    "code": "org_has_open_matches",
    "message": "Cannot deactivate — 3 open match(es) reference this org.",
    "open_match_count": 3
  }
}
```

**Routers:** `auth_magic_link.py`, `matches.py`, `contacts.py`
workflow transition, `drips.py`, `admin.py`, `suppression.py`.

**This is the convention for new routes.** When raising
`HTTPException`, always pass a `dict` as `detail` with at minimum
`code` and `message`. Optional contextual fields (counts, ids) go
alongside.

```python
raise HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail={
        "code": "org_has_open_matches",
        "message": "Cannot deactivate — open matches reference this org.",
        "open_match_count": int(open_match_count),
    },
)
```

### Shape C — HTTPException `detail` as bare string

```json
{ "detail": "Contact not found" }
```

**Routers:** parts of `contacts.py` (notably the 404 paths).

**Don't introduce new instances.** When touching existing string-
detail raises in `contacts.py`, upgrade them to shape B opportunistically.

## OpenAPI responses

Every new endpoint **must** declare its non-2xx response shapes via
the `responses=` kwarg on the route decorator. Without this, the
generated TS client has no typed representation for the body, and
the web layer falls back to `unknown` casts.

```python
@router.post(
    "/v1/admin/foo",
    response_model=FooRead,
    responses={
        404: {"description": "Foo not found"},
        409: {"description": "Foo is in use"},
    },
)
```

422 is auto-declared by FastAPI for Pydantic validation; you don't
need to add it.

## Web-side handling

`apps/web/src/lib/api-client.ts` carries `extractErrorMessage` +
`formatApiError`. Both already handle all three shapes:

```ts
formatApiError(e)
// shape A: returns the `error.message`
// shape B: returns `"{code}: {message}"` (code-prefixed)
// shape C: returns the bare string
// non-Error: returns "Request failed"
```

`formatApiError`'s code-prefix on shape B is the load-bearing
ergonomic. Operators see `org_has_open_matches: Cannot deactivate
— open matches reference this org.` instead of just the message.
This convention assumes shape B is the default; routers staying
on shape C will read fine but lose the code prefix.

## What we explicitly did not do

- **Rewrite shape C to shape B in `contacts.py`.** Every web call
  site treating contacts errors as plain strings would have to be
  audited; the upside (code prefix) doesn't justify the regression
  surface. Refactor opportunistically when touching the surrounding
  code.
- **Backport `responses=` declarations onto every legacy route.**
  The work is real but unconditionally compatible — TS client
  becomes more precise. Land it when each router is next opened
  for surgery.

## When this becomes wrong

If a fourth shape appears (or the upstream intake envelope diverges
from `jp-adopt-forms`), revisit this doc and `formatApiError`. The
extractor is structured so adding a shape is a 5-line change, not
a refactor.

## References

- `apps/web/src/lib/api-client.ts:108-132` — extractErrorMessage
- `apps/web/src/lib/api-client.ts:143-157` — formatApiError
- `docs/solutions/conventions/` — other binding conventions in
  this codebase
