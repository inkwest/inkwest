# Sandboxed Agent Runtime + Provisioning Worker — Plan

> **Update — `AI_PROVIDER` removed.** The engine is now the *sole* driver of a turn:
> `AI_PROVIDER` and the in-process orchestrators (`orchestrator.py` /
> `orchestrator_converse.py` / `orchestrator_common.py`) have been deleted. References
> to `AI_PROVIDER` and to those files below are historical context for the plan; the
> live runtime is the engine dispatch in `ai_client.get_orchestrator()` over
> `AGENTIC_ENGINES`.

Status: **design locked** (decisions below), not yet implemented.
Scope: move the two privileged things the portal does today — **running the agentic loop + executing tools**, and **minting cloud credentials** — out of the internet-facing portal. The loop moves into the per-session **sandbox** (parity with how Claude Code already runs); minting moves to a dedicated **provisioning worker** with its own identity, NOTIFY-driven. The portal becomes a thin broker. As a capability rider, the sandboxed custom loop unlocks the **whole Bedrock vendor catalog** (Amazon, Meta, Mistral, Cohere, AI21, DeepSeek, Stability, Anthropic) via the Converse adapter.

## Why — asymmetry today

| Concern | Claude Code engine | Custom engine (`orchestrator*`) | Provisioning |
|---|---|---|---|
| Loop runs in | per-session sandbox (`inkwest_conv_<sid>`) | **inkwest backend process** | — |
| Tool execution | sandbox (in-CLI MCP) | **backend subprocess** (`execute_kubectl`) | — |
| Egress | squid domain allowlist | **free portal egress** | — |
| Credential minting | — | — | **portal** holds SSO OAuth app creds, calls STS (`_apply_aws_selection`) |

A portal compromise today ⇒ tool execution + model API + cloud-credential minting. The CLI engine already avoids most of this (it lives in the sandbox); the custom engine and the minting path do not.

Current usage note: the typical Inkwest deployment runs only the `claude-cli` engine in the sandbox; the custom loop is reached solely by enabling `inkwest-agent`. (The earlier in-portal SDK/Converse loop this plan set out to relocate has since been removed outright — there is no non-sandboxed inference path anymore.)

## Engine model

The choice isn't `AI_PROVIDER`, it's **which agentic loop**:

| Engine | Backend | Adapter | Models | Runs in |
|---|---|---|---|---|
| `claude-cli` (Claude Code) | Bedrock or Anthropic | native CLI | Claude only | sandbox |
| `inkwest-agent` (custom) | Bedrock | **Converse** | all vendors | sandbox |
| `opencode` | Bedrock | opencode CLI (`opencode run --format json`) | all vendors | sandbox |

