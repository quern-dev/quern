"""API routes for server-level system operations: update awareness + trigger.

These endpoints exist so MCP clients (and other API consumers) can surface
"a new Quern release is available" inline in the workflow the user is
already in — Claude Code calls ``ensure_server``, learns there's an
update, mentions it, and can call ``POST /update`` if the user agrees.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.config import (
    VALID_UPDATE_CHANNELS,
    channel_to_release_branch,
    get_update_channel,
    set_update_channel,
)
from server.lifecycle.update_check import read_update_info

logger = logging.getLogger("quern-debug-server.system")

router = APIRouter(prefix="/api/v1/system", tags=["system"])


class UpdateStatusResponse(BaseModel):
    """Result of the most recent ``quern.dev/api/check-update`` poll.

    Populated by the periodic 24h background check. If the server hasn't
    run a check yet (fresh install, opted out, or first 24h), ``checked_at``
    is None and ``update_available`` is False.
    """

    update_available: bool = False
    current_version: str | None = None
    latest_version: str | None = None
    checked_at: str | None = None
    message: str | None = None
    channel: str = "stable"  # The user's configured update channel (#41)
    release_branch: str = "release/stable"  # Branch the channel tracks


class UpdateChannelResponse(BaseModel):
    """Current update channel preference."""

    channel: str  # "stable" | "beta"
    release_branch: str  # The git branch this channel tracks
    valid_channels: list[str]


class SetUpdateChannelRequest(BaseModel):
    channel: str  # "stable" | "beta"


class UpdateTriggerResponse(BaseModel):
    """Acknowledgment that the update was launched. The actual upgrade
    runs in a detached child — the response returns before the child
    starts replacing files, so the caller gets a clean reply even though
    the server is about to restart itself."""

    status: str  # "launched" | "skipped"
    pid: int | None = None
    note: str


@router.get("/update-status", response_model=UpdateStatusResponse)
async def update_status() -> UpdateStatusResponse:
    """Return the most recent persisted update-check result.

    Cached in ``~/.quern/update-info.json``; refreshed by the background
    task every 24 hours (see ``server/lifecycle/update_check.py``). Cheap
    to call — no network round-trip.

    Also includes the configured update channel and the git branch it
    tracks so MCP clients can mention things like *"you're on stable;
    beta has 0.13.5-beta.2 available"* once the channels work is wired
    end-to-end.
    """
    channel = get_update_channel()
    release_branch = channel_to_release_branch(channel)
    info = read_update_info()
    if info is None:
        return UpdateStatusResponse(channel=channel, release_branch=release_branch)
    return UpdateStatusResponse(
        update_available=bool(info.get("update_available")),
        current_version=info.get("current_version"),
        latest_version=info.get("latest_version"),
        checked_at=info.get("checked_at"),
        message=info.get("message"),
        channel=channel,
        release_branch=release_branch,
    )


@router.get("/channel", response_model=UpdateChannelResponse)
async def get_channel() -> UpdateChannelResponse:
    """Return the user's current update channel preference."""
    channel = get_update_channel()
    return UpdateChannelResponse(
        channel=channel,
        release_branch=channel_to_release_branch(channel),
        valid_channels=list(VALID_UPDATE_CHANNELS),
    )


@router.put("/channel", response_model=UpdateChannelResponse)
async def put_channel(body: SetUpdateChannelRequest) -> UpdateChannelResponse:
    """Set the update channel preference.

    Persists to ``~/.quern/config.json``. Does not switch git branches
    or apply an immediate update — that's a separate call to ``POST
    /update`` once the user is ready (and, for dev clones, has
    explicitly switched their branch).
    """
    try:
        set_update_channel(body.channel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return UpdateChannelResponse(
        channel=body.channel,
        release_branch=channel_to_release_branch(body.channel),
        valid_channels=list(VALID_UPDATE_CHANNELS),
    )


@router.post("/update", response_model=UpdateTriggerResponse)
async def trigger_update() -> UpdateTriggerResponse:
    """Launch ``quern update`` in a detached child and respond immediately.

    The actual update is a multi-step operation (git pull / tarball
    fetch, pip reinstall, MCP rebuild, daemon restart) that takes
    minutes and ends by killing this server. Running it inline would
    leave the caller hanging or breaking their connection mid-response.
    Detach via ``Popen(start_new_session=True)`` — same pattern Quern
    uses for the daemon — so this response can land before the child
    starts replacing files.
    """
    project_root = _find_project_root()
    if project_root is None:
        return UpdateTriggerResponse(
            status="skipped",
            note="Could not locate the Quern project root from this server "
                 "process — run `quern update` from your install manually.",
        )

    # Use the same Python interpreter that's running the server so we
    # don't accidentally pick up a different venv via PATH.
    cmd = [sys.executable, "-m", "server", "update"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "QUERN_UPDATE_TRIGGERED_BY": "api"},
        )
    except OSError as e:
        logger.warning("Failed to launch update child: %s", e)
        return UpdateTriggerResponse(
            status="skipped",
            note=f"Failed to launch update: {e}",
        )

    return UpdateTriggerResponse(
        status="launched",
        pid=proc.pid,
        note=(
            "Update started in a detached child. The server will restart "
            "in a few seconds — wait ~30-60s and reconnect."
        ),
    )


def _find_project_root() -> Path | None:
    """Find the Quern project root (the directory containing pyproject.toml)."""
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / "pyproject.toml").exists():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    return None
