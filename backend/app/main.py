"""
Inkwest Backend - FastAPI Application
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import FileResponse, Response
from starlette.types import Scope

from app.api.routes import (
    admin,
    auth,
    chat,
    clusters,
    config,
    connectors,
    egress,
    health,
    llm_proxy,
    mcp,
    mcp_proxy,
    me,
    metrics,
    sessions,
    sharing,
    skills,
    sync,
    system_prompts,
    usage,
)
from app.core.config import get_settings, validate_settings
from app.core.database import async_session_maker, close_db, init_db
from app.core.logging import AccessLogMiddleware, access_log_config, get_logger, setup_logging
from app.core.metrics_server import start_metrics_server, stop_metrics_server
from app.core.rate_limit import RateLimitMiddleware
from app.core.telemetry import shutdown_telemetry
from app.services import web_push
from app.services.app_settings import load_settings
from app.services.auth_cleanup import start_state_cleanup, stop_state_cleanup
from app.services.connector_cleanup import start_connector_cleanup, stop_connector_cleanup
from app.services.mcp_oauth_catalog import load_oauth_catalog, oauth_catalog_keys
from app.services.messages import purge_empty_sessions, reconcile_interrupted_turns
from app.services.model_catalog import refresh_catalog
from app.services.model_registry import load_registry
from app.services.prompts import ensure_default_system_prompt
from app.services.pubsub import start_listener, stop_listener
from app.services.sandbox import reap_orphans, start_sandbox_reaper, stop_sandbox_reaper
from app.services.session_skills import ensure_skills_frontmatter

# Set up structured logging
setup_logging()
logger = get_logger(__name__)

# Get settings
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan management."""
    # Startup
    logger.info(
        "starting_api",
        version=settings.version,
        commit_hash=settings.commit_hash,
        build_timestamp=settings.build_timestamp,
    )
    # Fail fast on an unsafe/incomplete configuration (prod SECRET_KEY, OIDC, engines,
    # connectors, role-filter regex) so a misconfigured deploy crash-loops instead of
    # booting green — e.g. with the default secret or dev auto-login admin exposed.
    validate_settings()
    # Start the metrics exporter first so the process is scrapeable even while the slow
    # startup steps below run. Best-effort: a bind clash (e.g. a second uvicorn worker
    # racing the same port) must degrade metrics, not crash the API.
    if settings.metrics_enabled:
        try:
            start_metrics_server(settings.metrics_port)
        except OSError:
            logger.exception("metrics_server_start_failed", port=settings.metrics_port)
    await init_db()
    logger.info("database_initialized")
    # Seed the hardcoded system prompt as the DB default on first startup.
    async with async_session_maker() as db:
        await ensure_default_system_prompt(db)
        logger.info("default_system_prompt_ensured")
        # Migrate any legacy skill (content without a frontmatter) to the SKILL.md format.
        # Never block startup on it: a failure degrades the skills feature, not the whole
        # app. Roll back so the shared session is usable for the steps below.
        try:
            await ensure_skills_frontmatter(db)
            logger.info("skills_frontmatter_ensured")
        except Exception:
            logger.exception("skills_frontmatter_migration_failed")
            await db.rollback()
        # Model config load is best-effort against DB failures: a Postgres blip
        # here must degrade the model picker (empty registry -> "offer everything
        # available"), not take down the whole API at boot. Only DB-layer errors
        # are swallowed — a programming error must still crash-loop visibly rather
        # than boot green with a silently-empty registry. refresh_catalog self-guards.
        try:
            # Scalar feature flags (e.g. bedrock_include_open) before the fetch reads them.
            await load_settings(db)
            await refresh_catalog()  # live provider catalog (best-effort, static fallback)
            await load_registry(db)  # admin registry: enabled subset, default, overrides
            await load_oauth_catalog(db)  # OAuth2 MCP catalog (seeds from env on first run)
        except SQLAlchemyError:
            logger.exception("model_config_load_failed")
            await db.rollback()
        # Recover turns killed mid-flight by the previous shutdown (restart,
        # rolling deploy, crash): no turn is in flight yet, so any session ending
        # on an unanswered user message is an interrupted turn — mark it retryable.
        try:
            reconciled = await reconcile_interrupted_turns(db)
            if reconciled:
                logger.info("reconciled_interrupted_turns", count=reconciled)
        except Exception:
            logger.exception("reconcile_interrupted_turns_failed")
        # Abandoned empty sessions (opened, configured, never messaged) are
        # invisible in the UI — sweep the stale ones so they don't accumulate.
        try:
            purged = await purge_empty_sessions(db)
            if purged:
                logger.info("purged_empty_sessions", count=purged)
        except Exception:
            logger.exception("purge_empty_sessions_failed")
    await start_listener()
    logger.info("pubsub_listener_started")
    if settings.oidc_enabled:
        await start_state_cleanup()
    # OAuth2 MCP entries reach the CLI only via the per-conversation .claude.json, which
    # exists only in sandbox mode. Warn loudly rather than fail: the rest of the app is fine.
    oauth_keys = oauth_catalog_keys()
    # The connector cleanup loop also sweeps MCP OAuth pending states, so run it whenever
    # either subsystem is active (not only when connectors are enabled).
    if settings.connectors_enabled or oauth_keys:
        await start_connector_cleanup()
    if settings.sandbox_enabled:
        # Clear container/network orphans left by a prior (crashed) backend, then run
        # the idle reaper. Durable per-conversation dirs are kept so turns can resume.
        await reap_orphans()
        start_sandbox_reaper()
    if oauth_keys and not settings.sandbox_enabled:
        logger.warning("mcp_oauth_catalog_present_but_sandbox_disabled", keys=len(oauth_keys))
    elif oauth_keys:
        # OAuth2 MCP holds the per-user DEK and the in-flight token in THIS process's RAM
        # (browser-fractioned custody, see app.services.mcp_dek_store). A second worker/replica
        # would handle the OAuth callback without the DEK and brick in-flight connects. Fail
        # fast on the one multi-process case we can detect here (uvicorn/gunicorn workers);
        # cross-host replicas can't be detected from app code and need session affinity (the
        # constraint is shared with the connector RAM stores — see docs/MCP.md).
        workers = int(os.environ.get("WEB_CONCURRENCY", "1") or "1")
        if workers > 1:
            raise RuntimeError(
                f"OAuth2 MCP requires a single worker (WEB_CONCURRENCY={workers}); the per-user "
                "key and in-flight token live in process RAM. Run single-worker with session "
                "affinity, or disable MCP_CATALOG_OAUTH2."
            )
        logger.info("mcp_oauth_catalog_loaded", keys=len(oauth_keys))
    # The LLM auth broker's per-turn grant store lives in THIS process's RAM (see
    # app.services.llm_proxy_token). A second worker/replica would receive a sandbox's broker
    # call having never minted that grant and 401 every model call — intermittent auth failures
    # that look like a bug at 3 AM. Fail fast on the one multi-process case we can detect here
    # (uvicorn/gunicorn workers); cross-host replicas can't be seen from app code and need
    # single-replica deployment or sticky routing (documented in docs/llm-auth-broker.md).
    if settings.llm_broker_enabled:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1") or "1")
        if workers > 1:
            raise RuntimeError(
                f"LLM_BROKER_ENABLED requires a single worker (WEB_CONCURRENCY={workers}); the "
                "per-turn grant store lives in process RAM. Run single-worker with single-replica "
                "deployment (or sticky routing), or disable LLM_BROKER_ENABLED."
            )
        logger.info("llm_broker_enabled", note="single-replica in-process grant store")
    web_push.open_http_session(create_client=settings.push_enabled)
    yield
    # Shutdown
    logger.info("shutting_down_api")
    await web_push.close_http_session()
    if settings.oidc_enabled:
        await stop_state_cleanup()
    if settings.connectors_enabled:
        await stop_connector_cleanup()
    if settings.sandbox_enabled:
        await stop_sandbox_reaper()
    await stop_listener()
    await close_db()
    logger.info("database_closed")
    if settings.metrics_enabled:
        stop_metrics_server()
    shutdown_telemetry()
    logger.info("telemetry_shutdown")


