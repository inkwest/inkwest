# MCP Servers (per-conversation)

> users enable a per-conversation set of MCP servers from a
> server-side **catalog**. The selection is merged into the conversation's
> `.claude.json` so the `claude-cli` agent picks the servers up. Coralogix is the
> first integration onboarded — one entry per environment (`Coralogix-<env>`:
> `coralogix-dev` / `coralogix-staging` / `coralogix-prod`).

## TL;DR

- A **catalog** of available MCP servers is defined by one env var, `MCP_CATALOG`
  (server-side). Only the **keys** ever reach the browser — never the definitions
  or any secret they embed.
- Each conversation stores its own list of enabled keys
  (`sessions.enabled_mcp_servers`).
- On every turn (and on every change), the enabled servers are merged into the
  conversation's `.claude.json` under `mcpServers`. Claude Code reads MCP config
  **at process start**, so a change takes effect on the **next message**.
- UI: an **MCP** button on the connector bar (green `N/total` when ≥1 enabled)
  opens a modal with a checkbox per server.
- Requires **sandbox mode** (`SANDBOX_ENABLED=true`) — that's what gives each
  conversation its own `.claude.json`. In non-sandbox mode the config is shared,
  so the merge is a no-op.

## The catalog (`MCP_CATALOG`)

A JSON object mapping a stable key to a **base64-encoded** JSON definition — the
value that goes under Claude's `mcpServers[<key>]` (command/args/env/url…):

```
MCP_CATALOG={"coralogix-prod": "<base64(server-def-json)>", "fetch": "<base64(...)>"}
```

Encode it locally (so secrets never leave your machine). Put your real
definitions in a local, gitignored file and run:

```bash
python3 -c '
import json, base64
servers = json.load(open("mcp_servers.json"))  # {key: {command, args, env, ...}}
print("MCP_CATALOG=" + json.dumps(
    {k: base64.b64encode(json.dumps(v).encode()).decode() for k, v in servers.items()}))
'
```

Paste the printed `MCP_CATALOG=…` into `backend/.env` and restart the backend.
Parsing is tolerant: a malformed catalog or a single bad entry is logged and
skipped — it never crashes startup, it just yields fewer selectable servers
(`app/services/mcp_catalog.py`).

## How a selection is applied

1. The user toggles servers in the modal → `PUT /api/sessions/{id}/mcp`.
2. Keys are validated against the catalog (no arbitrary server injection) and
   persisted to `sessions.enabled_mcp_servers`.
3. `app/services/mcp_config.py::apply_session_mcp_selection` reconciles the
   conversation's `.claude.json` `mcpServers`:
   - **adds/updates** the enabled catalog entries,
   - **prunes** catalog-managed entries that were deselected,
   - **preserves** any `mcpServers` entry not owned by the catalog.
4. The same reconcile runs each turn after `sandbox.ensure`, so a sandbox rebuild
   (which recreates an empty claude dir) re-applies the selection.

`.claude.json` lives at `<session_dir>/claude/.claude.json` on the host
(bind-mounted to `CLAUDE_CONFIG_DIR` in the toolbox).

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/mcp/catalog` | Available keys (no definitions, no secrets) |
| GET | `/api/sessions/{id}/mcp` | Keys enabled for the conversation |
| PUT | `/api/sessions/{id}/mcp` | Set enabled keys (validated vs catalog) |

## Secrets

The catalog value carries the **full** server definition, **including any secret**
in its `env` (decision: option A — written in clear into the conversation's
`.claude.json` on enable). This is acceptable because that file lives in the
**per-conversation isolated** sandbox dir and is never served to the browser nor
put in the prompt/logs. Mitigations: use read-only credentials, the secret is
removed from the file on deselection / session teardown, and `MCP_CATALOG` stays
server-side. If clear-text-at-rest is not acceptable for a given server, switch
that entry to an env reference (`${VAR}`) and inject the secret into the toolbox
container environment instead.

## Code map

| Concern | Location |
|---|---|
| Catalog parse/validate | `backend/app/services/mcp_catalog.py` |
| Merge into `.claude.json` | `backend/app/services/mcp_config.py` |
| Per-turn apply | `backend/app/services/orchestrator_cli.py` (after `sandbox.ensure`) |
| Selection storage | `sessions.enabled_mcp_servers` (JSONB) |
| API | `backend/app/api/routes/mcp.py`, `backend/app/api/routes/sessions.py` |
| Config | `MCP_CATALOG` in `backend/app/core/config.py` |
| UI | `frontend/src/components/connector/McpPanel.tsx` |
