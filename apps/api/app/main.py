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
"""

from __future__ import annotations

# Must run before any other project import touches an environment
# variable — Settings(), provider is_configured() checks, and the CORS
# origin list are all read from `os.environ` downstream of this call.
from .config import load_env_files

_loaded_env_files = load_env_files()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from .config import get_settings  # noqa: E402
from .errors import install_error_handlers  # noqa: E402
from .observability import RequestContextMiddleware, configure_logging, get_logger  # noqa: E402
from .rate_limit import RateLimitMiddleware  # noqa: E402
from .routers import analyze, comparisons, providers  # noqa: E402

settings = get_settings()
configure_logging(level=settings.log_level, json_output=settings.log_json)
logger = get_logger("startup")

app = FastAPI(title="Oxygen AI API Gateway", version="0.2.0")

install_error_handlers(app)

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


@app.on_event("startup")
async def _log_startup_state() -> None:
    from .engine.native_bridge import load_native

    native_status = load_native()
    logger.info(
        "oxygen-ai api starting",
        extra={
            "envFilesLoaded": _loaded_env_files,
            "databaseConfigured": settings.database_configured,
            "corsAllowedOrigins": settings.cors_allowed_origins,
            "nativeLayerAvailable": native_status.available,
            "nativeLayerDetail": native_status.reason,
        },
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
        "nativeLayerAvailable": status.available,
    }


app.include_router(analyze.router)
app.include_router(comparisons.router)
app.include_router(providers.router)