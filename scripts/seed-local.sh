#!/usr/bin/env bash
# Populate the local stack with enough data for end-to-end clicks.
#
# Idempotent: re-running won't duplicate rows.
#
# Works against either dev path:
#   - `pnpm run dev:stack` (API on :8000, Postgres on host :5434)
#   - `docker compose --profile full up` (same host ports)
#
# What it sets up:
#   1. dev-local gets a Contact row with an email + staff_admin role
#      (so the daily digest's _load_staff_recipients query finds them).
#   2. Two test facilitator B2C subjects with Contact rows + emails +
#      facilitator_org_membership rows against the seeded U5 demo orgs.
#   3. The facilitator-welcome drip campaign created + activated, with
#      one step pointing at the placeholder MJML template.
#   4. Three test adopter contacts via POST /v1/contacts/manual so the
#      match queue isn't empty.
#
# Usage:
#   scripts/seed-local.sh              # default API + DB host endpoints
#   API_URL=http://localhost:8001 scripts/seed-local.sh
#   PG_HOST=localhost PG_PORT=5434 scripts/seed-local.sh
#
# Requires either local `psql` OR a running `postgres` compose service.

set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8000}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5434}"
PG_USER="${PG_USER:-jp_adopt}"
PG_PASS="${PG_PASS:-jp_adopt}"
PG_DB="${PG_DB:-jp_adopt}"
BEARER="${BEARER:-dev-local}"

log() { printf '\033[1;34m[seed]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[seed]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[seed]\033[0m %s\n' "$*" >&2; exit 1; }

# ─── Compose CLI detection ────────────────────────────────────────────────
# Some setups have `docker compose` (v2 plugin), others have the legacy
# `docker-compose` standalone binary. Detect what's available.
if docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE=""
fi

# ─── psql shim ────────────────────────────────────────────────────────────
# Prefer host `psql` so the script works without docker; fall back to
# `docker exec` against the compose Postgres container if the host has no
# psql installed. We use raw `docker exec` instead of `compose exec`
# because the container is more reliably reachable that way regardless of
# which compose CLI is available.
PG_CONTAINER="${PG_CONTAINER:-jp-adopt-core-postgres-1}"

psql_run() {
    local sql="$1"
    if command -v psql >/dev/null 2>&1; then
        PGPASSWORD="$PG_PASS" psql \
            -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
            -v ON_ERROR_STOP=1 -At -c "$sql"
    elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER"; then
        docker exec -i \
            -e PGPASSWORD="$PG_PASS" \
            "$PG_CONTAINER" \
            psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -At -c "$sql"
    else
        fail "Neither host psql nor the postgres container ($PG_CONTAINER) is available. Start the stack first, or set PG_CONTAINER if your container name differs."
    fi
}

# ─── Preflight ────────────────────────────────────────────────────────────
log "Probing API at $API_URL..."
if ! curl -fsS "${API_URL}/healthz" >/dev/null; then
    fail "API not responding at $API_URL. Start it first (pnpm run dev:stack OR docker compose --profile full up)."
fi
log "API healthy."

log "Probing Postgres at $PG_HOST:$PG_PORT..."
if ! psql_run "SELECT 1" >/dev/null; then
    fail "Postgres not reachable at $PG_HOST:$PG_PORT"
fi
log "Postgres reachable."

# ─── 1. dev-local staff_admin row ────────────────────────────────────────
log "Granting dev-local sub the staff_admin role + Contact row..."

DEV_LOCAL_SUB="dev-local"
DEV_LOCAL_EMAIL="dev@local.invalid"
STAFF_ADMIN_ROLE_ID="00000003-0000-0000-0000-000000000001"

# Contact row first (so the digest's b2c_subject_id JOIN finds an email).
# email_normalized has a unique partial index — use ON CONFLICT DO NOTHING.
psql_run "
INSERT INTO contacts (
    id, party_kind, display_name, adopter_status,
    email_normalized, b2c_subject_id, source_system
)
VALUES (
    gen_random_uuid(), 'adopter', 'Dev Local Staff', 'new',
    '${DEV_LOCAL_EMAIL}', '${DEV_LOCAL_SUB}', 'local'
)
ON CONFLICT (email_normalized) WHERE email_normalized IS NOT NULL
DO UPDATE SET b2c_subject_id = EXCLUDED.b2c_subject_id;
" >/dev/null

