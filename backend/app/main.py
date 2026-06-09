"""
Inkwest Backend - FastAPI Application
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    admin,
    auth,
    chat,
    clusters,
    connectors,
    egress,
    health,
    prompt_templates,
    sessions,
    sync,
    system_prompts,
)
from app.core.config import get_settings
from app.core.database import async_session_maker, close_db, init_db
from app.core.logging import AccessLogMiddleware, access_log_config, get_logger, setup_logging
from app.core.telemetry import shutdown_telemetry
from app.services.auth_cleanup import start_state_cleanup, stop_state_cleanup
from app.services.connector_cleanup import start_connector_cleanup, stop_connector_cleanup
from app.services.messages import reconcile_interrupted_turns
from app.services.prompts import ensure_default_system_prompt
from app.services.pubsub import start_listener, stop_listener
from app.services.sandbox import reap_orphans, start_sandbox_reaper, stop_sandbox_reaper

# Set up structured logging
setup_logging()
logger = get_logger(__name__)

# Get settings
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan management."""
    # Startup
    logger.info("starting_api", version="0.1.0")
    await init_db()
    logger.info("database_initialized")
    # Seed the hardcoded system prompt as the DB default on first startup.
    async with async_session_maker() as db:
        await ensure_default_system_prompt(db)
        logger.info("default_system_prompt_ensured")
        # Recover turns killed mid-flight by the previous shutdown (restart,
        # rolling deploy, crash): no turn is in flight yet, so any session ending
        # on an unanswered user message is an interrupted turn — mark it retryable.
        try:
            reconciled = await reconcile_interrupted_turns(db)
            if reconciled:
                logger.info("reconciled_interrupted_turns", count=reconciled)
        except Exception:
            logger.exception("reconcile_interrupted_turns_failed")
    await start_listener()
    logger.info("pubsub_listener_started")
    if settings.oidc_enabled:
        await start_state_cleanup()
    if settings.connectors_enabled:
        await start_connector_cleanup()
    if settings.sandbox_enabled:
        # Clear container/network orphans left by a prior (crashed) backend, then run
        # the idle reaper. Durable per-conversation dirs are kept so turns can resume.
        await reap_orphans()
        start_sandbox_reaper()
    yield
    # Shutdown
    logger.info("shutting_down_api")
    if settings.oidc_enabled:
        await stop_state_cleanup()
    if settings.connectors_enabled:
        await stop_connector_cleanup()
    if settings.sandbox_enabled:
        await stop_sandbox_reaper()
    await stop_listener()
    from app.services.orchestrator_openai import close_cached_openai_client

    await close_cached_openai_client()
    await close_db()
    logger.info("database_closed")
    shutdown_telemetry()
    logger.info("telemetry_shutdown")


# Create FastAPI application
app = FastAPI(
    title="Inkwest API",
    description="AI-powered Kubernetes debugging assistant",
    version="0.1.0",
    lifespan=lifespan,
)

# Configure middleware (order matters - first added = outermost)
# Access logging should be outermost to capture full request lifecycle
app.add_middleware(AccessLogMiddleware, config=access_log_config)

cors_origins = (
    [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if settings.cors_origins
    else [settings.frontend_url]
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
app.include_router(prompt_templates.router, prefix="/api")
app.include_router(system_prompts.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(sessions.search_router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(connectors.router, prefix="/api")
app.include_router(chat.router)  # WebSocket routes (no prefix needed)
app.include_router(sync.router)  # Session sync WebSocket
app.include_router(egress.router)  # Per-conversation squid egress log WebSocket


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "Inkwest API",
        "version": "0.1.0",
        "docs": "/docs",
    }
