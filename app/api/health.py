"""Health endpoints.

`/health/live` answers "is the process up?" (used by container orchestrators to restart
a dead task). A readiness probe that checks the database is added with the DB layer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import __version__
from app.core.config import Settings, get_settings

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: str


@router.get("/live", response_model=LivenessResponse)
async def liveness(settings: Annotated[Settings, Depends(get_settings)]) -> LivenessResponse:
    return LivenessResponse(status="ok", version=__version__, environment=settings.app_env)
