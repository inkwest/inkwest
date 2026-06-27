# LLM auth broker

**Status — shipped, OFF by default (`LLM_BROKER_ENABLED=false`).** Port of the OAuth2 MCP
reverse-proxy pattern (`docs/mcp-oauth2-proxy.md`) to the **model inference call** itself.

## TL;DR

The sandboxed engines (Claude CLI, inkwest-agent) reach Bedrock only through squid. Today the
**real** `AWS_BEARER_TOKEN_BEDROCK` is mounted into the sandbox via `sandbox_env_file`, so a
compromised sandbox holds a valid Bedrock credential. The broker removes that: the sandbox
carries only a short-lived, opaque **per-turn grant**; a backend reverse-proxy verifies it,
swaps in the real bearer, and forwards to Bedrock. The real credential lives only in the
backend process. Both the Claude CLI (`ANTHROPIC_BEDROCK_BASE_URL`) and boto3 (`endpoint_url`)
send `Authorization: Bearer …` — **no SigV4** — so the broker is a pure header swap +
transparent byte passthrough; the eventstream response is streamed back untouched.

## Components

| Piece | File | Role |
|---|---|---|
| Broker route (data plane) | `app/api/routes/llm_proxy.py` | `POST /api/llm-proxy/model/{id}/{op}` — verify grant, RESTRICT (`model_id` ∈ enabled registry models/ARNs, `op` ∈ `converse`/`converse-stream`/`invoke`/`invoke-with-response-stream`), swap `Authorization` → real bearer, stream to `bedrock-runtime.<region>.amazonaws.com`. |
| Broker route (control plane) | same file | `GET /api/llm-proxy/{resource}` for a read-only allowlist (`inference-profiles`, `foundation-models`) → `bedrock.<region>.amazonaws.com`. Claude Code lists these to resolve a model to its **regional inference profile** (bare on-demand ids aren't invocable in regions like `eu-west-1`) and points the GETs at the same base URL — without this leg, resolution 404s and the data-plane invoke then fails. Same grant verify + bearer swap. |
| Grant store | `app/services/llm_proxy_token.py` | In-process registry of opaque per-turn grants. `mint` at turn start, `verify` by lookup, `burn` at turn end. **Not a JWT** — a JWT can't be revoked before expiry; the grant must die when the turn ends. |
| Wiring | `app/services/llm_broker.py` | `cli_broker_env` / `agent_broker_env` (inject), `agent_bedrock_endpoint_url`, `burn_env` (revoke). The single gate + base-URL derivation for both engines. |
| Backend bearer | `Settings.aws_bearer_token_bedrock` | The real Bedrock API key, read from the backend process env `AWS_BEARER_TOKEN_BEDROCK`. |

Upstream is structured as a dispatch (`_UPSTREAMS`, `bedrock-bearer` today) so a Mantle /
direct-Anthropic-key shape can be added later without touching the route body.

## Enable

1. **Backend process env**: `LLM_BROKER_ENABLED=true`, `AWS_BEARER_TOKEN_BEDROCK=<real key>`,
   `AWS_REGION=<region>`. The bearer lives **only here** now — and the model-catalog discovery
   (`model_catalog`) reads it from the **backend settings** too (never `sandbox.env`), so
   stripping the sandbox cred in step 3 doesn't break catalog refresh.
2. **`MCP_PROXY_BASE_URL` must be set** — the broker base URL is `<that host>/api/llm-proxy`,
   so it inherits the MCP proxy's squid allowlist + host-gateway `--add-host` (see Squid). If
   it is unset the broker logs a warning and falls back to direct Bedrock.
3. **Migration — strip the sandbox cred**: remove `AWS_BEARER_TOKEN_BEDROCK` from
   `sandbox_env_file`. `CLAUDE_CODE_USE_BEDROCK` / `ANTHROPIC_BEDROCK_BASE_URL` there are
   harmless (the orchestrator injects them per turn via `docker exec -e`, which overrides the
   env-file), but point them at nothing real or drop them for clarity. **Until this step is
   done the real key is still readable inside the container** (`/proc/1/environ` of the idle
   container) — the flag only re-routes the running engine process; the env-file is the
   container's base env.

## Rollback (fully reversible)

Set `LLM_BROKER_ENABLED=false` **and** restore `AWS_BEARER_TOKEN_BEDROCK` in `sandbox_env_file`.
The engines revert to mounting the real bearer into the sandbox (today's behavior). No schema,
no data migration.

## Squid — nothing to change

The broker shares the `MCP_PROXY_BASE_URL` host (a distinct `/api/llm-proxy` path prefix), so
it reuses the existing allowlist entry + `--add-host` that `app/services/sandbox.py` already
writes for the MCP proxy. `.amazonaws.com` stays open at the network level — but with no valid
Bedrock credential inside the sandbox it is no longer a credentialed Bedrock egress path.

## Security model

- **Per-turn grant, burned on turn end.** Minted at turn start, injected into the sandbox,
  and `burn`ed in the orchestrator `finally` (every exit path — cancel/error included, after
  the agent process is gone). A token exfiltrated from a sandbox is dead the moment the turn
  ends; the exfiltration window is the turn's duration, not a fixed lifetime.
- **Restriction.** Only enabled registry models (or their ARNs) and the allowed Bedrock ops
  pass the broker. A grant cannot invoke an unconfigured model.
- **Binding (current limit).** The grant carries `sub`/`sid` for audit, but the broker does
  not cross-check them against the request — Bedrock's URL has no session field. Isolation
  comes from the short, burned lifetime, **not** from per-call session enforcement. A grant
  alive mid-turn is interchangeable across enabled models. Tightening this (per-session model
  allowlist, or a forgeable-proof session binding) is deferred.
- **Single-replica assumption.** The grant store is in-process; a sandbox's broker call must
  reach the **same** backend process that minted its grant. This matches the documented
  single-worker / single-replica deployment (see the metrics note in `app/core/config.py`).
  Scale-out needs a shared store or sticky routing.
- **TTL backstop.** A 2 h TTL cleans up a grant orphaned when the backend is SIGKILLed before
  its `finally` runs; the explicit `burn` is the primary path.

## Scope

In scope: the sandboxed `claude-cli` deployment (CLI + inkwest-agent engines) — both cross the
sandbox boundary. Out of scope: the non-sandboxed `bedrock` provider (`orchestrator.py` /
`orchestrator_converse.py`) holds creds in-process and never hands them to a sandbox.
