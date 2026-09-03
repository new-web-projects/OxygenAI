"""
The API Gateway Layer, Passage 1 §3 — FastAPI (Python), REST, separate
from the Next.js web UI. The frontend calls this directly over HTTP; no
Next.js API route sits in between anymore (that would be exactly the
"silently treat Next.js routes as a replacement for FastAPI" this
migration exists to undo).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routers import analyze, comparisons

app = FastAPI(title="Oxygen AI API Gateway", version="0.1.0")

_allowed_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Reshaped to the same {"error", "details"} the Next.js/Zod version
    # returned, so the frontend's existing error handling needs no
    # changes — only the request URL changed.
    return JSONResponse(
        status_code=400,
        content={"error": "Invalid request", "details": exc.errors()},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "oxygen-ai-api"}


app.include_router(analyze.router)
app.include_router(comparisons.router)