> **opencode engine** (added after the original design): the [opencode](https://opencode.ai) terminal coding agent run non-interactively, parity with `claude-cli` — `orchestrator_opencode.py` spawns it in the conv container (`ai_cli_client.execute_opencode`), translates its NDJSON to `ChatEvent` (`parse_opencode_response`), and resumes via `--continue` over a durable per-conv data-dir bind-mount (`<sid>/opencode/` → `$HOME/.local/share/opencode`). It brings opencode's native bash/read/write/edit/grep tools, plus **MCP** (enabled servers materialized into `opencode.json`'s `mcp` block — static catalog + OAuth2 proxy/JWT, same inputs as the other engines, removed on turn end) and **skills** (bodies materialized to `.claude/skills`, indexed in `AGENTS.md` for on-demand read). On an **abnormal exit** (cancel / timeout / crash) the broker tears the conv container down (`sandbox.teardown(remove_dir=False)`) so opencode-spawned children are reaped; durable dirs survive and the next turn re-provisions. Enabling it (or `inkwest-agent`) opens the whole-Bedrock catalog (`engines.open_catalog_enabled()`). **LLM auth broker** (`llm_broker_enabled`): opencode is wired through it like the other engines — `opencode.json`'s `provider.amazon-bedrock.options.baseURL` points at `/api/llm-proxy` and the per-turn grant rides `AWS_BEARER_TOKEN_BEDROCK` injected via `docker exec -e`; with the broker on, `sandbox.env` drops the real key (per `sandbox.env.example` / `docs/llm-auth-broker.md`), so the only Bedrock token in the sandbox is this ephemeral grant — burned in the broker's `finally`. Off ⇒ direct Bedrock with the sandbox.env key (today's behavior). Operator prerequisites: **enable it via `AGENTIC_ENGINES`** (e.g. `AGENTIC_ENGINES=claude-cli,opencode`), rebuild the agent image (bakes the `opencode` binary), `models.dev` squid-allowlisted, `AWS_BEARER_TOKEN_BEDROCK` in `sandbox.env` (broker off) or the backend env (broker on) — with the broker on, `AWS_REGION` too. **Hardening/accounting** (see `orchestrator_opencode.py` / `agent/Dockerfile`): the binary is **version-pinned** (`OPENCODE_VERSION` build arg — the NDJSON parser is validated against that exact wire format; bump deliberately and re-capture `tests/fixtures/opencode_stream_*.jsonl`) with `autoupdate:false`; runs `--pure` + the per-turn `/workdir/.opencode` prune (blocks plugin/agent persistence dropped by a prompt-injected turn) and `--` before the prompt (no flag injection); the broker path **fail-closes** with a clear error when no registry model is enabled; usage counts fold Bedrock cache write/read into input tokens and pass opencode's authoritative per-turn `cost_usd` through to `record_usage` (zero/negative ⇒ token×rate fallback); `--continue` is guarded on the session store actually existing (missing `opencode.db` ⇒ fresh run, no silent context loss); discovered EKS contexts + granted connectors are surfaced in the prompt; failed `aws` calls smelling of expired creds raise the re-auth affordance (once per turn). With the broker on, the backend env must also carry `AWS_REGION` (checked by `validate_settings`). **Deferred for opencode**: ARN-pinned models (rejected with a clear error — needs an `opencode.json provider.models` mapping), registry `max_tokens` overrides (logged, not honored) and image attachments.

- Claude Code is constrained to Claude even when its backend is Bedrock — the limiter is the *client*, not Bedrock.
- Converse is the universal substrate for non-Claude Bedrock vendors; it also covers Claude-on-Bedrock. We do **not** build a Messages adapter (the only Anthropic-direct case is `claude-cli`, which uses its own loop).

### Engine selection — `AGENTIC_ENGINES`

- New env var **`AGENTIC_ENGINES`** (CSV) = engines enabled in this deployment. Default **`claude-cli`** (preserves current behavior exactly).
- `AI_PROVIDER` is kept for back-compat (backend/creds for the API path); its engine role is deprecated in **lot 2**.
- Engine is **first-class** (reserved `session.engine` column); a per-session engine picker in the UI is a **next step** (today: model-only UI).
- Catalog = **union of enabled engines' capabilities** (engine-aware):
  - `claude-cli` → Claude only (no non-Claude model offered — they'd be unusable via the CLI).
  - `claude-cli,inkwest-agent` → Claude + all Bedrock vendors.
  - `inkwest-agent` → all vendors (Claude via Converse too).
- Per-turn resolution: non-Claude → `inkwest-agent`; Claude → the single enabled engine, else default `claude-cli`.
- **Gate the catalog *fetch*, not just display**: non-Claude (open-weight) Bedrock models are loaded only when an engine that can run them is enabled (`inkwest-agent ∈ AGENTIC_ENGINES`). Under `claude-cli` only, do not fetch/surface them at all — pointless to load models the engine can't run, and they'd appear unusable in the picker. This supersedes the standalone `bedrock_include_open`/`include_open` flag.

## Portal ↔ Agent — service contract

Inkwest becomes an **agent host / control plane**; agents are pluggable **workers** behind a stable contract (LSP/ACP-shaped). Claude Code and `inkwest-agent` are interchangeable implementations.

| Role | Owns | Must NOT |
|---|---|---|
| **Inkwest** (host/broker) | identity, durable state (Postgres), policy + approval gates, UI/WS, lifecycle, credential projection, egress policy, budget | make agentic decisions · run the model loop · hold ambient cloud creds |
| **Agent** (worker, in sandbox) | the decision loop, local tool calls within the envelope, the event stream | own durable state · exceed the capability envelope · persist secrets |

- **Transport / lifecycle**: agent = a process the broker spawns in the sandbox. Turn-spec in (args/stdin); newline-delimited JSON `ChatEvent` stream out; exit code = terminal status. Reuses the `claude` spawn + `ai_cli_client` path. Ops host→agent: `start_turn` · `resume` · `cancel` (kill).
- **Turn-spec (host→agent)** *(design shape — the P1.4 sub-bullet below records what actually ships; notably `wallclock`→`wallclock_seconds`, no `schema`, and `resume`/"only creds" are reserved)*: `session_id`, `user_message`, `model`, `system_prompt`/refs, `tools[{name, schema, exec_profile}]`, `creds_path` (tmpfs — the only creds), `limits{max_iterations, max_tokens, budget_remaining, wallclock}`, `engine`, `context{mode: resume|replay, history?, resume_token?}`.
  - **Implemented (P1.4, `inkwest_agent.protocol.TurnSpec`, `PROTOCOL_VERSION = 2`)**: the full nested shape ships — `context{mode, history, resume_token}` (`replay` is the only live mode; the custom agent keeps no transcript yet, so `resume`/`resume_token` are reserved), `limits{max_iterations, max_tokens, max_conversation_messages, budget_remaining, wallclock_seconds}` (loop honors iterations/tokens/messages; budget/wallclock are broker-enforced ceilings — **reserved**, not yet enforced), `tools[{name, exec_profile}]` (Profile A only; the agent uses its own definitions, so `schema` is omitted), `creds_path`, `engine` (informational — the agent binary *is* the engine, nothing dispatches on it). Unknown keys at any level are rejected (`PROTOCOL_VERSION` gates forward-compat).
  - **Implemented (P1.7) — skills + MCP as Converse tools**: `PROTOCOL_VERSION` is now **3**. Three turn-spec fields added: `skills[{name, description}]` — the active skills as *progressive disclosure*: **only** the name + short description ride the wire (the always-visible half), the SKILL.md **bodies** stay on sandbox files under `skills_root` (the broker materializes them via `apply_session_skills`, parity with claude-cli) and are read on demand by a synthetic `read_skill` enum tool the loop adds (each short description is truncated in the always-visible tool desc to stay under Bedrock's toolSpec size limit); `skills_root` — the in-container dir holding those bodies; `mcp_config_path` — in-container path to a broker-materialized `.claude.json`-shaped MCP server-def file (creds/headers **projected one-way** like connector creds, **removed on turn end** + defensively pruned right after the next `sandbox.ensure` to catch a file orphaned by a SIGKILLed prior turn). A **lazy** in-sandbox MCP client (`inkwest_agent/mcp.py`) connects the enabled servers **concurrently** (per-server `AsyncExitStack` + timeouts), lists their tools, and merges them into the Converse toolConfig as `mcp__<server>__<tool>` (Claude Code parity — keeps cross-engine replayed history coherent; a colliding or base-tool-clashing name is skipped, not last-wins, since Converse rejects duplicate tool names; a timed-out/errored session is **evicted** so the rest of the turn fast-fails it). All of this is **Profile A** (the agent runs the tools, env-enforced). The `mcp` SDK is an **optional** dep: absent → no MCP tools merged, the loop still runs.
  - **Implemented (P1.8) — image attachments**: `PROTOCOL_VERSION` is now **4**. Two turn-spec fields added: `attachments[{path, content_type, filename, size}]` — files uploaded to the turn, already in the sandbox `/workdir` (only the in-container path + metadata ride the wire, not the bytes); and `input_modalities` — the resolved model's Bedrock input modalities (TEXT/IMAGE/…), handed in by the broker since the agent is DB-free. The Converse adapter reads each attachment and, **only for an IMAGE-capable model** (`input_modalities` contains `IMAGE`), turns a supported image (jpeg/png/gif/webp within a size cap) into a Converse image block on the user turn; non-image / oversized / unsupported / text-only-model attachments are skipped for image translation (the turn proceeds text-only, with a status note). Empty (the default) keeps pre-P1.8 text-only behavior.
  - **Implemented (LLM auth broker) — Bedrock endpoint override**: `PROTOCOL_VERSION` is now **5**. One turn-spec scalar added: `bedrock_endpoint_url` — when set, the agent's boto3 client targets the broker (`{url}/model/{id}/converse[-stream]`, bearer-grant auth, no SigV4) instead of real Bedrock, so no real Bedrock secret lives in the sandbox. None ⇒ direct Bedrock (today's default). The orchestrator omits unset/None scalars from the wire spec, so an older agent image still parses a broker-off turn (see `docs/llm-auth-broker.md`).
  - **Implemented — free-form exec tools**: `PROTOCOL_VERSION` is now **6**. One field added: `limits.bash_timeout_seconds` — the per-call timeout for the free-form `bash` exec tool (the agent is settings-free, so the broker carries its `claude_cli_timeout` here; None ⇒ the agent's own default). Still bounded by the broker's whole-turn wall-clock kill.
  - **Protocol-mismatch detection (broker-side)**: when the deployed agent image lags the broker, the agent's `parse_turn_spec` rejects any broker field it doesn't know (an unknown field can only mean a stale binary — both sides build the spec from the same source). The agent stamps its own `(agent protocol vN)` into that `TurnSpecError`, and the broker (`orchestrator_inkwest_agent._rewrite_protocol_mismatch`) rewrites the opaque `unknown field(s): …` text into an actionable error naming **both** protocol versions + the offending field(s). The cross-component contract is a single shared constant (`inkwest_agent.protocol.UNKNOWN_FIELDS_MARKER`); a round-trip test guards it. **Remedy** (env-specific, from `app_env`): **dev** → rebuild the local sandbox image (`backend/Dockerfile.agent`); **prod** → the image ships via CI, so redeploy with a matching image (no manual rebuild). The raw rejection is preserved in the error event's `metadata.original_error`.
  - **Carve-out**: `import inkwest_agent` pulls **no** sqlalchemy / fastapi / asyncpg — the DB-free tool loop + transcript truncation were carved out of `orchestrator_common`/`messages` into `tool_execution.py` + `message_window.py`, and `app/services/__init__` defers its heavy re-exports (PEP 562). The sandbox image (`backend/Dockerfile.agent`) installs only the carved runtime deps (`requirements-agent.txt`: boto3, httpx, pydantic, pydantic-settings, structlog, mcp) — ~7 instead of ~25, no DB engine on startup. Import + runtime guards (`tests/test_agent_{import,runtime,lean_deps_e2e}_guard`) lock it.
  - **Creds delivery (P1.4) is CLI-aligned, one-way**: the broker *pre-provides* per-session creds at spawn — `build_connector_env` renews-on-read and projects the AWS profiles onto the tmpfs at `creds_path`, then the env (AWS_* file paths, `GITLAB_TOKEN`, `KUBECONFIG`) is injected via value-less `docker exec -e` (values out of argv). The agent's executors read creds from that env (`ToolExecContext.projected_creds`), never the portal credential store — the agent never mints, renews, or calls back. The pure "`creds_path` is the only creds" + short-lived crypto handoff is the **Phase 2** target (GitLab's token rides the env today, like the CLI).
- **Event stream (agent→host)**: the existing `ChatEvent` set, standardized + versioned (`text_delta`, `thinking`, `tool_start`, `tool_result`, `tool_proposal`, `confirm_request`, `status_update`, `budget_warning`, `connector_required`, `done`(usage), `error`). The broker persists from these → durable source of truth.
- **Tool-execution boundary** (per-tool `exec_profile`):
  - **Profile A — agent executes** (default; today's read-only toolset): agent runs the tool locally, emits `tool_start`/`tool_result`; policy enforced by the *environment*.
  - **Profile B — broker-mediated**: agent emits `tool_proposal`; broker validates / human-approves / executes / returns. Reserved for a later **RW tools lot**.
- **Capability envelope** (enforced by the host, never trusted to the agent): squid network allowlist · per-session scoped creds on tmpfs only · ephemeral fs · iteration/token/wallclock/budget limits (broker kills on breach) · per-session identity that dies with the sandbox.
  - **Crux**: guarantees come from the *environment* (sandbox isolation + scoped projected identity + egress allowlist + broker policy gate), not from trusting the agent's code — which is what makes agents safely pluggable.
  - **Model egress (P1.5)**: `inkwest-agent`'s Bedrock Converse calls egress through the **same** per-conversation squid allowlist as `claude-cli` — boto3 honors the container's `HTTPS_PROXY` and the `--internal` network leaves no other route, so the proxy is the only path out. Bedrock/STS endpoints are already allowlisted (`sandbox/squid.conf`); `tests/test_egress.py` locks the policy (Bedrock/Anthropic allowed, path-style S3 + unknown hosts denied, fail-closed). **Accepted gaps, deferred to a later custom-squid-rules stream**: the allowlist keeps a broad `.amazonaws.com` allow (any non-S3 AWS service is reachable), and the S3 deny only anchors *path-style* — *virtual-hosted* `<bucket>.s3...` still leaks under the broad allow. Both are pinned by tests so the tightening is a deliberate, visible change.
    - **MCP egress (P1.7)**: the in-sandbox MCP client egresses through this **same** squid allowlist (it inherits `HTTPS_PROXY`; the `--internal` network leaves no other route), so a remote MCP server must be allowlisted to be reachable — onboarding a new one is a deliberate `squid.conf` edit (locked by `tests/test_egress.py`, which denies an off-list MCP host). **Coralogix is the only onboarded remote MCP server today** (`api.<region>.coralogix.com`, already covered by the existing `.coralogix.com` allow), so **no new host was needed** for P1.7. **Caveat**: a *stdio* MCP server runs an **in-sandbox subprocess** that does **not** ride squid (it's a local process, not network egress) — that's an operator-trust surface, but only **HTTP** MCP servers are onboarded today; a stdio server would only run if an operator added one to `MCP_CATALOG`.
- **State / resume**: broker = durable truth (Postgres). **`claude-cli`** keeps its own working transcript in the durable per-conv dir (`.claude`-style `--resume`). **`inkwest-agent`** keeps *no* transcript today — it runs in `context.mode=replay`: the broker seeds the full history every turn and persists from the event stream, so a crashed agent simply replays. Agent-side resume (`mode=resume`/`resume_token`) is reserved for a later lot.

### Adapter boundary (multi-host future)

The loop is host-agnostic; only the **adapter** is host-specific (format translation, tool-use protocol, streaming→`ChatEvent`, auth). Converse now; **OpenAI / Vertex / Ollama / LiteLLM** are additive adapters later. Rule: `ChatEvent` and the internal tool blocks stay **canonical** — no Converse-ism leaks into the loop or contract.

## Locked decisions

| # | Decision | Verdict |
|---|---|---|
| 1 | Custom-engine adapter | **Converse-only**. No Messages adapter (Anthropic-direct only ever via `claude-cli`). |
| 2 | Where the loop runs | Custom loop **moves into the sandbox** (Phase 1). One loop (shared DB-free tool execution carved into `tool_execution` + `message_window`) + Converse adapter, packaged as `inkwest-agent`. Portal = broker. |
| 3 | History / state | **Resume by default** (agent transcript in durable per-conv dir); broker persists events → DB canonical. Turn-spec reserves `context{mode:resume\|replay, history?}`. Agent: zero DB access. |
| 4 | Tool execution | **Profile A** (agent-exec, env-enforced) default for the read-only toolset. **Profile B** (broker-mediated) reserved per-tool for a later **RW lot**. `exec_profile` per-tool in the contract from day 1. |
| 5 | Transport / cancel | **NDJSON `ChatEvent` on stdout** + **cancel-by-kill**. Reuse `ai_cli_client`. One spawn/stream/cancel mechanism across the sandboxed engines (opencode later joined the CLI + inkwest-agent on it). |
| 6 | Packaging | Python package `inkwest_agent` (loop + Converse adapter + `app/tools/*` executors + validation) **+ the `mcp` optional extra** (P1.7 — the in-sandbox MCP client; the toolbox-image build must install it so MCP tools are available, else the loop degrades to no-MCP) **baked into the toolbox image** (python+boto3 already present). No standalone binary. **Realized lean:** `backend/Dockerfile.agent` (`FROM` toolbox) COPYs the code and installs only `requirements-agent.txt` (~7 deps) — not the full backend — and runs it via a `python -m inkwest_agent` launcher; no DB/web stack, no inkwest-backend dep metadata (keeps `uv pip check` clean). |
| 7 | Engine selection | **`AGENTIC_ENGINES`** CSV; default `claude-cli`. `AI_PROVIDER` kept (engine role deprecated lot 2). Engine first-class (`session.engine` reserved), UI picker next step. Per-turn resolution (see Engine model). |
| 8 | Replicas | **Single-replica** now. HA / sharding = **lot 2**. |
| 9 | Sequencing + cred handoff | Current lot = **Phase 0 + 1**. **Cred isolation = next lot** (gated). **Revised direction:** the **SSO user device-auth + runtime account/role selection is the keystone — kept** (rules out static IAM/IRSA roles, which would break the user's runtime choice). Minting **centralizes in the portal/broker** (the broker also brokers creds) with a **short-lived crypto handoff** to the sandbox — *not* a per-node DaemonSet holding the SSO app secret (that spreads the secret to every node for ~zero gain mono-node). A DaemonSet, if used, narrows to a **local cred injector** (node-local tmpfs). **Mono-node end-to-end first**, before any horizontal scale. |

### Conservative defaults ⇒ zero runtime impact

With `AGENTIC_ENGINES=claude-cli`, include-non-anthropic **off**, and an **engine-aware catalog** (`claude-cli` ⇒ Claude-only, no unusable non-Claude model in the picker), nothing changes until an admin opts in. Under `claude-cli` the custom loop is never on the chat path. Caveat: `AI_PROVIDER=anthropic`/`bedrock` deployments already run the custom loop in-portal; Phase 1 relocates it (intended, not retroactive).

## Phasing

- **Phase 0 — reframe + engine first-class** (small, conservative defaults): rename `open-weight`/`bedrock-open`/`include_open` → per-vendor tags + neutral framing; `AGENTIC_ENGINES`; engine-aware catalog; `session.engine` column reserved. Ships on the current in-process loop.
- **Phase 1 — sandboxed `inkwest-agent` runtime** (largest; the loop-isolation core): `inkwest-agent` entrypoint + toolbox deps; stdout `ChatEvent` protocol; broker spawn/stream/persist/cancel (reuse sandbox lifecycle); move `save`/`finalize` to broker side; feature-flag per engine, then retire the in-process loop.
- **Phase 2 — credential isolation** (next lot, gated; NOTIFY-driven): intent table + `pg_notify`; **minting stays centralized in the portal/broker** (which becomes the cred broker) — keep the **SSO user device-auth + runtime account/role selection** (the security keystone; rules out static IAM/IRSA roles); hand creds to the sandbox via a **short-lived crypto handoff**, projected onto node-local tmpfs by a **local injector** (a DaemonSet in that narrowed role only — *not* holding the SSO secret). **Mono-node end-to-end first.** (Superseded: the earlier "per-node DaemonSet holds the SSO secret + the worker mints" framing.)

## Implementation slices

Implementation slices:

| Ticket | Slice | |
|---|---|---|
| — | P0.1 | `AGENTIC_ENGINES` + per-turn engine resolution |
| — | P0.2 | Whole-Bedrock engine-aware catalog + per-vendor tags |
| — | P1.1 | `inkwest_agent` package (loop + Converse + tools) standalone |
| — | P1.2 | Bake `inkwest_agent` into the toolbox image |
| — | P1.3 | Broker: spawn-in-sandbox + parse + persist + cancel |
| — | P1.4 | Turn-spec contract: context (resume/replay) + creds + tools |
| — | P1.5 | Squid egress allowlist for model endpoints |
| — | P1.6 | Retire the in-process custom loop |
| — | P2.1 | Provisioning intent table + `pg_notify` |
| — | P2.2 | Provisioning worker (DaemonSet) skeleton + LISTEN |
| — | P2.3 | Move device-auth + STS mint into the worker |
| — | P2.4 | Credential projection to node-local tmpfs |
| — | P2.5 | Strip SSO/STS creds from the portal |
| — | P2.6 | Worker infra: DaemonSet manifest, IAM/SA, NetworkPolicy |

> **Note:** the P2.x rows predate the revised Phase 2 direction (decision #9). Minting stays in the **portal/broker** (not moved into a worker), and the DaemonSet narrows to a **local cred injector**; the SSO user device-auth + runtime role selection is kept. The ticket descriptions (esp. P2.3 "move STS mint into the worker", P2.6 "DaemonSet/IAM/NetworkPolicy") are to be reframed at Phase 2 build.

## Open decisions (deferred)

- **#2 worker→sandbox cred handoff** (Phase 2): **resolved → central portal/broker minting + short-lived crypto handoff** to the sandbox (the DaemonSet-holds-the-SSO-secret option spread the secret to every node for ~zero gain mono-node). A DaemonSet, if any, is only the **local injector** onto node-local tmpfs. **Mono-node first**; cross-node/HA revisited in a later lot.
- RW tools + broker-mediated validation (Profile B): later lot.
- HA / sharding (multi-replica, shared state): lot 2.
- Dedicated agent image distinct from the Claude Code toolbox: later option.

## Security payoff

After this lands, a portal compromise yields neither tool execution, nor model access, nor cloud-credential minting — the three capabilities each sit behind a separate, narrower identity (sandbox per-session identity; worker SSO/STS identity).

## Reuse — what's already in place

- Sandbox lifecycle: `app/services/sandbox.py` (`ensure`, reaper, squid proxy, tmpfs `~/.aws`/`~/.kube`, durable per-conv `claude/` + `workdir/` + `logs/squid/`).
- In-sandbox engine pattern: `orchestrator_cli.py` + `ai_cli_client.py` (spawn-in-container, stdout→`ChatEvent`, cancel-by-kill, `--resume`).
- The custom loop: `tool_execution.py` (DB-free tool-call loop, shared) / `message_window.py` (pure transcript truncation) / `orchestrator_common.py` (`finalize_turn`, DB) / `orchestrator.py` / `orchestrator_converse.py` (Converse already unifies Bedrock vendors). The DB-free pieces were carved out of `orchestrator_common`/`messages` so `inkwest_agent` imports them without the DB/web stack.
- NOTIFY infra: `pubsub.py` (channel, listener loop, fan-out) — reusable by the worker.
- Durable connector tables: `connector_pending_state`, `connector_audit` — extend for the intent queue.
