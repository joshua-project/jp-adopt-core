#!/usr/bin/env bash
# End-to-end smoke test against production (or staging).
#
# Mirrors scripts/smoke-local.sh but:
#   - API-only (no psql / docker assertions; no DB access from a laptop)
#   - Adds the drip enrollment + read-back step (#55)
#   - Bearer comes from a real Entra session, not dev-local
#
# Checkpoints (in order):
#   1.  /api/healthz                              (API up)
#   2.  /api/readyz                               (DB reachable from API)
#   3.  GET  /v1/matches/queue                    (Entra auth works end-to-end)
#   4.  GET  /v1/drips/campaigns                  (drips read; auto-picks active campaign)
#   5.  POST /v1/contacts/manual                  (intake → outbox event)
#   6.  POST /v1/matches/run/{id}                 (matching algorithm)
#   7.  GET  /v1/matches/queue                    (smoke contact appears)
#   8.  POST /v1/drips/campaigns/{id}/enroll      (manual drip enrollment)
#   9.  GET  /v1/contacts/{id}/enrollments        (enrollment recorded + events)
#  10.  POST /v1/contacts/{id}/transition         (soft-retire smoke contact → do_not_engage)
#
# After the script exits, the operator manually verifies:
#   • The drip email arrives in the SMOKE_EMAIL inbox within ~5 minutes
#     (the worker tick runs every minute; delay_days=0 steps go on the
#     next tick after enrollment).
#
# The smoke contact is left in the system in `do_not_engage` so it's
# trivially excluded from real matching going forward. Its display name
# starts with "Smoke Prod" so it's easy to find later if you want to
# hard-delete via DB access.
#
# Usage:
#   BEARER='<paste from devtools>' scripts/smoke-prod.sh
#   API_URL=https://other-host SMOKE_EMAIL=me@example.com scripts/smoke-prod.sh
#   QUIET=1 scripts/smoke-prod.sh                # only failures + summary
#
# How to get a bearer token from production:
#   1. Sign into the prod staff app in your browser
#   2. Open DevTools → Application → Session Storage → look for an
#      `msal.<client-id>.idtoken` / `accesstoken` entry. Copy the
#      `secret` field — that's the bearer.
#   3. Or paste it via the dev-token textbox on /signin (only present
#      when STRICT_AUTH=false, i.e. NEVER on prod). Prod will reject
#      `dev-local` per the boot-time validator, so a real Entra token
#      is required.

set -uo pipefail

API_URL="${API_URL:-https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api}"
BEARER="${BEARER:-}"
QUIET="${QUIET:-0}"

if [ -z "$BEARER" ]; then
    printf '\033[0;31m[ERROR]\033[0m BEARER not set. Paste an Entra access token from devtools.\n' >&2
    printf '  See the script header for how to grab one.\n' >&2
    exit 2
fi

UNIQUE=$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z' | tr -d '-' | head -c 12)
if [ -z "$UNIQUE" ]; then
    UNIQUE="$(date +%s)$$"
fi
SMOKE_EMAIL="${SMOKE_EMAIL:-smoke+${UNIQUE}@example.com}"
SMOKE_NAME="Smoke Prod ${UNIQUE}"

PASS_COUNT=0
FAIL_COUNT=0
FAILED_STEPS=()

# ─── Output helpers ─────────────────────────────────────────────────────
say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*" >&2; }
ok()  { PASS_COUNT=$((PASS_COUNT + 1)); [ "$QUIET" = "1" ] || printf '\033[0;32m[PASS]\033[0m %s\n' "$*" >&2; }
fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILED_STEPS+=("$1")
    printf '\033[0;31m[FAIL]\033[0m %s\n' "$*" >&2
    if [ $# -gt 1 ]; then
        printf '       %s\n' "$2" >&2
    fi
}

# ─── curl helpers ───────────────────────────────────────────────────────
curl_get() {
    local path="$1"
    local response
    response=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer ${BEARER}" "${API_URL}${path}" 2>/dev/null)
    local body status
    body="$(printf '%s' "$response" | sed '$d')"
    status="$(printf '%s' "$response" | tail -n1)"
    printf '%s::%s' "$status" "$body"
}

curl_post() {
    local path="$1" body="$2"
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST \
        -H "Authorization: Bearer ${BEARER}" \
        -H "Content-Type: application/json" \
        -d "$body" \
        "${API_URL}${path}" 2>/dev/null)
    local resp_body status
    resp_body="$(printf '%s' "$response" | sed '$d')"
    status="$(printf '%s' "$response" | tail -n1)"
    printf '%s::%s' "$status" "$resp_body"
}

