# Disciple.Tools → jp-adopt-core UI parity inventory

**Source:** crawled DT staging (`adopt-staging.joshuaproject.net`) on 2026-05-26
via the Fields Explorer (`wp-admin → Utilities (D.T) → Fields`) and the contact
record / list UI. Staging is anonymized — no real PII.

**Purpose:** authoritative map of what DT's staff UI does vs. what jp-adopt-core
(`apps/web` + `apps/api`) does today, to drive a prioritized parity backlog.

> **Key framing:** DT is built on the stock disciple.tools CRM, so its contact
> carries ~85 fields. Many are **stock disciple-making fields** (faith status,
> baptism, milestones, coaching connections, groups) that the greenfield
> adoption app deliberately dropped. The parity target is the **adoption-domain**
> subset, not 1:1 field parity. Each field below is tagged
> **[ADOPT]** (adoption-relevant — a real parity candidate) or
> **[STOCK]** (stock DT discipleship — likely out of scope unless asked).

---

## 1. Module-level parity

| DT module | URL | jp-adopt-core equivalent | Status |
|---|---|---|---|
| Contacts (list + record) | `/contacts` | `/contacts`, `/adopters`, `/contacts/new` | thin — see §2/§3 |
| Groups | `/groups` | — | none ([STOCK] mostly) |
| Metrics / dashboards | `/metrics` | — | **MISSING** |
| Notifications | `/notifications` | — | **MISSING** |
| Settings | `/settings` | — | **MISSING** |
| Users / roles mgmt | `/user-management/users` | roles in DB, no UI | **MISSING** |
| People Groups (FPGs) | wp-admin post type + Mapping | `fpg` table (read-only) | partial |
| Drip Templates | wp-admin | drip engine (backend) | **no UI** (issue #55) |
| Workflows (automations) | `dt_options&tab=workflows` | outbox + worker | different model |
| Matching | (not a DT concept) | `/matches` (+ detail) | **jp-adopt-only** ✓ |

---

## 2. Contacts list view

DT `/contacts`:
- **Columns:** Name · Assigned To · Last Modified (configurable)
- **Saved filter tabs:** Adopters, Facilitators (+ Subassigned only, Shared with me)
- **Custom filter builder** + "Split By" + summary counts
- **Exports:** CSV List · BCC Email List · Phone List

jp-adopt-core `/contacts` + `/adopters`: flat list, no saved filters, no custom
filter builder, no exports, no split/summary.

**Gap:** saved/custom filters, split-by, summary counts, CSV/BCC/phone exports — all **MISSING**.

---

## 2.5 Canonical field set — sourced from jp-adopt-forms (the key ones)

**The intake forms, not the DT crawl, are the source of truth.** DT's ~85
contact fields are a mix of (a) fields the JP adoption forms actually collect
and (b) stock disciple.tools / WordPress built-ins the program never uses.

The forms repo (`jp-adopt-forms`) makes this precise:
- `src/lib/__tests__/dt-field-parity.test.ts` carries `DT_REGISTERED_FIELDS` —
  the **~50** DT field names the forms are allowed to send (built-ins + the
  `dt-adoption-fields` plugin). Anything outside it DT rejects.
- The two zod schemas are the canonical user-facing field sets:
  - Adoption (Form A): `src/app/[locale]/adopt-frontier-people-groups/schema.ts`
  - Facilitation (Form B): `src/lib/schema.ts`

**Split:** ~50 form-driven fields (Phase 2 target) vs ~35 DT/WP-only fields
(out of scope — §3f).

### Form-driven fields → jp-adopt-core status

**Shared contact fields (both forms)**
`entityName`/`orgName`, `contactName`/`primaryContactName`, `email`, `phone`,
`secondaryContact{Name,Email,Phone}`, `website`, `country`, `stateRegion`,
`preferredCommunication`, `newsletterOptIn`, `campaign`, `partner`,
`referralSource`, `additionalNotes`.
- Have today: display_name, email_normalized, country_code, newsletter_opt_in, origin.
- **MISSING:** phone, secondary contacts (×3), website, state_region,
  preferred_communication, campaign, partner, referral_source, additional_notes,
  org-name-vs-contact-name split.

**Adoption-only (Form A)**
`adopterType`, `entitySize`, `knowsTargetFpg`, `wantOrgGuidance`,
`guidanceInterests`, `wantFacilitatorConnection`, `missionsInvolvement` (12),
`wantPartnerConnection`, `partnerEntityTypes`, `desiredPartnerInfo`,
`hasDoctrinalDistinctives`, `doctrinalDistinctives`, `mouAccepted`. → all **MISSING**.

**Facilitation-only (Form B)**
`worksWithFpgs`, `willingToFacilitate`, `wantNetworkConnection`,
`networkPartnerInfo`, `partnerWith{Individuals,SmallGroups,Churches,Orgs,Networks}`
+ per-type `*Sizes`, `entitySize`, `ministryAreas`, `hasAccountabilityMembership`,
`accountabilityMemberships`, `mouSignatureName`. → all **MISSING**.

**Per-FPG selection → belongs on `AdopterInterest`, NOT `Contact`**
- Adoption: `commitmentTypes[]` per FPG.
- Facilitation: `engagementStatus` (ready/potential/none), `canFacilitate`,
  `facilitationServices[]`, `networkServices[]` per FPG.
- `AdopterInterest` today has rop3 + commitment_level + notes → needs
  commitment_types[], engagement_status, facilitation_services[], network_services[].

**MOU consent record (compliance artifact)**
Form A's API variant attaches `consents[]` (`consentType`, `version`,
`contentHash`, `acceptedAt`, `conversationId`, `evidence`). jp-adopt-core has no
equivalent — needs a `consent`/MOU table, not just a status field.

