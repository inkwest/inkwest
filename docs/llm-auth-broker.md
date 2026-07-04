# LLM auth broker

**Status — implemented, pending merge; OFF by default (`LLM_BROKER_ENABLED=false`).** Port of
the OAuth2 MCP reverse-proxy pattern (`docs/mcp-oauth2-proxy.md`) to the **model inference call**
itself.

## TL;DR

The sandboxed engines (Claude CLI, inkwest-agent, opencode) reach Bedrock only through squid. Today the
**real** `AWS_BEARER_TOKEN_BEDROCK` is mounted into the sandbox via `sandbox_env_file`, so a
compromised sandbox holds a valid Bedrock credential. The broker removes that: the sandbox
carries only a short-lived, opaque **per-turn grant**; a backend reverse-proxy verifies it,
swaps in the real bearer, and forwards to Bedrock. The real credential lives only in the
backend process. Every sandboxed transport sends `Authorization: Bearer …` — the Claude CLI
(`ANTHROPIC_BEDROCK_BASE_URL`), the inkwest-agent's boto3 (`endpoint_url`), and opencode's AI
SDK (`opencode.json` `provider.amazon-bedrock.options.baseURL`) — with **no SigV4**, so the
broker is a pure header swap + transparent byte passthrough; the eventstream is streamed back
untouched. Since the budget-accuracy work the broker additionally **meters** each data-plane
call: a passive tee (`app/services/llm_metering.py`) observes the forwarded bytes and, in a
detached post-stream task (bounded concurrency + pending cap, one idempotent retry — a slow
billing DB never sits in the proxied call's teardown), persists one authoritative
`usage_records` row per Bedrock call
(`source='broker'`, cache tokens included, shared per-turn `turn_id`) — with the broker on,
the orchestrators skip their own budget row (see `docs/BUDGET.md`). The bytes themselves are
still forwarded unmodified; a metering failure only logs `llm_proxy_metering_*` warnings and
bumps `inkwest_llm_metering_failures_total{reason}` — it never breaks the stream. A stream cut
mid-call (user Stop) is still billed by AWS: invoke-stream falls back to the Anthropic
self-report seen so far; converse-stream has no pre-terminal usage, so the cut call yields no
row and counts as a `no_usage` miss (stop semantics in `docs/BUDGET.md`).

## Components

| Piece | File | Role |
|---|---|---|
| Broker route (data plane) | `app/api/routes/llm_proxy.py` | `POST /api/llm-proxy/model/{id}/{op}` — verify grant, RESTRICT (`model_id` ∈ enabled registry models/ARNs, `op` ∈ `converse`/`converse-stream`/`invoke`/`invoke-with-response-stream`), swap `Authorization` → real bearer, stream to `bedrock-runtime.<region>.amazonaws.com`, and meter the response (one `source='broker'` usage row per call). |
| Metering tee | `app/services/llm_metering.py` | Decodes the forwarded bytes (eventstream via botocore / JSON body) into the billed token counts — `invocationMetrics` is the invoke-stream truth, Converse `usage` accepts both cache-key spellings; fail-safe (parse failure ⇒ no row + warning, never touches the stream). |
| Broker route (control plane) | same file | `GET /api/llm-proxy/{resource}` for a read-only allowlist (`inference-profiles`, `foundation-models`) → `bedrock.<region>.amazonaws.com`. Claude Code lists these to resolve a model to its **regional inference profile** (bare on-demand ids aren't invocable in regions like `eu-west-1`) and points the GETs at the same base URL — without this leg, resolution 404s and the data-plane invoke then fails. Same grant verify + bearer swap. |
| Grant store | `app/services/llm_proxy_token.py` | In-process registry of opaque per-turn grants. `mint` at turn start, `verify` by lookup, `burn` at turn end. **Not a JWT** — a JWT can't be revoked before expiry; the grant must die when the turn ends. |
| Wiring | `app/services/llm_broker.py` | `cli_broker_env` / `agent_broker_env` (inject), `agent_bedrock_endpoint_url`, `burn_env` (revoke). The single gate + base-URL derivation for all three engines (opencode reuses `agent_broker_env` + points its `provider.amazon-bedrock.options.baseURL` at the broker via its per-turn `opencode.json`). |
| Backend bearer | `Settings.aws_bearer_token_bedrock` | The real Bedrock API key, read from the backend process env `AWS_BEARER_TOKEN_BEDROCK`. |

Upstream is structured as a dispatch (`_UPSTREAMS`, `bedrock-bearer` today) so a Mantle /
direct-Anthropic-key shape can be added later without touching the route body.

## Enable

0. **Precondition — single worker / single replica.** The grant store is in-process, so with
   `LLM_BROKER_ENABLED=true` the backend **refuses to start** (`RuntimeError` at lifespan) when
   `WEB_CONCURRENCY>1`. It cannot detect `uvicorn/gunicorn --workers N` set without that env var,
   nor cross-host replicas — those must be single-replica or sticky-routed by deployment. Reconcile
   topology to single-worker **before** flipping the flag, or the boot fails fast.
1. **Backend process env**: `LLM_BROKER_ENABLED=true`, `AWS_BEARER_TOKEN_BEDROCK=<real key>`,
   `AWS_REGION=<region>`. The bearer lives **only here** now — and the model-catalog discovery
   (`model_catalog`) reads it from the **backend settings** too (never `sandbox.env`), so
   stripping the sandbox cred in step 3 doesn't break catalog refresh. Enable at least one model
   in admin settings: the broker is **fail-closed**, so with no enabled models the CLI turn is
   refused with a clear error (the agent path 403s — see Restriction).
2. **`MCP_PROXY_BASE_URL` must be set** — the broker base URL is `<that host>/api/llm-proxy`,
   so it inherits the MCP proxy's squid allowlist + host-gateway `--add-host` (see Squid). If
   it is unset the broker logs at **ERROR** (alertable — the fallback re-exposes the real bearer
   in the sandbox) and falls back to direct Bedrock.
3. **Migration — strip the sandbox cred**: remove `AWS_BEARER_TOKEN_BEDROCK` from
   `sandbox_env_file`. `CLAUDE_CODE_USE_BEDROCK` / `ANTHROPIC_BEDROCK_BASE_URL` there are
   harmless (the orchestrator injects them per turn via `docker exec -e`, which overrides the
   env-file), but point them at nothing real or drop them for clarity. **Until this step is
   done the real key is still readable inside the container** (`/proc/1/environ` of the idle
   container) — the flag only re-routes the running engine process; the env-file is the
   container's base env.

> **inkwest-agent engine — rebuild the agent image first.** The agent only routes through the
> broker if its baked-in `inkwest_agent` honours `bedrock_endpoint_url` (added in
> `PROTOCOL_VERSION = 5`). A toolbox image built before that ignores/rejects the field and would
> call Bedrock directly with the now-stripped key — broken model calls. Rebuild + redeploy the
> agent toolbox image (protocol ≥ v5 — v5 is the minimum, current images ship newer) **before**
> step 3 when the `inkwest-agent` engine is in use.
> This leg also requires the `inkwest-agent` engine enabled via `AGENTIC_ENGINES` (the CLI leg
> does not). Broker-OFF turns are unaffected: the orchestrator omits the unset field from the
> turn-spec, so an older agent still parses it.

> **opencode engine — rebuild the agent image + allowlist `models.dev`.** opencode ships in the
> same agent image (pinned `OPENCODE_VERSION`), so enabling it needs an image rebuild + redeploy,
> and its per-run model-catalog fetch needs `models.dev` on the squid allowlist. Broker wiring is
> automatic (the orchestrator writes the proxy `baseURL` into the per-turn `opencode.json` and
> injects the grant via `docker exec -e`); enable it via `AGENTIC_ENGINES` (e.g.
> `claude-cli,opencode`). With the broker on, `AWS_REGION` must be set in the backend env
> (`validate_settings` enforces it whenever the broker runs with a sandboxed engine).

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
  ends; the exfiltration window is the turn's duration, not a fixed lifetime — and within it
  the grant is bounded to `_MAX_CALLS_PER_GRANT` requests (see the backstops below).
- **Restriction (fail-closed).** Only enabled registry models (their ARNs + regional
  inference-profile ids) and the allowed Bedrock ops pass the broker. **No enabled rows ⇒
  nothing invocable** (unlike the default-open model picker — the broker is a credential gate).
  A grant cannot invoke an unconfigured model. The broker also strips any `x-amz-*` SigV4/
  session-token header from the sandbox's request, so a compromised sandbox can't inject AWS
  signing material onto the broker's authenticated upstream call (`x-amzn-bedrock-*` is kept).
- **Binding (current limit).** The grant carries `user_id`/`session_id` for audit, but the
  broker does not cross-check them against the request — Bedrock's URL has no session field.
  Isolation comes from the short, burned lifetime, **not** from per-call session enforcement. A
  grant alive mid-turn is interchangeable across enabled models. Tightening this (per-session
  model allowlist, or a forgeable-proof session binding) is deferred.
- **Single-replica — ENFORCED.** The grant store is in-process; a sandbox's broker call must
  reach the **same** backend process that minted its grant. The backend fail-fasts at startup
  when `LLM_BROKER_ENABLED=true` and `WEB_CONCURRENCY>1` (`app/main.py`); `--workers N` without
  that env var, and cross-host replicas, are undetectable from app code and need single-replica
  deployment or sticky routing.
- **TTL backstop + store cap + call cap + stream cap.** A 2 h TTL cleans up a grant orphaned
  when the backend is SIGKILLed before its `finally` runs; the explicit `burn` is the primary
  path. The store is also hard-capped (`_MAX_GRANTS`) — at the cap, mint evicts the
  oldest-expiring grant, so a burst of un-burned grants can't grow memory without bound between
  TTLs. Each grant additionally dies after `_MAX_CALLS_PER_GRANT` (500) verified requests —
  legit turns use tens — bounding the cost amplification of a grant that outlives its turn (a
  compromised sandbox in the crash-orphan window can no longer hammer Bedrock until the TTL).
  Each proxied response stream is force-closed at a 2 h total wall-clock ceiling
  (`LLM_PROXY_STREAM_MAX_SECONDS`) so a slowloris upstream can't pin a worker slot past the
  inter-byte idle timeout.
- **Recognizable grant.** Every grant is prefixed `inkwest-ephemeral-` (256-bit CSPRNG suffix),
  so it's instantly distinguishable in logs / `/proc/<pid>/environ` / a Bedrock error body from
  a real Bedrock key (`ABSK…`) — the prefix is cosmetic; the suffix is what's unguessable.
- **Network exposure.** The grant is the route's only application-level auth, so `/api/llm-proxy`
  must NOT sit on a public ingress: the sandbox reaches it solely via the host-gateway alias
  (the squid-allowlisted `mcp_proxy_base_url` host). Keep it off the public ALB / restrict it to
  the host-gateway interface so a leaked grant alone isn't usable off-host for its lifetime.

## Scope

In scope: all three sandboxed engines (CLI + inkwest-agent + opencode) — they cross the sandbox boundary, so the
broker keeps the real Bedrock credential out of the container. (There is no non-sandboxed
in-process inference path anymore; the legacy SDK/Converse orchestrators were removed.)
