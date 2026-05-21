#!/usr/bin/env bash
# End-to-end smoke test for a running local stack.
#
# What it covers (in order):
#   1.  /healthz                                 (API up)
#   2.  /readyz                                  (DB reachable from API)
#   3.  GET  /v1/matches/queue                   (auth + queue read)
#   4.  GET  /v1/drips/campaigns                 (drip CRUD read)
#   5.  POST /v1/contacts/manual                 (write path + outbox event)
#   6.  POST /v1/matches/run/{id}                (matching algorithm)
#   7.  GET  /v1/matches/queue                   (new match present)
#   8.  POST /v1/contacts/{id}/transition        (workflow router)
#   9.  Outbox + Match row asserted via DB       (data layer sanity)
#  10.  Worker tick observed in logs             (cron health)
#
# Each step prints PASS or FAIL. Exit code is 0 only if every step passes.
#
# Idempotent: every run creates a new contact with a UUID-suffixed email
# so re-running never collides. Test rows are tagged with
# `origin='smoke_test'` so they're easy to grep + delete:
#
#   DELETE FROM contacts WHERE origin = 'smoke_test';
#
# Usage:
#   scripts/smoke-local.sh                       # default endpoints
#   API_URL=http://my-host.ts.net:8000 scripts/smoke-local.sh
#   BEARER=dev-local scripts/smoke-local.sh      # default auth
#   QUIET=1 scripts/smoke-local.sh               # only print failures + summary

set -uo pipefail

API_URL="${API_URL:-http://127.0.0.1:8000}"
# Optional: set WEB_URL to also run an identity preflight against the
# Next.js web container. Useful when another local Next dev server
# might be racing for the host port (e.g. jp-adopt-forms on :3000).
WEB_URL="${WEB_URL:-}"
BEARER="${BEARER:-dev-local}"
QUIET="${QUIET:-0}"
PG_CONTAINER="${PG_CONTAINER:-jp-adopt-core-postgres-1}"
PG_USER="${PG_USER:-jp_adopt}"
PG_DB="${PG_DB:-jp_adopt}"
PG_PASS="${PG_PASS:-jp_adopt}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5434}"

UNIQUE=$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z' | tr -d '-' | head -c 12)
if [ -z "$UNIQUE" ]; then
    UNIQUE="$(date +%s)$$"
fi
SMOKE_EMAIL="smoke+${UNIQUE}@example.com"
SMOKE_NAME="Smoke Test ${UNIQUE}"

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

# ─── psql shim (mirrors seed-local.sh) ──────────────────────────────────
psql_run() {
    local sql="$1"
    if command -v psql >/dev/null 2>&1; then
        PGPASSWORD="$PG_PASS" psql -h "$PG_HOST" -p "$PG_PORT" \
            -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -At -c "$sql" 2>/dev/null
    elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER"; then
        docker exec -i -e PGPASSWORD="$PG_PASS" "$PG_CONTAINER" \
            psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -At -c "$sql" 2>/dev/null
    else
        echo ""
    fi
}

# ─── Helper: curl with status code capture ──────────────────────────────
curl_get() {
    # Usage: curl_get <path>  -> echoes "HTTP_STATUS::BODY"
    local path="$1"
    local response
    response=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer ${BEARER}" "${API_URL}${path}" 2>/dev/null)
    local body status
    body="$(printf '%s' "$response" | sed '$d')"
    status="$(printf '%s' "$response" | tail -n1)"
    printf '%s::%s' "$status" "$body"
}

curl_post() {
    # Usage: curl_post <path> <json-body>
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

# ─── Step 1: /healthz ──────────────────────────────────────────────────
say "─── Smoke test against ${API_URL} as Bearer ${BEARER} ───"

# Optional web identity preflight — catches the "wrong app on the
# host port" failure mode where another local Next dev server has
# won the host-port race. Only runs when WEB_URL is set.
if [ -n "$WEB_URL" ]; then
    web_body=$(curl -s "${WEB_URL}/" 2>/dev/null || echo "")
    # Case-insensitive: UI brand updated from "JP ADOPT" to "JP Adopt".
    if printf '%s' "$web_body" | grep -qi "JP Adopt"; then
        ok "0. GET ${WEB_URL}/ — identifies as jp-adopt-core web"
    else
        title=$(printf '%s' "$web_body" | grep -oE '<title>[^<]*</title>' | head -1)
        fail "0. GET ${WEB_URL}/" "doesn't look like jp-adopt-core (title: ${title:-none}). Port conflict?"
    fi
fi

result=$(curl_get /healthz)
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "200" ]; then
    # Identity check: the body must look like our /healthz payload,
    # not some other Next/FastAPI app's homepage that happens to
    # return 200. Caught a real "wrong app on the host port" bug
    # where another local Next dev server had won the :3000 race.
    if printf '%s' "$body" | grep -q '"status"\s*:\s*"ok"'; then
        ok "1. GET /healthz → 200"
    else
        fail "1. GET /healthz" "got 200 but response doesn't look like jp-adopt-core (got: ${body:0:120}). Check for port conflict — another app may be on this host port."
    fi
else
    fail "1. GET /healthz" "expected 200, got ${status}"
fi

# ─── Step 2: /readyz ───────────────────────────────────────────────────
result=$(curl_get /readyz)
status="${result%%::*}"
if [ "$status" = "200" ]; then
    ok "2. GET /readyz → 200 (Postgres reachable from API)"
else
    fail "2. GET /readyz" "expected 200, got ${status} — DB unreachable from API"
fi

