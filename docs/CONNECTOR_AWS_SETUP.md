# AWS connector setup (IAM Identity Center)

Runbook to enable Inkwest's AWS access: **Device Authorization Grant** to
**IAM Identity Center** → **multi-account/role** selection → **temporary STS (~1 h)**.

> Why the device grant and not the redirect flow? Identity Center public
> clients only accept **loopback** redirect URIs (and in a very
> constrained way). The device grant requires **no redirect URI** — it is the
> flow `aws sso login` uses, robust and supported.
>
> **Read-only** is guaranteed by the chosen **role / permission set** (not by an
> allowlist in code). Use read-only permission sets.

---

## 0. Prerequisites

- **IAM Identity Center enabled** on the AWS organization.
- One (or more) **read-only permission set(s)** **assigned to your user** on the
  accounts to investigate (otherwise the account/role list will be empty). The
  **existing SSO users/groups/permissions are reused** — nothing to create.
- AWS access to register the OIDC client (management account / CloudShell).
- The `aws` CLI binary is present in the backend image (done).

---

## 1. Register the OIDC client (→ `client_id` + `client_secret`)

A **simple public client** is enough (the device grant takes neither redirect URI nor
explicit grant-types). From **AWS CloudShell** (management account):

```bash
aws sso-oidc register-client \
  --client-name "inkwest" \
  --client-type public \
  --region eu-west-1            # your Identity Center region
```

The response contains **`clientId`** and **`clientSecret`** → these are
`AWS_SSO_OIDC_CLIENT_ID` and `AWS_SSO_OIDC_CLIENT_SECRET`.

> ⚠️ The `clientSecret` expires (~90 days) → you will need to re-register a client and
> update the `.env` at that point (no delete API: old
> clients expire on their own).

Also get the **access portal URL**: console → **IAM Identity Center →
Settings → AWS access portal URL** (e.g. `https://d-xxxxxxxxxx.awsapps.com/start`).

---

## 2. Configure Inkwest (`.env` at the repo root)

```bash
CONNECTORS_ENABLED=true

AWS_SSO_ENABLED=true
AWS_SSO_REGION=eu-west-1                              # Identity Center region
AWS_SSO_START_URL=https://d-xxxxxxxxxx.awsapps.com/start
AWS_SSO_OIDC_CLIENT_ID=<clientId from step 1>
AWS_SSO_OIDC_CLIENT_SECRET=<clientSecret from step 1>   # exact name: ..._OIDC_CLIENT_SECRET

# Optional — regex filter on offered roles (defense in depth, see §5)
# AWS_SSO_ROLE_FILTER=read-?only$
```

Apply (recreates the backend so it re-reads the `.env`):

```bash
docker compose up -d backend
```

> No need for `CONNECTOR_REDIRECT_BASE` for AWS (no redirect). This variable
> is only used by OAuth-redirect connectors such as GitLab.

---

## 3. Usage (UI)

1. Chat session → **External access** banner → **AWS** row → **Give access**.
2. A **small popup** opens on the AWS verification page + a **code** is
   displayed → sign in and **confirm the code** (the popup closes automatically
   once approved).
3. The **selection modal** opens: **checkboxes**, **grouped by account**
   (sorted), **search** at the top, **select/deselect all**. Pick **one or
   more** accounts/roles.
4. **Give access** → status **connected** (~1 h). One **AWS profile** is created per
   selected pair.
5. In the chat, the agent can:
   ```
   aws configure list-profiles
   aws --profile <account>-<role> eks list-clusters
   aws s3 ls                       # default profile = 1st selected
   ```

---

## 4. How it works (technical)

```
Give access AWS
  → StartDeviceAuthorization (clientId/secret/startUrl) → userCode + verificationUri
  → the user approves in the browser
  → polling CreateToken (grant device_code) → SSO access token
  → ListAccounts + ListAccountRoles → allowed accounts/roles (multi-select modal)
  → GetRoleCredentials × N (one per pair) → N STS sets (~1 h) stored IN RAM
  → each turn: RAM projection → tmpfs ~/.aws/{credentials,config} (one profile
    per pair + [default]); AWS_SHARED_CREDENTIALS_FILE/AWS_CONFIG_FILE injected
```

**Storage**: the STS creds live in the Python **RAM store** (source of truth). At
execution time, they are projected into **RAM** tmpfs — the shared creds volume
`<sandbox_creds_root>/<sid>/.aws` (mode `0644`, mounted read-only in the sandbox,
which runs under another uid) with a sandbox, otherwise the backend's local `/dev/shm`
(mode `0600`) — **never on persistent disk**, and **deleted on revocation**.

Files: `backend/app/services/connectors/aws.py` (device grant + multi-select),
`backend/app/tools/connector_exec.py` (`write_aws_profiles` / tmpfs),
`backend/app/api/routes/connectors.py` (endpoints `aws/device/start`,
`aws/device/poll`, `aws/options`, `aws/select`).

---

## 5. Security / least privilege

- **Read-only at the IAM level**: choose read-only permission sets
  (no allowlist in code on the `aws_call` side).
- **STS ~1 h**: temporary, in RAM (tmpfs at execution time), never in
  log/prompt/browser; argv never contains the secret (only the path
  of the tmpfs file).
- **Capped by the user's SSO**: no escalation.
- **Role filter (`AWS_SSO_ROLE_FILTER`)**: second lock on the backend side. **Case-insensitive**
  regex, matched (`search`, so unanchored — the operator places
  their own `^`/`$`) against **`<account_name>.<role_name>`**
  (e.g. `acme-integration.AWSReadonlyAccess`). Only roles that **match**
  appear in the modal and are mintable; an account with no matching role
  **disappears**. Empty / unset → **no filter** (all roles).
  Applied to SSO enumeration (`provisional_from_token` → `available_roles`); the
  selection (`select_account_roles`) only mints what was offered, so a
  filtered role is de facto unselectable. Invalid regex → **startup failure**.
  Read-only example: `AWS_SSO_ROLE_FILTER=read-?only$`.
- **Per-session partitioning**: one tmpfs `~/.aws` file per session.
- **Revocation**: Revoke button → creds purged from the store + tmpfs files deleted
  (the STS creds expire at ~1 h anyway).

---

## 6. Troubleshooting

| Symptom | Lead |
|---|---|
| AWS connector missing from the UI | `AWS_SSO_ENABLED`/`AWS_SSO_REGION`/`AWS_SSO_START_URL`/`AWS_SSO_OIDC_CLIENT_ID`/`AWS_SSO_OIDC_CLIENT_SECRET` missing → backend recreated after the `.env`? |
| `register-client`: "unknown options" | CLI too old → use **CloudShell** (recent CLI) |
| `register-client`: "Could not connect to endpoint" | region typo (e.g. `eu-west1` instead of `eu-west-1`) |
| Stays "awaiting approval" | you have not confirmed the code in the AWS popup yet |
| Empty account/role list | no permission set assigned to your user on the accounts — **or** `AWS_SSO_ROLE_FILTER` too strict (matches no `account.role`) |
| Backend refuses to start (`AWS_SSO_ROLE_FILTER is not a valid regex`) | malformed `AWS_SSO_ROLE_FILTER` regex → fix or remove the variable |
| `aws` fails "no credentials" in the chat | AWS access not granted / expired → click "Give access" again |
| `clientSecret` expired (~90 d) | re-register a client + update the `.env` |