**DT-side CRM/automation fields the forms also write**
`last_contact_date`, `engagement_score`, `drip_campaign_status`,
`next_followup_date`, `mou_status`, `submission_id`, `fpg_submission_data`,
`file_download_url`, `donation_locations`, `donation_restrictions`.

> **Implication for the plan:** Phase 2's field list = the form-driven set above,
> minus what jp-adopt-core already models. ~30–33 net-new Contact-level fields
> + ~3–4 AdopterInterest fields + a consent/MOU table. The §3 matrix below
> (organized by DT tile) remains the visual layout reference, but **this section
> is the authoritative scope** for which fields get built.

---

## 2.6 Provenance: WordPress/stock-DT vs JP-custom (authoritative)

**Source of truth:** `dt-adoption-fields/includes/custom-fields.php` (the WordPress
plugin JP layers on top of the DT theme). Whatever that file registers is
**JP-custom**; everything else on the contacts post type is **stock
disciple.tools / WordPress**. The plugin also *explicitly hides* a set of stock
DT fields — that hide-list is the authoritative "not used by the adoption
program" set.

### A. JP-custom fields (42) — built by JP, grouped by the plugin's own tile keys

These tile keys are the **authoritative Phase 2 contact-page IA** (not guessed
from the crawl):

| Plugin tile key | Fields |
|---|---|
| `adopter_pipeline` | adopter_status |
| `facilitator_pipeline` | facilitator_status |
| `contact_info` | ministry_areas, entity_size, primary_contact_name, secondary_contact_name, secondary_contact_email, secondary_contact_phone, website, preferred_communication, form_country, form_state_region |
| `adoption_profile` | adopter_type, commitment_level, commitment_types, commitment_date |
| `facilitation_profile` | works_with_fpgs, willing_to_facilitate, facilitation_entity_types, facilitation_entity_sizes, mou_status, mou_signature_name |
| `connection_prefs` | want_facilitator_connection, facilitator_entity_types, desired_facilitator_info |
| `network_prefs` | want_network_connection, network_partner_info |
| `vetting` | has_doctrinal_distinctives, doctrinal_distinctives, has_accountability_membership, accountability_memberships |
| `fpg_commitments` | fpg_submission_data (hidden JSON blob, rendered by a custom tile) |
| `engagement` | last_contact_date, engagement_score, drip_campaign_status, next_followup_date |
| `form_submission` | submission_id, referral_source (readonly), campaign (readonly), partner (readonly), additional_notes, file_download_url (hidden) |

These ~42 are the **real "specific to our platform" set** and the Phase 2 build
target. jp-adopt-core already models 2–3 of them (adopter_status,
facilitator_status; commitment_level on AdopterInterest) → ~38 net-new.

### B. Stock DT/WordPress fields the platform KEEPS (used, not JP-built)
`title` (→ display_name), `type`, `sub_type` (→ party_kind), `contact_email`,
`contact_phone`, `notes`, plus DT system fields left visible (`assigned_to`,
`tasks`, `last_modified`, `post_date`, `favorite`, `requires_update`,
`duplicate_data`, `location_grid`). These are disciple.tools/WordPress, not JP —
jp-adopt-core re-implements the handful it needs natively.

### C. Stock DT/WordPress fields the plugin EXPLICITLY HIDES (definitively out of scope)
From `dt_adoption_hide_unused_fields()`: `overall_status`, `contact_status`,
`contact_facebook`, `contact_other`, `baptism_date`, `milestones`, `baptized_by`,
`baptized`, `faith_status`, `coaching`, `coached_by`, `seeker_path`, `tags`,
`relation`, `groups`, **`people_groups`**. Note JP hides DT's `people_groups`
connection and uses `fpg_submission_data` (JSON) + its own FPG model instead.