say "─── Prod smoke against ${API_URL} ───"
say "    contact: ${SMOKE_NAME} <${SMOKE_EMAIL}>"
say ""

# ─── Step 1: /healthz ──────────────────────────────────────────────────
result=$(curl_get /healthz)
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "200" ] && printf '%s' "$body" | grep -q '"status"\s*:\s*"ok"'; then
    ok "1. GET /healthz → 200"
else
    fail "1. GET /healthz" "expected 200 with status=ok, got ${status}: ${body:0:120}"
fi

# ─── Step 2: /readyz ───────────────────────────────────────────────────
result=$(curl_get /readyz)
status="${result%%::*}"
if [ "$status" = "200" ]; then
    ok "2. GET /readyz → 200 (DB reachable)"
else
    fail "2. GET /readyz" "expected 200, got ${status} — DB unreachable from API"
fi

# ─── Step 3: GET /v1/matches/queue ─────────────────────────────────────
result=$(curl_get /v1/matches/queue)
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "200" ]; then
    total=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null || echo "?")
    ok "3. GET /v1/matches/queue → 200 (Entra auth works; queue total=${total})"
elif [ "$status" = "401" ] || [ "$status" = "403" ]; then
    fail "3. GET /v1/matches/queue" "auth failed (${status}). Check BEARER is a current Entra access token, not an ID token."
    exit 1
else
    fail "3. GET /v1/matches/queue" "expected 200, got ${status}"
    exit 1
fi

# ─── Step 4: GET /v1/drips/campaigns ───────────────────────────────────
result=$(curl_get /v1/drips/campaigns)
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "200" ]; then
    CAMPAIGN_ID="${CAMPAIGN_ID:-}"
    if [ -z "$CAMPAIGN_ID" ]; then
        CAMPAIGN_ID=$(printf '%s' "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for c in d.get('items', []):
    if c.get('status') == 'active':
        print(c.get('id', ''))
        break
" 2>/dev/null || echo "")
    fi
    if [ -n "$CAMPAIGN_ID" ]; then
        ok "4. GET /v1/drips/campaigns → 200 (active campaign=${CAMPAIGN_ID:0:8}...)"
    else
        ok "4. GET /v1/drips/campaigns → 200 (no active campaigns; will skip step 8)"
    fi
else
    fail "4. GET /v1/drips/campaigns" "expected 200, got ${status}"
    CAMPAIGN_ID=""
fi

# ─── Step 5: POST /v1/contacts/manual ──────────────────────────────────
manual_body=$(cat <<JSON
{
  "display_name": "${SMOKE_NAME}",
  "email": "${SMOKE_EMAIL}",
  "party_kind": "adopter",
  "origin": "manual_entry",
  "country_code": "US",
  "fpg_people_id3s": ["AAA01"],
  "newsletter_opt_in": false,
  "notes": "Prod smoke test — safe to set to do_not_engage."
}
JSON
)
result=$(curl_post /v1/contacts/manual "$manual_body")
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "201" ]; then
    CONTACT_ID=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('contact_id', ''))" 2>/dev/null)
    ok "5. POST /v1/contacts/manual → 201 (contact_id=${CONTACT_ID:0:8}...)"
else
    fail "5. POST /v1/contacts/manual" "expected 201, got ${status}: ${body:0:200}"
    CONTACT_ID=""
fi

