# Credentials at Rest — Audit & Plan

**Goal**: a dump of DB + disk + env vars, taken while no user is connected, must not yield
usable credentials.

**TL;DR verdict**: the *connector* credentials (AWS STS, GitLab OAuth) already meet the goal —
RAM-only, expiring, gone on restart. The real at-rest exposure is elsewhere: (1) MCP catalog
secrets fanned out in plaintext to durable, world-readable `.claude.json` files per
conversation; (2) the same secrets sitting in the `MCP_CATALOG` env var; (3) an earlier draft of the OAuth2-proxy
design (Fernet key in env + ciphertext in Postgres = self-contained decryption — since
resolved, §1.6); (4) static admin kubeconfigs and the sandbox model-creds env file on plain
disk. A full browser-held-key scheme is **not worth it** as a blanket policy (the
background-turn constraint breaks it, and org-level secrets have no single user to hold the
key) — optionally adopt it for per-user MCP OAuth tokens, fix the disk leaks mechanically,
and use KMS/sealed-key envelope encryption for everything org-level.

---

## Phase 1 — Inventory

### 1.1 Connector credentials (AWS / GitLab) — GOOD, already zero-at-rest

| Item | Where | Form | Lifetime | Who can read |
|---|---|---|---|---|
| AWS SSO access token, STS creds (per account/role) | `CredentialStore` dict — process RAM only (`backend/app/services/connectors/store.py`) | plaintext in RAM | token expiry (~1–8 h), session delete, or process exit | backend process memory (core dump, `/proc/<pid>/mem`, debugger) |
| GitLab access + refresh token | same RAM store (`gitlab.py`) | plaintext in RAM | ~2 h access / session lifetime | same |
| AWS `~/.aws` projection | tmpfs: `sandbox_creds_root/<sid>/.aws` (shared RAM volume) or `/dev/shm/inkwest/<sid>/.aws` (`connector_exec.write_aws_profiles`) | plaintext files, **0644/0755 when shared** | until revoke / session end / host reboot | any uid on the host that can reach the shared tmpfs; per-session ro bind mount is the only isolation |
| EKS kubeconfigs | tmpfs `<sid>/kube/` (`eks_refresh.py`) | **no embedded secret** — `aws eks get-token` exec plugin | same as above | n/a (secret-free by design) |
| PKCE `code_verifier` (connector OAuth) | Postgres `connector_pending_states` | plaintext column | ~10 min, swept on insert + checked on consume | DB readers — but useless without the matching auth code; acceptable |
| AWS device codes | route-module dict `_aws_device_pending` (RAM) | plaintext RAM | ~10 min | process memory |

Verdict: `MintedCredential.secret` never hits DB or disk (repr-suppressed, env-only injection,
never argv). The store dies with the process — **this layer already satisfies the goal**; its
known costs (single replica, re-auth after restart) are accepted and documented in `store.py`.

Caveats inside this layer:
- `write_aws_profiles` files are 0644 on the *shared* creds volume; treat any disk-backed
  misconfiguration of `sandbox_creds_root` as a P1 (nothing validates it is tmpfs).
- STS creds are in the conversation container's env/`~/.aws` while a turn runs — in-use
  exposure, out of scope of the at-rest claim (see 2.4).

### 1.2 MCP catalog secrets — MAIN GAP

| Item | Where | Form | Lifetime | Who can read |
|---|---|---|---|---|
| `MCP_CATALOG` (server defs with embedded API keys / static headers) | env var → compose `.env` / k8s manifest / `/proc/<pid>/environ` (`mcp_catalog.py`, `config.py`) | base64 = **plaintext-equivalent** | life of deployment | anyone with host, image-inspect, or env access |
| Per-conversation `.claude.json` (merged `mcpServers` incl. secrets) | **durable disk**: `sandbox_sessions_root/<sid>/claude/.claude.json` (`mcp_config._write_config_atomic`) | plaintext JSON, **mode 0666**, dir 0777 | survives idle reap and backend restart; deleted only on explicit session delete | every uid on the host; every backup of the sessions volume |

