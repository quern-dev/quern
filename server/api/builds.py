"""API routes for build results."""

from __future__ import annotations

import asyncio
import logging
import pathlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server.api.actions import logged_action
from server.device import build_records
from server.models import BuildRecord, BuildResult

router = APIRouter(prefix="/api/v1/builds", tags=["builds"])
logger = logging.getLogger(__name__)


class BuildParseRequest(BaseModel):
    output: str


class BuildParseFileRequest(BaseModel):
    file_path: str
    include_raw_warnings: bool = False
    fuzzy_groups: bool = True


def _ensure_summary(result: BuildResult) -> BuildResult:
    """Populate the summary field if not already set."""
    if not result.summary:
        result = result.model_copy(update={"summary": result.generate_summary()})
    return result


@router.get("/latest")
async def get_latest_build(request: Request) -> BuildResult | None:
    """Return the most recent parsed build result."""
    build_adapter = request.app.state.build_adapter
    if build_adapter is None:
        return None
    result = build_adapter.latest_result
    return _ensure_summary(result) if result else None


@router.post("/parse", response_model=BuildResult)
@logged_action("parse_build", category="build")
async def parse_build(request: Request, body: BuildParseRequest) -> BuildResult:
    """Accept raw xcodebuild output and return the parsed result."""
    build_adapter = request.app.state.build_adapter
    return _ensure_summary(await build_adapter.parse_build_output(body.output))


@router.post("/parse-file", response_model=BuildResult)
@logged_action("parse_build_file", category="build")
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
    return _ensure_summary(result)


class RecordAndroidBuildRequest(BaseModel):
    module_path: str
    variant: str


class RecordAndroidBuildResponse(BaseModel):
    record: BuildRecord
    summary: str


@router.post("/android/record", response_model=RecordAndroidBuildResponse)
@logged_action("record_android_build", category="build")
async def record_android_build(
    request: Request, body: RecordAndroidBuildRequest,
) -> RecordAndroidBuildResponse:
    """Record a Gradle build so its crashes can be symbolicated (#326).

    quern does not run Gradle (#347): build the variant, then record it. The
    record keeps the APK's package and version, R8's mapping.txt for a
    minified variant, and the unstripped native libraries by BuildId, all
    copied, because the next build overwrites them.
    """
    module = pathlib.Path(body.module_path).expanduser()
    if not body.variant.strip():
        raise HTTPException(
            status_code=400, detail="variant is empty: name one, e.g. stagingRelease")
    if not module.is_absolute():
        # Relative to the daemon's directory, which is nobody's project -- and
        # stored as given, the key retention counts builds by.
        raise HTTPException(status_code=400, detail=f"module_path must be absolute: {module}")
    if not module.is_dir():
        raise HTTPException(status_code=400, detail=f"{module} is not a directory")
    try:
        record = await build_records.record_android_build(module, body.variant.strip())
    except build_records.AndroidBuildNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if record.build_id and not record.error:
        try:
            removed = await asyncio.to_thread(build_records.prune)
        except Exception as e:  # noqa: BLE001 -- the record was made
            record.notes.append(f"retention of older build records failed: {e}")
        else:
            if removed:
                logger.info("Build record retention removed: %s", "; ".join(removed))
    return RecordAndroidBuildResponse(record=record, summary=build_records.summary_line(record))