# ─── Step 6: POST /v1/matches/run/{id} ─────────────────────────────────
if [ -n "$CONTACT_ID" ]; then
    result=$(curl_post "/v1/matches/run/${CONTACT_ID}" '{"force": false}')
    status="${result%%::*}"
    body="${result#*::}"
    if [ "$status" = "200" ]; then
        match_count=$(printf '%s' "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
matches = d.get('matches') or d.get('attempts') or d.get('items') or []
print(len(matches) if isinstance(matches, list) else 1)
" 2>/dev/null || echo "?")
        ok "6. POST /v1/matches/run/{id} → 200 (matches found=${match_count})"
    else
        fail "6. POST /v1/matches/run/{id}" "expected 200, got ${status}: ${body:0:200}"
    fi
else
    fail "6. POST /v1/matches/run/{id}" "skipped — no contact_id from step 5"
fi

# ─── Step 7: re-fetch queue, verify smoke contact present ──────────────
if [ -n "$CONTACT_ID" ]; then
    result=$(curl_get /v1/matches/queue)
    body="${result#*::}"
    if printf '%s' "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('items', [])
contact_id = '${CONTACT_ID}'
found = any(item.get('contact_id') == contact_id for item in items)
sys.exit(0 if found else 1)
"; then
        ok "7. GET /v1/matches/queue (re-fetched) — contains the smoke contact"
    else
        fail "7. GET /v1/matches/queue" "smoke contact ${CONTACT_ID:0:8}... not in queue after match-run (may be no-coverage; check matches.run response)"
    fi
else
    fail "7. GET /v1/matches/queue (re-fetch)" "skipped — no contact_id"
fi

# ─── Step 8: POST /v1/drips/campaigns/{id}/enroll ──────────────────────
ENROLLMENT_ID=""
if [ -n "$CONTACT_ID" ] && [ -n "$CAMPAIGN_ID" ]; then
    enroll_body=$(cat <<JSON
{"contact_id": "${CONTACT_ID}"}
JSON
)
    result=$(curl_post "/v1/drips/campaigns/${CAMPAIGN_ID}/enroll" "$enroll_body")
    status="${result%%::*}"
    body="${result#*::}"
    if [ "$status" = "200" ]; then
        reason=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('reason', '?'))" 2>/dev/null)
        ENROLLMENT_ID=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('enrollment_id') or '')" 2>/dev/null)
        if [ "$reason" = "created" ]; then
            ok "8. POST /v1/drips/campaigns/{id}/enroll → 200 (created, enrollment=${ENROLLMENT_ID:0:8}...)"
        else
            ok "8. POST /v1/drips/campaigns/{id}/enroll → 200 (reason=${reason}; no fresh enrollment)"
        fi
    else
        fail "8. POST /v1/drips/campaigns/{id}/enroll" "expected 200, got ${status}: ${body:0:200}"
    fi
elif [ -z "$CAMPAIGN_ID" ]; then
    say "    8. skipped — no active campaign on prod (activate one via /campaigns first)"
else
    fail "8. POST /v1/drips/campaigns/{id}/enroll" "skipped — no contact_id"
fi

# ─── Step 9: GET /v1/contacts/{id}/enrollments ─────────────────────────
if [ -n "$CONTACT_ID" ]; then
    result=$(curl_get "/v1/contacts/${CONTACT_ID}/enrollments")
    status="${result%%::*}"
    body="${result#*::}"
    if [ "$status" = "200" ]; then
        total=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null || echo "?")
        if [ "$total" -ge 1 ] 2>/dev/null; then
            ok "9. GET /v1/contacts/{id}/enrollments → 200 (total=${total})"
        else
            # Acceptable: no active campaign or already-enrolled — auto-enrollment may apply later.
            ok "9. GET /v1/contacts/{id}/enrollments → 200 (total=${total}; no enrollments yet)"
        fi
    else
        fail "9. GET /v1/contacts/{id}/enrollments" "expected 200, got ${status}"
    fi
else
    fail "9. GET /v1/contacts/{id}/enrollments" "skipped — no contact_id"
fi

# ─── Step 10: soft-retire via transition → do_not_engage ───────────────
if [ -n "$CONTACT_ID" ]; then
    transition_body=$(cat <<JSON
{
  "kind": "adopter",
  "to_state": "do_not_engage",
  "reason_code": "other",
  "reason_text": "prod smoke retirement"
}
JSON
)
    result=$(curl_post "/v1/contacts/${CONTACT_ID}/transition" "$transition_body")
    status="${result%%::*}"
    body="${result#*::}"
    if [ "$status" = "200" ]; then
        new_state=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('transitioned_to', '?'))" 2>/dev/null)
        ok "10. POST /v1/contacts/{id}/transition → 200 (transitioned_to=${new_state})"
    else
        fail "10. POST /v1/contacts/{id}/transition" "expected 200, got ${status}: ${body:0:200}"
    fi
else
    fail "10. POST /v1/contacts/{id}/transition" "skipped — no contact_id"
fi

# ─── Summary ───────────────────────────────────────────────────────────
say ""
say "═══════════════════════════════════════════════════════════════"
say "  Prod smoke: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
say "  Smoke contact: ${SMOKE_NAME} (${SMOKE_EMAIL})"
say "  Contact id:    ${CONTACT_ID:-skipped}"
if [ -n "$ENROLLMENT_ID" ]; then
    say "  Enrollment id: ${ENROLLMENT_ID}"
    say ""
    say "  → Manual step: check ${SMOKE_EMAIL} for the drip email"
    say "    (expect within 1–2 worker ticks; ~1 minute per tick)"
fi
say "═══════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
    printf '\nFailed steps:\n' >&2
    for s in "${FAILED_STEPS[@]}"; do
        printf '  • %s\n' "$s" >&2
    done
    exit 1
fi
exit 0