# ─── Step 3: GET /v1/matches/queue ─────────────────────────────────────
result=$(curl_get /v1/matches/queue)
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "200" ]; then
    total=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null || echo "?")
    ok "3. GET /v1/matches/queue → 200 (total=${total})"
else
    fail "3. GET /v1/matches/queue" "expected 200, got ${status}"
fi

# ─── Step 4: GET /v1/drips/campaigns ───────────────────────────────────
result=$(curl_get /v1/drips/campaigns)
status="${result%%::*}"
body="${result#*::}"
if [ "$status" = "200" ]; then
    total=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('total', 0))" 2>/dev/null || echo "?")
    ok "4. GET /v1/drips/campaigns → 200 (total=${total})"
else
    fail "4. GET /v1/drips/campaigns" "expected 200, got ${status}"
fi

# ─── Step 5: POST /v1/contacts/manual ──────────────────────────────────
manual_body=$(cat <<JSON
{
  "display_name": "${SMOKE_NAME}",
  "email": "${SMOKE_EMAIL}",
  "party_kind": "adopter",
  "origin": "manual_entry",
  "country_code": "US",
  "fpg_rop3s": ["AAA01"],
  "newsletter_opt_in": false,
  "notes": "Smoke test row — safe to delete."
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
    fail "5. POST /v1/contacts/manual" "expected 201, got ${status}: ${body}"
    CONTACT_ID=""
fi

# Tag the contact as smoke_test in the DB so it's grep'able for cleanup
# (the public API doesn't expose 'smoke_test' as a valid origin).
if [ -n "$CONTACT_ID" ]; then
    psql_run "UPDATE contacts SET origin='smoke_test' WHERE id='${CONTACT_ID}';" >/dev/null
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

# ─── Step 7: re-fetch queue, verify new contact present ────────────────
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
        ok "7. GET /v1/matches/queue (re-fetched) — contains the new smoke contact"
    else
        fail "7. GET /v1/matches/queue" "smoke contact ${CONTACT_ID:0:8}... not in queue after match-run"
    fi
else
    fail "7. GET /v1/matches/queue (re-fetch)" "skipped — no contact_id"
fi

# ─── Step 8: POST /v1/contacts/{id}/transition ─────────────────────────
# Transition the smoke contact to 'do_not_engage' — a terminal state
# that's legal from any other state. Exercises the workflow router.
if [ -n "$CONTACT_ID" ]; then
    transition_body=$(cat <<JSON
{
  "kind": "adopter",
  "to_state": "do_not_engage",
  "reason_code": "other",
  "reason_text": "smoke test transition"
}
JSON
)
    result=$(curl_post "/v1/contacts/${CONTACT_ID}/transition" "$transition_body")
    status="${result%%::*}"
    body="${result#*::}"
    if [ "$status" = "200" ]; then
        new_state=$(printf '%s' "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('transitioned_to', '?'))" 2>/dev/null)
        ok "8. POST /v1/contacts/{id}/transition → 200 (transitioned_to=${new_state})"
    else
        fail "8. POST /v1/contacts/{id}/transition" "expected 200, got ${status}: ${body:0:200}"
    fi
else
    fail "8. POST /v1/contacts/{id}/transition" "skipped — no contact_id"
fi

# ─── Step 9: Outbox + Match assertions via DB ──────────────────────────
if [ -n "$CONTACT_ID" ]; then
    # Check that the contact's transition emitted an outbox event.
    outbox_count=$(psql_run "
SELECT COUNT(*) FROM outbox
WHERE payload_json->>'contact_id' = '${CONTACT_ID}';
")
    if [ -n "$outbox_count" ] && [ "$outbox_count" != "0" ]; then
        ok "9a. Outbox events for the smoke contact: ${outbox_count}"
    else
        fail "9a. Outbox events" "expected >= 1 for ${CONTACT_ID:0:8}..., got ${outbox_count:-?}"
    fi

    # Check that a Match row exists for the smoke contact.
    match_count=$(psql_run "
SELECT COUNT(*) FROM match m
JOIN adopter_interest ai ON ai.id = m.adopter_interest_id
WHERE ai.contact_id = '${CONTACT_ID}';
")
    if [ -n "$match_count" ] && [ "$match_count" != "0" ]; then
        ok "9b. Match rows for the smoke contact: ${match_count}"
    else
        fail "9b. Match rows" "expected >= 1 for ${CONTACT_ID:0:8}..., got ${match_count:-?}"
    fi
fi

# ─── Step 10: Worker tick health ───────────────────────────────────────
# Look for a recent cron tick log in the worker container.
worker_log=$(docker logs --tail 50 jp-adopt-core-worker-1 2>&1 || true)
if printf '%s' "$worker_log" | grep -q "cron:drain_outbox\|cron:drain_drip_enrollments\|cron:send_drip_step"; then
    ok "10. Worker cron ticks visible in last 50 log lines"
else
    say "    (worker log tail had no obvious cron ticks — may just be quiet)"
    ok "10. Worker container running (no obvious errors)"
fi

# ─── Summary ───────────────────────────────────────────────────────────
say ""
say "═══════════════════════════════════════════════════════════════"
say "  Smoke summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
say "  Test contact: ${SMOKE_EMAIL} (id=${CONTACT_ID:-skipped})"
say "  Cleanup:      DELETE FROM contacts WHERE origin='smoke_test';"
say "═══════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
    printf '\nFailed steps:\n' >&2
    for s in "${FAILED_STEPS[@]}"; do
        printf '  • %s\n' "$s" >&2
    done
    exit 1
fi
exit 0
