"""HTTP surface for the sandbox job queue (conductor task e825e00a).

Mounted at /api/conductor/jobs. Deliberately agent-agnostic: any MCP
client OR a plain `curl` can drive the pull-loop, which is the whole
point — the flow lives on the server, the worker just claims and reports.
Kept off the real conductor router so the experiment stays isolated.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from prism_service.services import job_queue

router = APIRouter()


class EnqueueBody(BaseModel):
    task_id: str
    stages: list  # list[list[{step, role?, kind?, instructions?, expected_proof?}]]


class ClaimBody(BaseModel):
    worker_id: str
    role: Optional[str] = None
    task_id: Optional[str] = None


class ReportBody(BaseModel):
    job_id: str
    worker_id: str
    outcome: object = ""


@router.post("/enqueue")
def enqueue(body: EnqueueBody) -> dict:
    return job_queue.enqueue(body.task_id, body.stages)


@router.post("/claim")
def claim(body: ClaimBody) -> dict:
    return job_queue.claim(body.worker_id, role=body.role, task_id=body.task_id)


@router.post("/report")
def report(body: ReportBody) -> dict:
    return job_queue.report(body.job_id, body.worker_id, body.outcome)


@router.get("/state")
def state(task_id: str) -> dict:
    return job_queue.state(task_id)