# Role grant — idempotent via composite PK.
psql_run "
INSERT INTO user_roles (user_subject_id, role_id)
VALUES ('${DEV_LOCAL_SUB}', '${STAFF_ADMIN_ROLE_ID}')
ON CONFLICT DO NOTHING;
" >/dev/null

log "  dev-local: staff_admin granted, Contact row present."

# ─── 2. Test facilitator users + memberships ────────────────────────────
log "Seeding two test facilitator users..."

EXAMPLE_MISSION_ORG_ID="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
FRONTIER_ALLIANCE_ORG_ID="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3"
FACILITATOR_ROLE_ID="00000003-0000-0000-0000-000000000004"

seed_facilitator() {
    local sub="$1" name="$2" email="$3" org_id="$4"

    psql_run "
INSERT INTO contacts (
    id, party_kind, display_name, facilitator_status,
    email_normalized, b2c_subject_id, source_system
)
VALUES (
    gen_random_uuid(), 'facilitator', '${name}', 'ready',
    '${email}', '${sub}', 'local'
)
ON CONFLICT (email_normalized) WHERE email_normalized IS NOT NULL
DO UPDATE SET b2c_subject_id = EXCLUDED.b2c_subject_id;
" >/dev/null

    psql_run "
INSERT INTO user_roles (user_subject_id, role_id)
VALUES ('${sub}', '${FACILITATOR_ROLE_ID}')
ON CONFLICT DO NOTHING;
" >/dev/null

    # Use the admin endpoint for the membership — exercises the
    # public surface end-to-end.
    #
    # Drop -f so a 409 (membership already exists) doesn't trip
    # `set -euo pipefail`. The HTTP status is reported either way;
    # we only care about 201 (created) or 409 (already there).
    local code
    code=$(curl -sS -X POST "${API_URL}/v1/admin/facilitator-memberships" \
        -H "Authorization: Bearer ${BEARER}" \
        -H "Content-Type: application/json" \
        -d "{\"user_subject_id\": \"${sub}\", \"facilitator_org_id\": \"${org_id}\", \"role_in_org\": \"member\"}" \
        -o /dev/null \
        -w "%{http_code}" 2>/dev/null || echo "000")
    case "$code" in
        201) echo "  ${name} → ${email} (org ${org_id:0:8}): created";;
        409) echo "  ${name} → ${email} (org ${org_id:0:8}): already exists (ok)";;
        *)   warn "  ${name} → ${email} (org ${org_id:0:8}): HTTP $code (unexpected)";;
    esac
}

# Pydantic's email-validator rejects RFC 2606 reserved TLDs (.test,
# .example, .invalid, .localhost). Use real-shape addresses on
# example.com (RFC 2606 reserved for documentation but accepted by
# email-validator).
seed_facilitator "fac-example-1" "Example Mission — Alice" "alice+example@example.com" "$EXAMPLE_MISSION_ORG_ID"
seed_facilitator "fac-frontier-1" "Frontier Alliance — Bob" "bob+frontier@example.com" "$FRONTIER_ALLIANCE_ORG_ID"

# ─── 3. Drip campaign ────────────────────────────────────────────────────
log "Creating + activating facilitator-welcome drip campaign..."