> **Net for the plan:** Phase 2 = section A (the JP-custom plugin fields), using
> the plugin tile keys as the page IA. Sections B/C are disciple.tools base, not
> parity work. Also authoritative from the plugin: DT remaps a single
> `contact_status` to adopter/facilitator status by `sub_type` and defaults to
> `new` — jp-adopt-core already splits these, so no remap shim is needed.

---

## 3. Contact field matrix (DT contacts post type → jp-adopt-core)

Status legend: ✓ present (model+UI) · ◐ backend only / partial · ✗ missing.

### 3a. Identity & contact info
| DT field | key | type | tag | jp-adopt-core | Status |
|---|---|---|---|---|---|
| Name | `name` | text | ADOPT | `Contact.display_name` | ✓ |
| Nickname | `nickname` | text | ADOPT | — | ✗ |
| Primary Contact Name | `primary_contact_name` | text | ADOPT | — | ✗ |
| Secondary Contact Name/Email/Phone | `secondary_contact_*` | text | ADOPT | — | ✗ |
| Phone | `contact_phone` | comm_channel | ADOPT | — | ✗ |
| Email | `contact_email` | comm_channel (multi) | ADOPT | `email_normalized` (single) | ◐ |
| Address | `contact_address` | comm_channel | ADOPT | — | ✗ |
| Facebook / Other social | `contact_facebook`, `contact_other` | comm_channel | ADOPT | — | ✗ |
| Website | `website` | text | ADOPT | — | ✗ |
| Country | `form_country` | text | ADOPT | `country_code` | ◐ |
| State / Region | `form_state_region` | text | ADOPT | — | ✗ |
| Languages | `languages` | multi_select | ADOPT | `language_codes` | ✓ |
| Preferred Communication | `preferred_communication` | key_select | ADOPT | — | ✗ |
| Locations / map grid | `location_grid`, `location_grid_meta` | location | ADOPT | — | ✗ (no geo/mapping) |

### 3b. Adoption status & lifecycle
| DT field | key | type | tag | jp-adopt-core | Status |
|---|---|---|---|---|---|
| Contact Sub Type (adopter/facilitator) | `sub_type` | key_select | ADOPT | `party_kind` | ✓ |
| Contact Status (master) | `overall_status` | key_select | ADOPT | — (split into two) | ◐ |
| Adopter Status | `adopter_status` | key_select | ADOPT | `adopter_status` (richer set) | ✓ |
| Facilitator Status | `facilitator_status` | key_select | ADOPT | `facilitator_status` | ✓ |
| Adopter Type (indiv/group/church/org/network) | `adopter_type` | key_select | ADOPT | — | ✗ |
| Entity Size | `entity_size` | key_select | ADOPT | — | ✗ |
| Reason Not Ready / Paused / Archived | `reason_unassignable`, `reason_paused`, `reason_closed` | key_select | ADOPT | transition `reason_code` (diff model) | ◐ |
| Accepted | `accepted` | boolean | ADOPT | match accept flow | ◐ |

### 3c. Facilitation & FPG
| DT field | key | type | tag | jp-adopt-core | Status |
|---|---|---|---|---|---|
| People Groups | `people_groups` | connection | ADOPT | `AdopterInterest.rop3` / `fpg` | ✓ |
| Works with FPGs | `works_with_fpgs` | boolean | ADOPT | implied by interest | ◐ |
| Willing to Facilitate | `willing_to_facilitate` | boolean | ADOPT | — | ✗ |
| Wants Facilitator / Network Connection | `want_facilitator_connection`, `want_network_connection` | boolean | ADOPT | — | ✗ |
| Facilitation Entity Types / Sizes | `facilitation_entity_*`, `facilitator_entity_types` | multi_select | ADOPT | — | ✗ |
| Desired Facilitator Activities | `desired_facilitator_info` | multi_select | ADOPT | — | ✗ |
| Network Partnership | `network_partner_info` | multi_select | ADOPT | — | ✗ |
| Commitment Level | `commitment_level` | key_select | ADOPT | `AdopterInterest.commitment_level` | ✓ |
| Commitment Types | `commitment_types` | multi_select | ADOPT | — | ✗ |
| Commitment Date | `commitment_date` | date | ADOPT | — | ✗ |
| Ministry Areas | `ministry_areas` | multi_select | ADOPT | — | ✗ |

