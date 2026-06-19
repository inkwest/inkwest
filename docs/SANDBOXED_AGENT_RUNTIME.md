# Sandboxed Agent Runtime + Provisioning Worker — Plan

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

Current usage note: with `AI_PROVIDER=claude-cli` (the typical Inkwest deployment, CLI pointed at Bedrock) the custom loop is **dormant** — the CLI engine in the sandbox handles everything. A deployment on `AI_PROVIDER=anthropic`/`bedrock` (the repo's `docker-compose.prod.yml` default) already runs the custom loop **in-portal** today; Phase 1 moves it.

## Engine model

The choice isn't `AI_PROVIDER`, it's **which agentic loop**:

| Engine | Backend | Adapter | Models | Runs in |
|---|---|---|---|---|
| `claude-cli` (Claude Code) | Bedrock or Anthropic | native CLI | Claude only | sandbox |
| `inkwest-agent` (custom) | Bedrock | **Converse** | all vendors | sandbox |

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
  - **Creds delivery (P1.4) is CLI-aligned, one-way**: the broker *pre-provides* per-session creds at spawn — `build_connector_env` renews-on-read and projects the AWS profiles onto the tmpfs at `creds_path`, then the env (AWS_* file paths, `GITLAB_TOKEN`, `KUBECONFIG`) is injected via value-less `docker exec -e` (values out of argv). The agent's executors read creds from that env (`ToolExecContext.projected_creds`), never the portal credential store — the agent never mints, renews, or calls back. The pure "`creds_path` is the only creds" + short-lived crypto handoff is the **Phase 2** target (GitLab's token rides the env today, like the CLI).
- **Event stream (agent→host)**: the existing `ChatEvent` set, standardized + versioned (`text_delta`, `thinking`, `tool_start`, `tool_result`, `tool_proposal`, `confirm_request`, `status_update`, `budget_warning`, `connector_required`, `done`(usage), `error`). The broker persists from these → durable source of truth.
- **Tool-execution boundary** (per-tool `exec_profile`):
  - **Profile A — agent executes** (default; today's read-only toolset): agent runs the tool locally, emits `tool_start`/`tool_result`; policy enforced by the *environment*.
  - **Profile B — broker-mediated**: agent emits `tool_proposal`; broker validates / human-approves / executes / returns. Reserved for a later **RW tools lot**.
- **Capability envelope** (enforced by the host, never trusted to the agent): squid network allowlist · per-session scoped creds on tmpfs only · ephemeral fs · iteration/token/wallclock/budget limits (broker kills on breach) · per-session identity that dies with the sandbox.
  - **Crux**: guarantees come from the *environment* (sandbox isolation + scoped projected identity + egress allowlist + broker policy gate), not from trusting the agent's code — which is what makes agents safely pluggable.
- **State / resume**: broker = durable truth (Postgres). **`claude-cli`** keeps its own working transcript in the durable per-conv dir (`.claude`-style `--resume`). **`inkwest-agent`** keeps *no* transcript today — it runs in `context.mode=replay`: the broker seeds the full history every turn and persists from the event stream, so a crashed agent simply replays. Agent-side resume (`mode=resume`/`resume_token`) is reserved for a later lot.

### Adapter boundary (multi-host future)

The loop is host-agnostic; only the **adapter** is host-specific (format translation, tool-use protocol, streaming→`ChatEvent`, auth). Converse now; **OpenAI / Vertex / Ollama / LiteLLM** are additive adapters later. Rule: `ChatEvent` and the internal tool blocks stay **canonical** — no Converse-ism leaks into the loop or contract.

## Locked decisions

| # | Decision | Verdict |
|---|---|---|
| 1 | Custom-engine adapter | **Converse-only**. No Messages adapter (Anthropic-direct only ever via `claude-cli`). |
| 2 | Where the loop runs | Custom loop **moves into the sandbox** (Phase 1). One loop (`orchestrator_common`) + Converse adapter, packaged as `inkwest-agent`. Portal = broker. |
| 3 | History / state | **Resume by default** (agent transcript in durable per-conv dir); broker persists events → DB canonical. Turn-spec reserves `context{mode:resume\|replay, history?}`. Agent: zero DB access. |
| 4 | Tool execution | **Profile A** (agent-exec, env-enforced) default for the read-only toolset. **Profile B** (broker-mediated) reserved per-tool for a later **RW lot**. `exec_profile` per-tool in the contract from day 1. |
| 5 | Transport / cancel | **NDJSON `ChatEvent` on stdout** + **cancel-by-kill**. Reuse `ai_cli_client`. One spawn/stream/cancel mechanism for both engines. |
| 6 | Packaging | Python package `inkwest_agent` (loop + Converse adapter + `app/tools/*` executors + validation) **baked into the toolbox image** (python+boto3 already present). Dedicated image (`sandbox_agent_image`) = later option. No standalone binary. |
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
- The custom loop: `orchestrator_common.py` / `orchestrator.py` / `orchestrator_converse.py` (Converse already unifies Bedrock vendors).
- NOTIFY infra: `pubsub.py` (channel, listener loop, fan-out) — reusable by the worker.
- Durable connector tables: `connector_pending_state`, `connector_audit` — extend for the intent queue.
