"""Asynchronous job-queue routes for Hermes.

Flow: POST /queries enqueues a QueryJobRow (202 + poll URL) -> the
QueryWorker daemon answers it -> GET /queries/{id} returns the result
envelope ``{type, data, note?}`` (or ``{error}`` with status=error).
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from algobot.api.auth import require_api_key
from algobot.api.readers import DEFAULT_LIMIT, MAX_LIMIT
from algobot.persistence.db import session_scope
from algobot.persistence.schema import QueryJobRow

router = APIRouter(prefix="/queries", tags=["queries"])

_STATUSES = {"queued", "running", "done", "error"}


class QueryCreate(BaseModel):
    """Either a typed query {type, params} or a freeform {question}."""
    type: Optional[str] = Field(None, description="typed query name, e.g. 'pnl'")
    params: dict = Field(default_factory=dict)
    question: Optional[str] = Field(None, description="freeform natural-language question")

    @model_validator(mode="after")
    def _one_of(self) -> "QueryCreate":
        if not self.type and not self.question:
            raise ValueError("provide either 'type' (with optional 'params') "
                             "or a freeform 'question'")
        return self


def _job_dict(row: QueryJobRow) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "payload": row.payload_json,
        "result": row.result_json,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


# POST is key-protected: the worker executes control job types (promote/
# killswitch) through the same do_* helpers as the control routes.
@router.post("", status_code=202, dependencies=[Depends(require_api_key)])
def enqueue_query(body: QueryCreate) -> dict:
    """Enqueue a query job; poll the returned URL for the answer."""
    job_id = str(uuid.uuid4())
    payload = body.model_dump(exclude_none=True)
    with session_scope() as s:
        s.add(QueryJobRow(id=job_id, payload_json=payload, status="queued",
                          created_at=dt.datetime.utcnow()))
    return {"id": job_id, "status": "queued", "poll": f"/queries/{job_id}"}


@router.get("/{job_id}")
def get_query(job_id: str) -> dict:
    """Job state + result. status: queued|running|done|error."""
    with session_scope() as s:
        row = s.get(QueryJobRow, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown query job '{job_id}'")
        return _job_dict(row)


@router.get("")
def list_queries(
    status: Optional[str] = Query(None, description="queued|running|done|error"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[dict]:
    """Recent jobs, newest first, optionally filtered by status."""
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"status must be one of {sorted(_STATUSES)}")
    with session_scope() as s:
        q = s.query(QueryJobRow)
        if status:
            q = q.filter(QueryJobRow.status == status)
        rows = q.order_by(QueryJobRow.created_at.desc()).limit(limit).all()
        return [_job_dict(r) for r in rows]
