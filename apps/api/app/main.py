"""
The API Gateway Layer, Passage 1 §3 — FastAPI (Python), REST, separate
from the Next.js web UI. The frontend calls this directly over HTTP; no
Next.js API route sits in between anymore.

Rewired this pass to actually wire in the modules this session built
that were previously dead code — present in the codebase but never
called from the app that serves requests:

* `config.load_env_files()` — makes `.env.local` real (see
  `app/config.py`'s docstring for the defect this closes). Called
  first, before anything else reads an environment variable.
* `observability.configure_logging()` + `RequestContextMiddleware` —
  structured logs with a request id, replacing the three bare `print()`
  calls that were the entire error-reporting story before this pass.
* `errors.install_error_handlers()` — one error contract for every
  failure path, replacing the split where only `RequestValidationError`
  got a clean `{error, details}` body and every `HTTPException` fell
  through to FastAPI's default `{detail}` (which the frontend's
  `data.error` read never saw).
* `rate_limit.RateLimitMiddleware` — Passage 4 §6.2's per-plan limits,
  previously declared in settings but never enforced.
* `routers.providers` — the restored `/api/ai/route` (Passage 4 §6.1 /
  G08a) and the generalised `/api/ai/providers`, `/api/ai/{id}/test`,
  `/api/ai/{id}/health` endpoints.
* `routers.auth` — register/login/logout/me, the endpoints that make
  `config.AuthSettings` and `security/jwt.py` real rather than unused
  scaffolding. `/api/ai/{id}/test` and `/api/ai/{id}/health` require the
  admin role; `/api/ai/route` requires any authenticated user.

Phase 1 of the Passage 1 + Passage 4 continuation (authentication and
gateway security): `/api/ai/analyze` and the comparison-feedback endpoint
now REQUIRE a JWT (Passage 4 §6.2) unless `AUTH_REQUIRED=false` is set
explicitly for local development; knowledge upload/delete are admin-only
and search/list need a login; rate-limit identity is the user (or a
validated client IP) instead of a spoofable header; unhandled-error
responses carry CORS headers so a browser can read the error id.
"""

from __future__ import annotations

# Must run before any other project import touches an environment
# variable — Settings(), provider is_configured() checks, and the CORS
# origin list are all read from `os.environ` downstream of this call.
from .config import load_env_files

_loaded_env_files = load_env_files()

import asyncio  # noqa: E402
import contextlib  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from .config import get_settings  # noqa: E402
from .errors import install_error_handlers  # noqa: E402
from .observability import RequestContextMiddleware, configure_logging, get_logger  # noqa: E402
from .rate_limit import RateLimitMiddleware  # noqa: E402
from .routers import analyze, auth, comparisons, knowledge, models, providers, tools  # noqa: E402

settings = get_settings()
configure_logging(level=settings.log_level, json_output=settings.log_json)
logger = get_logger("startup")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from .engine.native_bridge import load_native
    from .tools.registry import get_tool_registry

    native_status = load_native()

    tool_registry = get_tool_registry()
    await tool_registry.seed_database()

    if not settings.auth.auth_required:
        logger.warning(
            "AUTH_REQUIRED=false — /api/ai/analyze accepts anonymous requests. "
            "Local development only; Passage 4 §6.2 requires JWT in any shared deployment."
        )
    elif not settings.auth.is_configured:
        logger.warning(
            "AUTH_REQUIRED is on but JWT_SECRET is not set — nobody can log in, so every "
            "authenticated endpoint will return 401. Set JWT_SECRET (see apps/api/.env.example)."
        )

    from .db.client import is_db_configured
    from .db.retention import retention_loop
    from .providers.availability import availability_loop
    from .providers.runtime_config import reload_runtime_config, runtime_config_loop

    # What the database controls (model registry, provider settings, breaker thresholds) is loaded
    # into memory before the first request, seeding the model catalogue into an empty/older
    # database. A failure is reported and logged, never fatal: the process serves from the
    # built-in catalogue and environment defaults until the next refresh succeeds.
    runtime_report = await reload_runtime_config(seed=True) if is_db_configured() else None

    # Background jobs — each only with a database, and each disabled by setting its interval to 0:
    #   retention         purge expired sessions/memories             (db/retention.py)
    #   runtime-config    re-read admin-managed configuration         (providers/runtime_config.py)
    #   model-availability poll the vendors' model lists (G02g)       (providers/availability.py)
    jobs: dict[str, asyncio.Task] = {}
    cfg = get_settings()
    if is_db_configured():
        if cfg.retention_interval_seconds > 0:
            jobs["retention"] = asyncio.create_task(retention_loop(cfg.retention_interval_seconds))
        if cfg.runtime_config_refresh_seconds > 0:
            jobs["runtime-config"] = asyncio.create_task(runtime_config_loop(cfg.runtime_config_refresh_seconds))
        if cfg.model_availability_interval_seconds > 0:
            jobs["model-availability"] = asyncio.create_task(
                availability_loop(cfg.model_availability_interval_seconds)
            )

    logger.info(
        "oxygen-ai api starting",
        extra={
            "envFilesLoaded": _loaded_env_files,
            "databaseConfigured": settings.database_configured,
            "authConfigured": settings.auth.is_configured,
            "authRequired": settings.auth.auth_required,
            "corsAllowedOrigins": settings.cors_allowed_origins,
            "nativeLayerAvailable": native_status.available,
            "nativeLayerDetail": native_status.reason,
            "toolsRegistered": len(tool_registry.all()),
            "backgroundJobs": sorted(jobs),
            "runtimeConfig": runtime_report,
        },
    )
    try:
        yield
    finally:
        for task in jobs.values():
            task.cancel()
        for task in jobs.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Release the asyncpg pool's connections instead of leaving them to
        # process exit (P2: `close_pool()` previously had no caller in the server).
        from .db.client import close_pool

        await close_pool()


app = FastAPI(title="Oxygen AI API Gateway", version="0.3.0", lifespan=_lifespan)

install_error_handlers(app, allowed_origins=settings.cors_allowed_origins)

app.add_middleware(RateLimitMiddleware, settings=settings.rate_limit)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id", "X-Response-Time-Ms"],
)


@app.get("/health")
async def health():
    from .db.client import is_db_configured
    from .engine.native_bridge import native_status

    status = native_status()
    return {
        "status": "ok",
        "service": "oxygen-ai-api",
        "databaseConfigured": is_db_configured(),
        "authConfigured": settings.auth.is_configured,
        "authRequired": settings.auth.auth_required,
        "nativeLayerAvailable": status.available,
    }


app.include_router(auth.router)
app.include_router(analyze.router)
app.include_router(comparisons.router)
app.include_router(providers.router)
app.include_router(models.router)
app.include_router(tools.router)
app.include_router(knowledge.router)