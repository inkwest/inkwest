# Security Hardening

TL;DR of the security controls added before release and the **deploy-time settings an
operator must set** — several controls can't live in app code (ingress, uvicorn flags).

Scope note: Inkwest targets **trusted single-organisation deployments**. Cost/DoS controls
(rate limiting, budgets) are therefore **available but OFF by default** — enable them if the
tool is ever exposed more broadly. The auth/egress/input hardening below is always on.

## Must-set before exposing publicly

| Setting | Why |
|---|---|
| `OIDC_ENABLED=true`, `APP_ENV=production`, a real `SECRET_KEY` | The app now refuses to boot otherwise (`validate_settings` runs at startup). With OIDC off, every request is an unauthenticated admin. |
| `TRUSTED_HOSTS=<your-host>[,<host2>]` | Enables Host-header validation (`TrustedHostMiddleware`). Off by default so k8s/ALB probes (Host = pod IP) aren't rejected — set it to your public hostname(s). |
| Ingress: **deny `/api/llm-proxy` and `/api/mcp-proxy` from the public listener** | These are authenticated only by the per-turn broker grant / internal JWT and are reached by the sandbox via the host-gateway alias — they never need public exposure. The app can't enforce this; do it at the ALB/ingress. |
| uvicorn `--proxy-headers` + `--forwarded-allow-ips=<ALB subnet>` and `--ws-max-size=<bytes>` | Correct client IP for rate limiting (or rely on `RATE_LIMIT_TRUSTED_PROXY_HOPS`), and a transport-level WS frame cap complementing `MAX_MESSAGE_CHARS`. |
| Cluster read roles must **exclude Secret read** | kubectl `secret(s)` reads are now denied in-app regardless, but least privilege belongs in the granted role too. |

## Controls added in code (defaults safe)

- **Startup validation** (`validate_settings`): prod `SECRET_KEY` non-default, OIDC required in prod.
- **Rate limiting** (`RATE_LIMIT_*`, **off by default**): per-IP HTTP (stricter on `/api/auth`) + per-user WS turn cap. Set `RATE_LIMIT_ENABLED=true` to turn on. In-process (single-replica); pair with an ingress/WAF limit if exposed.
- **Budgets** (`BUDGET_ENABLED`, **off by default**): set `BUDGET_ENABLED=true` + `DEFAULT_MONTHLY_COST_LIMIT_USD=<n>` for a server-side LLM spend ceiling. When on, the chat gate fails **closed** on a check error. Tunable at runtime via the admin UI.
- **Sandbox egress** (`sandbox/squid.conf`): S3 denied in all addressing styles; AWS allow is per-service/per-region (eu-west-1 + global/us-east-1) not blanket `.amazonaws.com`. EKS is region-agnostic. Add other regions via a `/conf.d/*.conf` fragment, e.g.
  `acl allowed_domains_re dstdom_regex ^[a-z0-9-]+\.us-west-2\.amazonaws\.com$`
- **Attachment path confinement**: both engines validate client-supplied filenames (`resolve_existing`) before referencing them — no `../` escape.
- **aws_call**: rejects `--endpoint-url`/`--no-verify-ssl`/`--profile`/`--debug` and `file://` values.
- **Sharing**: minting an invite or a public link requires the *real* owner (admins are read-only inspectors; revoke/list stay admin-capable for break-glass).
- **OIDC login CSRF**: `state` is bound to a short-lived HttpOnly cookie and verified at callback.
- **HTTP hardening**: `/docs`,`/redoc`,`/openapi.json` disabled in production; HSTS in production; CSP tightened (`connect-src 'self'`, `frame-ancestors 'none'`); readiness probe returns a static error.
- **Connection/size caps**: `MAX_MESSAGE_CHARS`, `MAX_WS_CONNECTIONS_PER_USER`, `MAX_WS_CONNECTIONS_PER_SHARE`, bounded attachment upload reads.

## Known residual / follow-ups

- Rate limiter and budget/grant stores are **in-process** — single-replica only (`WEB_CONCURRENCY>1` fail-fasts). Multi-node needs a shared store + ingress/WAF limits.
- A retried non-idempotent request (behind the frontend's retry logic) can rarely double-apply — durable fix is server-side idempotency keys on the AWS mint/grant routes.
