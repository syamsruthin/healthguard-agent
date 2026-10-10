from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import require_admin
from backend.core.errors import NotFoundError
from backend.db.models import EvalRun, Trace, User
from backend.db.session import get_session
from backend.services import metrics_service
from backend.workers.queue import get_enqueuer

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/metrics/summary")
async def metrics_summary(
    days: int = Query(7, ge=1, le=90), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return await metrics_service.summary(session, days=days)


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise NotFoundError("Trace not found")
    return {
        "id": str(trace.id),
        "conversation_id": str(trace.conversation_id) if trace.conversation_id else None,
        "message_id": str(trace.message_id) if trace.message_id else None,
        "total_ms": trace.total_ms,
        "created_at": trace.created_at.isoformat(),
        "steps": trace.steps,
    }


class EvalRunIn(BaseModel):
    limit: int | None = Field(default=10, ge=1, le=500, description="Golden cases to run (None = all)")


@router.post("/evals/run", status_code=status.HTTP_202_ACCEPTED)
async def start_eval(
    body: EvalRunIn, admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    run = EvalRun(suite="golden", status="queued", config={"limit": body.limit}, triggered_by=admin.id)
    session.add(run)
    await session.commit()
    await get_enqueuer().enqueue("run_offline_eval", str(run.id), body.limit, job_id=f"eval:{run.id}")
    return {"id": str(run.id), "status": run.status}


@router.get("/evals/runs")
async def eval_runs(
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    rows = await session.execute(select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit))
    return [
        {
            "id": str(r.id),
            "suite": r.suite,
            "status": r.status,
            "passed": r.passed,
            "summary": r.summary,
            "config": r.config,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "error": r.error,
        }
        for r in rows.scalars()
    ]