# Create FastAPI application. In production the interactive docs and the OpenAPI schema
# are disabled: they are unauthenticated and would expose the full API surface (admin,
# proxy routes) to anonymous recon. Dev keeps them for convenience.
_docs_enabled = settings.app_env != "production"
app = FastAPI(
    title="Inkwest API",
    description=(
        "AI-powered investigation assistant brokering read-only access "
        "across Kubernetes, AWS, and GitLab"
    ),
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Configure middleware (order matters - first added = outermost)
# Access logging should be outermost to capture full request lifecycle
app.add_middleware(AccessLogMiddleware, config=access_log_config)

# Per-IP rate limiting sits just inside access logging (so 429s are logged) and outside
# CORS/routing (so a flood is shed before doing real work). No-op when disabled.
app.add_middleware(RateLimitMiddleware)

# Host header validation (opt-in via TRUSTED_HOSTS). Blocks Host-header spoofing / cache
# poisoning / absolute-URL injection. Left off by default so k8s/ALB probes (which send the
# pod IP as Host) are not rejected; production warns when unset so it is a conscious choice.
_trusted_hosts = [h.strip() for h in settings.trusted_hosts.split(",") if h.strip()]
if _trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)
elif settings.app_env == "production":
    logger.warning(
        "trusted_hosts_unset",
        note="Set TRUSTED_HOSTS to your public hostname(s) to enable Host-header validation.",
    )

