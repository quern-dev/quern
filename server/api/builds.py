"""API routes for build results."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from server.models import BuildResult

router = APIRouter(prefix="/api/v1/builds", tags=["builds"])


class BuildParseRequest(BaseModel):
    output: str


@router.get("/latest")
async def get_latest_build(request: Request) -> BuildResult | None:
    """Return the most recent parsed build result."""
    build_adapter = request.app.state.build_adapter
    if build_adapter is None:
        return None
    return build_adapter.latest_result


@router.post("/parse", response_model=BuildResult)
async def parse_build(request: Request, body: BuildParseRequest) -> BuildResult:
    """Accept raw xcodebuild output and return the parsed result."""
    build_adapter = request.app.state.build_adapter
    return await build_adapter.parse_build_output(body.output)
