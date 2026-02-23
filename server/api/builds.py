"""API routes for build results."""

from __future__ import annotations

import pathlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server.models import BuildResult

router = APIRouter(prefix="/api/v1/builds", tags=["builds"])


class BuildParseRequest(BaseModel):
    output: str


class BuildParseFileRequest(BaseModel):
    file_path: str
    include_raw_warnings: bool = False
    fuzzy_groups: bool = False


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


@router.post("/parse-file", response_model=BuildResult)
async def parse_build_file(request: Request, body: BuildParseFileRequest) -> BuildResult:
    """Read a build log file and return the parsed result."""
    path = pathlib.Path(body.file_path).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"File not found: {body.file_path}")
    try:
        content = path.read_text(errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read file: {exc}") from exc
    build_adapter = request.app.state.build_adapter
    result = await build_adapter.parse_build_output(content, fuzzy=body.fuzzy_groups)
    if not body.include_raw_warnings:
        result = result.model_copy(update={"warnings": []})
    return result