# Look up the campaign by name first — if it exists, skip create.
EXISTING_CAMPAIGN=$(psql_run "
SELECT id FROM campaign WHERE name = 'Facilitator welcome' LIMIT 1;
" || echo "")

if [[ -n "$EXISTING_CAMPAIGN" ]]; then
    log "  Campaign already exists: $EXISTING_CAMPAIGN — skipping create."
    CAMPAIGN_ID="$EXISTING_CAMPAIGN"
else
    CAMPAIGN_RESPONSE=$(curl -fsS -X POST "${API_URL}/v1/drips/campaigns" \
        -H "Authorization: Bearer ${BEARER}" \
        -H "Content-Type: application/json" \
        -d '{
            "name": "Facilitator welcome",
            "description": "First touch when a facilitator org accepts a match.",
            "trigger_type": "event",
            "trigger_event_type": "jp.adopt.v1.match.accepted_by_facilitator",
            "auto_enroll_existing": false,
            "precedence": 10
        }')
    CAMPAIGN_ID=$(echo "$CAMPAIGN_RESPONSE" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p' | head -1)
    log "  Created campaign: $CAMPAIGN_ID"

    # Add step 0 — pointed at the placeholder MJML template.
    curl -fsS -X POST "${API_URL}/v1/drips/campaigns/${CAMPAIGN_ID}/steps" \
        -H "Authorization: Bearer ${BEARER}" \
        -H "Content-Type: application/json" \
        -d '{
            "position": 0,
            "delay_days": 0,
            "mjml_template_name": "facilitator-welcome.step-0.mjml",
            "subject": "Welcome to Joshua Project Adoption",
            "send_at_hour": 0,
            "send_at_minute": 0
        }' >/dev/null
    log "  Added step 0 (send_at_hour=0 so dev sends fire immediately, not 9am)."

    # Activate
    curl -fsS -X POST "${API_URL}/v1/drips/campaigns/${CAMPAIGN_ID}/activate" \
        -H "Authorization: Bearer ${BEARER}" >/dev/null
    log "  Activated."
fi

# ─── 4. Test adopter contacts ────────────────────────────────────────────
log "Creating 3 test adopter contacts..."

seed_adopter() {
    local email="$1" name="$2" rop3s_json="$3"

    # Skip if a contact with this email already exists.
    EXISTING=$(psql_run "SELECT id FROM contacts WHERE email_normalized = '${email}' LIMIT 1;" || echo "")
    if [[ -n "$EXISTING" ]]; then
        log "  ${name} (${email}) — already exists, skipping."
        return 0
    fi

    curl -fsS -X POST "${API_URL}/v1/contacts/manual" \
        -H "Authorization: Bearer ${BEARER}" \
        -H "Content-Type: application/json" \
        -d "{
            \"display_name\": \"${name}\",
            \"email\": \"${email}\",
            \"party_kind\": \"adopter\",
            \"origin\": \"manual_entry\",
            \"country_code\": \"US\",
            \"fpg_people_id3s\": ${rop3s_json},
            \"newsletter_opt_in\": false
        }" \
        -o /dev/null \
        -w "  ${name} (${email}): HTTP %{http_code}\n"
}

# Three adopters: one with a real FPG, one multi-FPG, one with none
# (lands as potential_adopter → triage queue).
seed_adopter "alice+adopter@example.com" "Alice Adopter" '["AAA01"]'
seed_adopter "bob+adopter@example.com" "Bob Adopter (multi-FPG)" '["AAA01", "AAA02"]'
seed_adopter "carol+adopter@example.com" "Carol Adopter (no FPG)" '[]'

# ─── 5. Trigger matching on the new contacts ─────────────────────────────
log "Running matching algorithm on each new adopter..."

for email in "alice+adopter@example.com" "bob+adopter@example.com" "carol+adopter@example.com"; do
    contact_id=$(psql_run "SELECT id FROM contacts WHERE email_normalized = '${email}' LIMIT 1;")
    if [[ -z "$contact_id" ]]; then continue; fi

    curl -fsS -X POST "${API_URL}/v1/matches/run/${contact_id}" \
        -H "Authorization: Bearer ${BEARER}" \
        -H "Content-Type: application/json" \
        -d '{"force": false}' \
        -o /dev/null \
        -w "  ${email}: HTTP %{http_code}\n" || warn "  matching run failed for ${email} (likely already has open match)"
done

# ─── Done ────────────────────────────────────────────────────────────────
log ""
log "Seed complete."
log ""
log "Verify in a browser:"
log "  http://localhost:3000/matches      — pending recommendations"
log "  http://localhost:3000/facilitator  — facilitator portal view"
log "  http://localhost:3000/contacts/new — add another by hand"
log ""
log "Or via curl:"
log "  curl -s -H 'Authorization: Bearer dev-local' ${API_URL}/v1/matches/queue | jq ."
log "  curl -s -H 'Authorization: Bearer dev-local' ${API_URL}/v1/drips/campaigns | jq ."
