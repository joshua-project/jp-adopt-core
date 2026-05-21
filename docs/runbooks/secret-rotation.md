# Secret rotation runbook (U12)

ACA + Key Vault references do NOT auto-rotate. When a referenced
secret rotates, the container revision needs a restart for the new
value to take effect. This runbook covers the common cases.

## Secrets that may rotate

| Secret | Storage | Effect of stale value |
|--------|---------|------------------------|
| `DATABASE_URL` (runtime app user password) | Key Vault → ACA secret ref | API can't reach Postgres; `/readyz` returns 503 |
| `MAGIC_LINK_SIGNING_KEY` | Key Vault → ACA secret ref | New magic-link issues sign with the new key; mid-flight tokens issued under the old key fail validation (acceptable; 15-min TTL) |
| `ACS_CONNECTION_STRING` | Key Vault → ACA secret ref | Worker can't send email; ACS attempt logs `acs_sdk_missing` or auth-failure exceptions |
| `WEBHOOK_HMAC_SECRET` (outbox webhook signing) | Key Vault → ACA secret ref | Downstream webhook consumers fail signature verification on new deliveries |
| `INTAKE_API_KEYS` | Key Vault → ACA secret ref | jp-adopt-forms POSTs return 401 unauthorized once the key is rotated |
| `OP_SERVICE_ACCOUNT_TOKEN` (CI/CD) | GitHub repo secret | Deploy workflow fails at the `Load deploy secrets from 1Password` step |
| `azure_client_id` for OIDC federation | 1Password vault | Deploy workflow's `azure/login@v2` step fails |

## Rotation procedure (Key-Vault-backed secrets)

The "ACA secret ref does NOT auto-rotate" gotcha bites here. After
updating the Key Vault secret value:

```bash
# 1. Update the Key Vault secret value
az keyvault secret set \
  --vault-name kv-jp-adopt-prod \
  --name database-url \
  --value "<new-connection-string>"

# 2. Force a revision restart on the container apps that reference it.
#    Without this, the running pods keep the OLD value in memory.
az containerapp update \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --set-env-vars "REVISION_RESTART_TOKEN=$(date +%s)"

az containerapp update \
  --name jp-adopt-worker \
  --resource-group rg-jp-adopt-prod \
  --set-env-vars "REVISION_RESTART_TOKEN=$(date +%s)"

# 3. Verify the new revision picked up the new value
fqdn=$(az containerapp show \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --query "properties.configuration.ingress.fqdn" -o tsv)
curl -fsS "https://${fqdn}/readyz"
# Expect status:ready
```

The `REVISION_RESTART_TOKEN` env var has no functional effect on the
app — it exists solely so changing it forces ACA to roll a new
revision. Use a fresh timestamp each rotation.

## Rotation procedure (intake API keys — multi-key)

Intake API keys support staged rotation via the comma-separated
`INTAKE_API_KEYS` setting. Procedure for adding a new key without
breaking jp-adopt-forms in-flight:

```bash
# 1. Add the new key to the existing comma-separated list (old key first)
az keyvault secret set \
  --vault-name kv-jp-adopt-prod \
  --name intake-api-keys \
  --value "<old-key>,<new-key>"

# 2. Restart the API container so it picks up both keys
az containerapp update \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --set-env-vars "REVISION_RESTART_TOKEN=$(date +%s)"

# 3. Update jp-adopt-forms env to send the NEW key. Deploy + verify.

# 4. Once forms is fully on the new key, drop the old one:
az keyvault secret set \
  --vault-name kv-jp-adopt-prod \
  --name intake-api-keys \
  --value "<new-key>"

az containerapp update \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --set-env-vars "REVISION_RESTART_TOKEN=$(date +%s)"
```

## Rotation procedure (`OP_SERVICE_ACCOUNT_TOKEN`)

1. In 1Password, generate a new service-account token scoped to read
   `JoshuaProject/jp-adopt-core`.
2. Add it to the GitHub repo secret `OP_SERVICE_ACCOUNT_TOKEN`.
3. Run a no-op deploy to verify the new token works:
   ```bash
   gh workflow run deploy.yml -f target=api -R joshua-project/jp-adopt-core
   ```
4. Revoke the old token in 1Password.

## What NOT to rotate without coordination

- **The Azure AD app registration's federated identity.** Rotating
  this breaks every active deploy. Coordinate with the JP IT team
  before changing the subject pattern or audience.
- **B2C tenant signing keys.** Managed by Azure; not something we
  rotate manually.
- **The Postgres migrator role password.** Rotating mid-deploy breaks
  the migrate step. Schedule rotation outside the cutover window and
  rebuild the GitHub Actions workflow's stored token between schedule
  and execution.

## Audit

Every rotation should leave an audit trail in the operator handbook
(`docs/runbooks/operator-handbook.md`, U14) — date, secret rotated,
who, and why.