### 3d. MOU, engagement, follow-up
| DT field | key | type | tag | jp-adopt-core | Status |
|---|---|---|---|---|---|
| MOU Status / Signature Name | `mou_status`, `mou_signature_name` | key_select/text | ADOPT | — | ✗ |
| Engagement Score | `engagement_score` | number | ADOPT | — | ✗ |
| Last Contact Date | `last_contact_date` | date | ADOPT | — | ✗ |
| Next Follow-up Date | `next_followup_date` | date | ADOPT | — | ✗ |
| Quick buttons (no-answer / established / meeting sched/complete/no-show) | `quick_button_*` | number | ADOPT | — | ✗ |
| Drip Campaign Status | `drip_campaign_status` | key_select | ADOPT | `Enrollment.state` (backend) | ◐ (no UI, #55) |

### 3e. Source / intake / admin
| DT field | key | type | tag | jp-adopt-core | Status |
|---|---|---|---|---|---|
| Sources | `sources` | multi_select | ADOPT | `origin` | ◐ |
| Referral Source | `referral_source` | text | ADOPT | — | ✗ |
| Form Submission ID / FPG Submission Data | `submission_id`, `fpg_submission_data` | text | ADOPT | `source_id` | ◐ |
| Campaign / Campaigns | `campaign`, `campaigns` | text/tags | ADOPT | — | ✗ |
| Partner | `partner` | text | ADOPT | — | ✗ |
| Additional Notes | `additional_notes` | text | ADOPT | — | ✗ |
| Tags | `tags` | tags | ADOPT | — | ✗ |
| Notes (activity) | `notes` | array | ADOPT | `activity_log` (backend) | ◐ (no UI, #56) |
| Assigned To (staff) | `assigned_to` | user_select | ADOPT | — | ✗ (no staff ownership model) |
| Sub-assigned | `subassigned`, `subassigned_on` | connection | ADOPT | — | ✗ |
| Tasks | `tasks` | task | ADOPT | — | ✗ |
| Favorite / Follow / Requires Update | `favorite`, `follow`, `requires_update` | bool/multi | ADOPT | — | ✗ (UX affordances) |
| Doctrinal Distinctives / Accountability | `doctrinal_distinctives`, `accountability_memberships`, `has_*` | text/bool | ADOPT | — | ✗ |

### 3f. Stock disciple.tools fields — likely OUT OF SCOPE
`faith_status`, `seeker_path`, `milestones` (faith milestones), `baptism_date`,
`baptism_generation`, `baptized` / `baptized_by` (connections), `coaching` /
`coached_by` (connections), `groups` / `group_leader` / `group_coach`
(connections), `relation` (contact↔contact), `gender`, `age`, `type` (user vs
personal contact), `corresponds_to_user*`. **[STOCK]** — confirm with Amy
whether any are wanted before building.

---

## 4. Contact record interactions (beyond fields)

DT record (`/contacts/{id}`) — 5 visible tiles (Details, Contact Information,
Engagement, Form Submission Details, Communication & Activity) **+ 7 hidden
tiles**, plus:
- **Comment + activity feed** with composer AND automatic **field-change history**
  ("Ministry Areas changed to…", "MOU Status changed…", emails sent, form submissions).
- **Quick-action buttons**, **inline edit** on every field, **status pills**, **tags**.
- **Admin actions:** Delete, View Contact History, Merge with another Contact, Change Contact Type, See duplicates.
- **"Send email (JP ADOPT)"** action.

jp-adopt-core: `activity_log` table exists (notes + change history possible) but
**no contact record UI** renders it. No quick actions, inline edit, merge/dedupe,
or contact-history view.

---

## 5. Net-new gaps (not in any GitHub issue yet)

- Saved + custom filters, split-by, summary counts (list)
- List exports: CSV / BCC email / phone
- **Staff assignment + sub-assignment** — design gap: jp-adopt routes to facilitator
  *orgs*, with no per-contact staff owner concept
- Quick-action buttons; tasks; next-follow-up reminders
- Metrics/dashboards, Notifications, Users/roles-management modules
- MOU tracking, engagement score, tags, sources, merge/dedupe, contact-history view
- Many adoption intake fields (entity type/size, facilitation prefs, secondary contacts, address/phone/social, state/region)

---

## 6. Recommended parity backlog (prioritized)

1. **Contact record page** (issue **#56**) — the keystone. Tiled, inline-edit,
   activity/comment feed + change history. Covers §3 fields + §4 interactions.
2. **Contacts list parity** (new) — saved/custom filters (Adopters/Facilitators
   tabs), summary counts, CSV/BCC/phone exports.
3. **Drips UI** (issue **#55**) — surfaces `drip_campaign_status` + enrollments.
4. **Facilitator/org admin UI** (issue **#57**).
5. **Richer match review + staff override** (issue **#52**).
6. **Decisions for Amy:** which §3d/§3e adoption fields are required for v1
   (MOU, engagement score, quick buttons, tasks, assignment), and which §3f stock
   DT fields (if any) to keep.

**Method note:** crawl artifacts in `/tmp/dtcrawl/` (`report.json`,
`fields_report.json`, screenshots `01`–`07`). Re-run via the Playwright scripts there.
