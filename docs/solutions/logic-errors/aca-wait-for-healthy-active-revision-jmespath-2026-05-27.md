---
title: ACA wait-for-healthy false-positive — `[?active==true]|[0]` matches the OLD revision
date: 2026-05-27
module: .github/workflows/deploy.yml
problem_type: logic_error
component: tooling
severity: medium
symptoms:
  - "Deploy workflow's wait-for-healthy step reported `Healthy` on poll 1, before the new image was actually serving"
  - "Subsequent smoke check failed because the new revision had not finished activating yet"
  - "Logs showed the same JMESPath result repeatedly even though `az containerapp revision list` clearly had a new revision in `Activating` state"
root_cause: logic_error
resolution_type: code_fix
related_components:
  - development_workflow
tags:
  - azure
  - container-apps
  - deploy
  - jmespath
  - polling
  - revisions
  - false-positive
---

# ACA wait-for-healthy false-positive — `[?active==true]|[0]` matches the OLD revision

## Problem

The Container Apps deploy workflow's "wait for new revision to become
ready" step reported success on the very first poll, before the new
revision had actually finished activating. The smoke check that
followed then failed because it was hitting the old revision's image
(or a 502 during the cutover window).

## Symptoms

- `Healthy` reported by the poll loop ~5 seconds after
  `az containerapp update` returned
- Subsequent `curl /healthz` returned the *previous* deploy's SHA
- `az containerapp revision list` showed both the old revision (still
  `active: true`) and a brand-new revision in `provisioningState:
  Provisioned, runningState: Activating`

## What Didn't Work

- Adding `sleep 10` before the poll loop — the old revision was still
  `active: true` during the activation window, so the query still
  matched it. The sleep just delayed the wrong-revision match.
- Increasing the poll timeout — the loop already exited successfully on
  poll 1 against the wrong revision; making it wait longer changed
  nothing.
- Filtering by image tag in JMESPath — fragile, broke on retry deploys
  where two revisions shared the same image tag.

## Solution

**Capture the new revision's name from the `az containerapp update`
call itself, then poll that specific revision by name** instead of
trying to discover it via `[?active==true]`. During an ACA rolling
update the OLD revision stays `active: true` until the new one is
fully provisioned AND running — so `[?active==true]|[0]` reliably
returns the wrong revision for the entire activation window.

```bash
set -euo pipefail

new_rev=$(az containerapp update \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --image "$IMAGE_TAG" \
  --set-env-vars "DEPLOY_SHA=$SHA" \
  --query properties.latestRevisionName \
  -o tsv)

if [ -z "$new_rev" ]; then
  echo "::error::Could not determine new revision name"
  exit 1
fi

echo "Polling revision $new_rev for readiness…"

for i in $(seq 1 60); do
  # tsv output is NEWLINE-separated for multi-value queries (NOT tab-separated).
  read -r prov < <(az containerapp revision show \
    --name "$APP_NAME" \
    --resource-group "$RG" \
    --revision "$new_rev" \
    --query "properties.provisioningState" -o tsv)
  read -r running < <(az containerapp revision show \
    --name "$APP_NAME" \
    --resource-group "$RG" \
    --revision "$new_rev" \
    --query "properties.runningState" -o tsv)

  echo "[$i] provisioningState=$prov runningState=$running"

  if [ "$prov" = "Provisioned" ] && [ "$running" = "Running" ]; then
    echo "Revision $new_rev is healthy."
    exit 0
  fi
  sleep 5
done

echo "::error::Revision $new_rev did not become healthy in time"
exit 1
```

## Why This Works

`az containerapp update` returns the new revision's name in
`properties.latestRevisionName`. Capturing it locks the poll loop onto
the specific revision being deployed, immune to the old revision's
`active=true` state during the activation overlap window. Readiness is
defined as **both** `provisioningState=Provisioned` AND
`runningState=Running` — checking only one of the two leaves a window
where the revision is provisioned but not yet accepting traffic.

A second subtle gotcha that came out during debugging: `az ... -o tsv`
returns multiple query values **newline-separated**, NOT tab-separated.
A naive `IFS=$'\t' read -r prov running <<< "$output"` will read both
values into `$prov` and leave `$running` empty, producing infinite
"`prov=Provisioned running=`" log lines until timeout. Use one
single-value query per `read`, or `{ read -r prov; read -r running; }
<<< "$output"`.

## Prevention

- Never trust `[?active==true]|[0]` to find a "new" revision during a
  rolling update. The Azure platform's definition of "active" includes
  both the old and new during the overlap window.
- Capture `properties.latestRevisionName` at the moment of the update
  call and poll *that specific name*.
- Require **both** `provisioningState=Provisioned` and
  `runningState=Running` before declaring healthy.
- When parsing `-o tsv` output with multiple query values, treat the
  separator as newline, not tab. Test the parser with a known
  multi-value query before assuming.
- Bound the poll loop with an explicit iteration cap + sleep, and emit
  each poll's observed state to logs — silent loops obscure exactly
  this class of failure.

## References

- jp-adopt-core PR #66 — Phase 1 SWA → Container App migration
  (original sighting of the false-positive)
- `.github/workflows/deploy.yml` — `Wait for new API revision to
  become ready` step (current correct shape)
- [Azure docs — Container Apps revisions
  lifecycle](https://learn.microsoft.com/en-us/azure/container-apps/revisions)
