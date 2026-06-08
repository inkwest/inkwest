# Claude CLI Provider Setup

The `claude-cli` provider runs Claude Code inside the backend container, using your Claude Max/Pro subscription instead of API keys. The container provides isolation — Claude CLI should **not** be run unattended on the host machine.

## Security considerations

- **Keep Claude CLI inside Docker.** The `--dangerously-skip-permissions` flag disables all confirmation prompts. Container isolation limits blast radius — running this on the host gives Claude unrestricted access to your filesystem, shell, and credentials.
- **Treat `app_home/` as sensitive.** The `.claude.json` file and `.claude/` directory contain your OAuth session tokens, account metadata, and conversation history. Do not commit them (they are gitignored), do not share them, and restrict file permissions.
- **Kubeconfigs are mounted read-only** (`:ro`), but the Claude session tokens are read-write. Back up `app_home/` if needed and keep it out of backups that go to untrusted storage.

## How it works

The docker-compose backend service mounts two paths from the host into the container's `app` user home:

```yaml
volumes:
  - ./app_home/.claude:/home/app/.claude      # session data, conversation history
  - ./app_home/.claude.json:/home/app/.claude.json  # OAuth tokens, account config
```

These persist the Claude CLI authentication across container restarts. The CLI binary is installed in the Dockerfile as the `app` user (UID 1000) into `/home/app/.local/bin/`.

## Initial setup

### 1. Mount targets (automatic)

The backend bind-mounts `app_home/.claude` (a **directory**) and `app_home/.claude.json` (a **file**) into the `app` user's home. The one-shot `claude-init` service creates both with the correct type and ownership (UID 1000) on every `docker compose up`, so there is **nothing to set up by hand**.

> Why a dedicated step: Docker auto-creates a missing bind source as a **root-owned directory** — harmless for `.claude`, but for `.claude.json` it puts a *directory* where the CLI expects a *file* (`EISDIR`). `claude-init` seeds and repairs them before the backend's mounts resolve, so this can't happen.

### 2. Start the stack

```bash
docker compose up -d
```

### 3. Enter the container and authenticate

```bash
docker compose exec -u app backend bash
```

Inside the container, run the interactive login:

```bash
claude
```

This opens an OAuth flow — follow the URL, authenticate in your browser, and paste the code back. Once authenticated, the session is saved into the mounted `app_home/` directory and persists across restarts.

Verify the CLI works:

```bash
claude --version
claude -p "say hello"
```

Exit the container:

```bash
exit
```

### 4. Configure the provider

Set `AI_PROVIDER=claude-cli` in your `.env` or docker-compose environment (this is the default).

The backend will invoke `claude` with `--dangerously-skip-permissions` for non-interactive tool execution. This is acceptable because the container provides the isolation boundary.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `.claude.json` is a directory / CLI `EISDIR` reading config | Re-run `docker compose up -d` — `claude-init` removes the stray dir and recreates the file (add `--force-recreate backend` if the backend was already running). |
| `Permission denied` on `.claude.json` | Re-run `docker compose up -d` — `claude-init` re-applies UID 1000 ownership. |
| `claude: command not found` | Rebuild the image: `docker compose build backend` |
| OAuth token expired | Re-enter the container and run `claude` again |
| Empty responses | Check `docker compose logs backend` for CLI stderr output |