This is the single worst finding: an org-level shared secret is copied into N world-readable
files on durable disk, one per conversation that ever enabled the MCP, and lingers after the
sandbox is gone.

### 1.3 Sandbox durable dirs — secondary leak channels

| Item | Where | Risk |
|---|---|---|
| Claude CLI state + transcripts | `<sid>/claude/` (durable, 0777) | transcripts replay tool outputs; an agent that `cat`s a credential or env var persists it to disk |
| Agent scratch | `<sid>/workdir/` (durable, 0777) | `git clone https://oauth2:$GITLAB_TOKEN@…` writes the token into `.git/config` **on durable disk** — nothing prevents the agent doing this today |
| Compaction archives | `<sid>/workdir/.inkwest*/history-<n>.md` (file 0644; a directory the backend creates is 0755, an existing `.inkwest` keeps its mode; a fresh `.inkwest-<hex>/` when `.inkwest` is a symlink, a file or not linkable) | a verbatim dump of the pre-boundary transcript **including tool output** — same class as the Claude CLI transcripts above, and durable for the same lifetime. Deliberate: recall must reproduce what the investigation actually saw, so the content is not filtered. Written via `sandbox.publish_workdir_file` (symlink-safe against both a planted directory symlink and a mid-write swap: every path segment is opened `O_NOFOLLOW` and the file is hard-linked into place relative to that fd, never over an existing name; nothing the sandbox may own is moved, chmod'ed or deleted, and an abandoned archive is removed only if its inode is still the one published) with every row in full and each content block capped at ~200k characters; nothing caps the size or *number* of archives — the workdir lifecycle owns that |
| squid access logs | `<sid>/logs/squid/access.log` (0666) | CONNECT host names only; low sensitivity, keep |

### 1.4 Server master secrets (env) — classic, not zero-knowledge

| Item | Setting (`config.py`) | Blast radius if env dumps |
|---|---|---|
| JWT signing key | `secret_key` (default `change-me-in-production`, enforced ≠ default only in prod) | forge any user/admin session; also signs the planned mcp-proxy internal JWTs |
| OIDC client secret | `oidc_client_secret` | impersonate the app at the IdP |
| AWS IdC OAuth client secret | `aws_sso_oidc_client_secret` | run device-grant phishing as the app (user consent still required) |
| Anthropic / Bedrock keys, web-search key | `anthropic_api_key`, `aws_*`, `web_search_api_key` | spend / model access |
| Sandbox model-backend creds | `sandbox_env_file` — **plaintext file on host disk**, injected via `--env-file` | model-provider access from any host reader |

### 1.5 Database & static files

| Item | Where | Assessment |
|---|---|---|
| `clusters.kubeconfig_path` → static kubeconfigs | `/etc/inkwest/kubeconfigs` (admin-provisioned, plain disk, long-lived) | may embed client certs/tokens; plaintext at rest, no rotation story |
| `role_kubeconfig_mappings` | Postgres | paths only — clean |
| `connector_audit_log` | Postgres | metadata only — clean by design |
| `messages` (conversation content + tool outputs) | Postgres, plaintext | not credentials, but tool outputs are sensitive infra data at rest; note for a later "encrypt conversation bodies" decision, out of scope here |

### 1.6 Planned: `docs/mcp-oauth2-proxy.md` vs the goal

An earlier draft of the plan stored per-user OAuth tokens Fernet-encrypted in Postgres with
the key in env. Against this audit's goal that **fails by construction**: DB dump + env dump
= plaintext tokens (it covers DB-only compromise — backup leak, SQLi exfiltration — which is
useful, just not zero-knowledge). **Resolved**: the plan now stores tokens through the
shared `TokenVault` interface (`docs/connector-hot-auth.md` §5) with envelope encryption and
key custody per Phase B below (KMS instance-role or sealed-at-startup, no bare key in env).
The plan's other properties (tokens never in sandbox, scoped internal JWT, refresh at proxy)
were right from the start and are kept.

---

## Phase 2 — Design options

### 2.1 Browser-held key, supplied per WS session

Browser holds a KEK (passphrase-derived via Argon2id, or random WebCrypto key persisted in
IndexedDB); sends it to the backend over WSS at session start; backend keeps it **in RAM
only** while the user is connected, uses it to unwrap per-user secrets, evicts on disconnect
(+ optional TTL grace).

- Note on WebCrypto: a *non-extractable* IndexedDB key can never be sent to the server — that
  variant forces all unwrap operations client-side, which cannot feed a server-side CLI turn.
  For this architecture the key must be extractable (or passphrase-derived), i.e. XSS can
  steal it. Non-extractable buys nothing here.
- At-rest claim achieved: offline dump (DB + disk + env) holds only wrapped blobs.
- Not protected: live backend memory, malicious server code (can log the key on arrival),
  XSS in the SPA (can exfiltrate the key or just drive the session).

### 2.2 Envelope: per-user DEK wrapped by browser KEK

Server stores `wrap(KEK, DEK)` + `enc(DEK, secrets)`. Browser supplies KEK per session;
server unwraps DEK into RAM.

- Multi-device: wrap the same DEK under one KEK per device + one recovery-code KEK. Adding a
  device requires an already-keyed session to re-wrap.
- Recovery: lost passphrase + lost recovery code = secrets gone; mitigation is cheap **only
  because every secret here is re-obtainable** (re-run OAuth) — recovery failure costs a
  re-connect, not data loss. This makes the scheme unusually viable for this app.
- Cost: key UX (passphrase prompt or silent IndexedDB key), re-wrap flows, support burden.

### 2.3 Server-side comparators (honest threat coverage)

| Scheme | Dump of DB+disk+env yields creds? | Live-host attacker | Ops cost |
|---|---|---|---|
| Fernet key in env (rejected earlier draft) | **yes** | yes | ~zero |
| Sealed key at startup (operator passphrase / init secret, RAM-only) | no — key not at rest | yes | manual unseal on every restart |
| KMS envelope, **instance-role auth (no static AWS creds in env)** | no — decryption requires *executing* inside the role, not possessing files | yes (attacker with exec can call KMS) | low (AWS-native); ties deploy to AWS |
| Browser-held KEK (2.1/2.2) | no | yes (while a key is in RAM) | UX + recovery flows |

Key insight: **KMS-with-instance-role already satisfies the literal goal** ("DB + disk + env
vars do not yield usable credentials") for org-level secrets, with zero user-facing cost.
Browser keys only add value where the *user* is the natural key owner — per-user OAuth grants
— and where re-prompting is acceptable.

### 2.4 Hard constraint: credentials needed while the browser is away

Background turns (CLI still streaming after a tab close), `eks_refresh` fire-and-forget,
future scheduled work — all need plaintext creds with no browser attached. Honest options:

1. **RAM cache with TTL** (recommended): key/DEK stays in backend RAM for N hours after last
   WS disconnect; long enough to finish a turn, short enough to bound exposure. Zero-knowledge
   holds for *cold* dumps (restart, stolen snapshot, backup) — which is what the stated goal
   describes — but not for a live-RAM capture during the TTL window. Say so in the docs.
2. Re-prompt on demand: background work fails with "re-connect to continue" — kills scheduled
   work, poor UX.
3. Server-side escrow for scheduled work: explicit per-task opt-in that re-wraps the needed
   credential under a server key — a documented, user-visible downgrade, not a silent one.

After a backend restart the RAM cache is empty: every user re-supplies the key (transparent if
IndexedDB-held, a prompt if passphrase). This matches the *existing* behavior of the RAM-only
connector store, so it is an already-accepted operational property, not a regression.

### 2.5 Sandbox disk files — scope the claim

While a conversation is live, the toolbox necessarily sees plaintext (`~/.aws` tmpfs, env
vars, `.claude.json`). That is **in-use**, not at-rest. The defensible claim is: *no secret
survives on durable media after the sandbox/turn ends, and no secret is readable on durable
media while it runs*. tmpfs satisfies it; the current `.claude.json` and `.git/config`-style
workdir leaks violate it and are fixable (3.x below) without any cryptography.

---

## Phase 3 — Recommendation & plan

**Verdict: partial adoption.** Do not build a blanket browser-key system. Three secrets
classes, three treatments:

| Class | Treatment |
|---|---|
| Per-session connector creds (AWS/GitLab, derived layer) | keep RAM-only store — already meets the goal; no change |
| Per-user platform grants (planned, `docs/connector-hot-auth.md` §5) | durable refresh tokens in the vault — encrypted with Phase B key custody; same browser-DEK upgrade path as MCP tokens applies |
| Org-level secrets (MCP catalog, OIDC/IdC client secrets, JWT key, model keys) | no user owns these → browser keys structurally impossible; use KMS envelope (instance role) or sealed-at-startup key; meanwhile kill the plaintext-on-disk copies |
| Per-user MCP OAuth tokens (planned proxy) | the one good fit for browser-wrapped envelope encryption — user-owned, re-obtainable on key loss, decryption naturally co-occurs with user activity |

### Phase A — stop the bleeding (no crypto, ~days)

1. **Secrets out of `.claude.json`**: write `${VAR}`-style placeholders (Claude Code expands
   env vars in `mcpServers` `env`/`headers`) and inject the real values per turn via the
   existing valueless `docker exec -e KEY` path (`ai_cli_client.py` already does this for
   connector creds). Durable file becomes secret-free. Fallback if expansion proves unreliable
   for a server type: move `.claude.json` to the creds tmpfs and symlink — costs `--resume`
   durability only for the MCP block, not transcripts.
2. Tighten modes: `.claude.json` 0666→0640-equivalent strategy (group-based or matching uid),
   document why 0777 dirs exist and what compensates.
3. Workdir token leak: set `GIT_TERMINAL_PROMPT=0` + inject a `git credential` helper reading
   `$GITLAB_TOKEN` (askpass), and instruct the agent (system prompt) to clone without inlining
   tokens in URLs; optionally scrub `workdir/**/.git/config` at teardown.
4. Startup guard: refuse `sandbox_creds_root` that is not on tmpfs (`statvfs`/`/proc/mounts`).
5. Static kubeconfigs (`/etc/inkwest/kubeconfigs`): document as legacy plaintext-at-rest;
   prefer the exec-plugin (secret-free) pattern for new clusters; rotate or retire embedded
   long-lived tokens.

### Phase B — org-level key custody (~days, ops-dependent)

6. ~~Amend `mcp-oauth2-proxy.md`~~ **done** — the plan now specifies envelope encryption
   behind the shared `TokenVault` interface (`docs/connector-hot-auth.md` §5): data keys
   wrapped by AWS KMS (auth via instance/IRSA role, **no static KMS creds in env**), with
   sealed-at-startup as the alternative backend behind the same `app/core/crypto.py`
   interface for non-AWS deploys. Remaining work here = implementing the backends.
7. Move `MCP_CATALOG` secret payloads into the same encrypted store (catalog structure stays
   env-driven; secret values become references), so the env var stops being a credential dump.

### Phase C — browser-wrapped per-user tokens (optional, ~1–2 weeks, only if the
zero-knowledge property is actually demanded)

8. Per-user DEK; wrapped copies per device KEK (IndexedDB WebCrypto key, silent) + recovery
   code; KEK sent over WSS on session open; backend RAM cache with a 4–8 h post-disconnect
   TTL for background turns; restart ⇒ users transparently re-key on next page load.
9. Key-loss path = "reconnect your MCPs" (tokens re-obtainable) — no data-loss recovery
   engineering needed.

### What none of this protects against — say it out loud

- An attacker with **code execution on the live backend**: reads RAM keys as they arrive,
  or calls KMS with the instance role. Browser keys narrow the window to connected/TTL
  periods; they do not close it.
- **Malicious or compromised server code**: can exfiltrate keys by design (the browser hands
  the key to the server). True zero-knowledge would require client-side-only crypto, which is
  incompatible with server-side CLI execution.
- **XSS in the SPA**: steals the extractable key or simply drives authenticated sessions.
  CSP + no-inline-script hygiene is a prerequisite for Phase C to be worth anything.
- **In-use exposure** in the sandbox while a turn runs (env vars, tmpfs files, transcripts of
  whatever the agent prints). Scope every claim as *at rest on durable media*.