cors_origins = (
    [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if settings.cors_origins
    else [settings.frontend_url]
)
# Session auth rides a SameSite=Lax cookie: the browser only sends it to the API
# when the SPA is same-site as the API (the combined prod image is same-origin;
# dev :3000/:8000 is same-site localhost). An explicit CORS_ORIGINS override
# usually means a cross-site SPA host — where Lax silently withholds the cookie
# and every request 401s. Surface that at startup so it isn't debugged at 3 AM.
if settings.cors_origins:
    logger.warning(
        "cors_origins_override_set",
        origins=cors_origins,
        note=(
            "Session cookie is SameSite=Lax; a cross-site SPA host will not "
            "receive it. Keep the SPA same-site as the API."
        ),
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Exception handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for unhandled exceptions."""
    # Walk the cause chain to find the root cause
    root: BaseException = exc
    while root.__cause__:
        root = root.__cause__
    logger.error(
        "unhandled_exception",
        error=str(root),
        error_type=type(root).__name__,
        exc_type=type(exc).__name__,
        path=str(request.url.path),
        method=request.method,
    )
    logger.debug("unhandled_exception_trace", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# Include routers
app.include_router(health.router)
app.include_router(clusters.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(me.router, prefix="/api")  # Current-user prefs + personal skills
app.include_router(system_prompts.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")  # Per-conversation sandbox resource metrics
app.include_router(sessions.search_router, prefix="/api")
app.include_router(sharing.router, prefix="/api")  # Conversation sharing / collaboration
app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(usage.router, prefix="/api")  # Self-service usage reports (/api/me/usage)
app.include_router(config.router, prefix="/api")
app.include_router(connectors.router, prefix="/api")
app.include_router(mcp.router, prefix="/api")
# /api/mcp-proxy/{key}/{path} — auth is the internal proxy JWT, not get_current_user.
app.include_router(mcp_proxy.router, prefix="/api")
# /api/llm-proxy/model/{id}/{op} — auth is the per-turn broker grant, not get_current_user.
app.include_router(llm_proxy.router, prefix="/api")
app.include_router(chat.router)  # WebSocket routes (no prefix needed)
app.include_router(sync.router)  # Session sync WebSocket
app.include_router(egress.router)  # Per-conversation squid egress log WebSocket


class SPAStaticFiles(StaticFiles):
    """Static files with index.html fallback so client-side routes resolve on reload."""

    # Never fall back for these: a missing API endpoint must stay a 404 (version
    # skew during rolling deploys would otherwise surface as JSON-parse errors
    # client-side), and a purged hashed asset must not come back as text/html.
    NO_FALLBACK_PREFIXES = ("api/", "assets/", "ws/")

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith(self.NO_FALLBACK_PREFIXES):
                raise
            response = await super().get_response("index.html", scope)
        # index.html must revalidate on every load: hashed asset names change per
        # deploy, and a heuristically-cached index would reference purged bundles.
        if isinstance(response, FileResponse) and response.path.endswith("index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def mount_spa(app: FastAPI, static_dir: Path) -> bool:
    """Serve the built frontend from static_dir; no-op if it doesn't exist."""
    if not static_dir.is_dir():
        return False
    app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="spa")
    return True


# The combined prod image bakes the built frontend into ./static; in dev the
# dir doesn't exist (vite serves the SPA) so the API-info root is kept instead.
if mount_spa(app, Path(__file__).resolve().parent.parent / "static"):
    # Compensate what nginx provided in the split-image layout: response
    # compression and browser security headers (CSP kept in sync with the
    # values previously set in frontend/nginx.conf).
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            # connect-src 'self' also covers same-origin ws/wss (CSP3) — the SPA and the
            # WebSocket are same-origin in the combined image, so no wildcard ws: is needed.
            # blob: lets attachment thumbnails render — they're fetched with the auth
            # header then shown via URL.createObjectURL() (a same-origin blob: URL).
            "default-src 'self'; script-src 'self'; connect-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "frame-ancestors 'none'",
        )
        # HSTS only in production (served over HTTPS via the ALB); pointless/counter-
        # productive on plain-HTTP localhost in dev.
        if settings.app_env == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response

else:

    @app.get("/")
    async def root() -> dict[str, str]:
        """Root endpoint."""
        return {
            "name": "Inkwest API",
            "version": settings.version,
            "docs": "/docs",
        }
