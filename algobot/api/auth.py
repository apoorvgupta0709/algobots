"""API-key auth for state-changing routes.

Control actions (promote/demote/killswitch/gates) and job enqueueing can
change platform state — including, with the live fuse open, promoting a
strategy to real-money trading — so they require a shared secret.

The key is configured via the ``ALGOBOT_API_KEY`` env var (see .env.example)
and presented in the ``X-API-Key`` request header. When no key is configured
the protected routes are DISABLED (503) rather than open: fail closed.
Read-only GET routes stay unauthenticated (the API binds to loopback).
"""
from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    configured = os.getenv("ALGOBOT_API_KEY")
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="control routes disabled: ALGOBOT_API_KEY is not configured "
                   "(set it in .env to enable state-changing endpoints)")
    if not x_api_key or not secrets.compare_digest(x_api_key, configured):
        raise HTTPException(status_code=401,
                            detail="invalid or missing X-API-Key header")
