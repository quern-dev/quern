"""Capture session manager — bracket UI actions to isolate their network flows."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from server.models import (
    CaptureStartRequest,
    CaptureStopResponse,
    FlowQueryParams,
    FlowSummaryItem,
)
from server.proxy.flow_store import FlowStore

logger = logging.getLogger("quern-debug-server.capture-session")


class CaptureSession:
    """An active capture session tracking a time window and filter config."""

    __slots__ = (
        "id", "start_time", "hosts", "exclude_hosts",
        "simulator_udid", "client_ip", "detail",
    )

    def __init__(
        self,
        session_id: str,
        start_time: datetime,
        hosts: list[str] | None,
        exclude_hosts: list[str] | None,
        simulator_udid: str | None,
        client_ip: str | None,
        detail: str,
    ) -> None:
        self.id = session_id
        self.start_time = start_time
        self.hosts = hosts
        self.exclude_hosts = exclude_hosts
        self.simulator_udid = simulator_udid
        self.client_ip = client_ip
        self.detail = detail


class CaptureSessionManager:
    """Manages active capture sessions. State is in-memory only."""

    def __init__(self, ttl_seconds: float = 3600) -> None:
        self._sessions: dict[str, CaptureSession] = {}
        self._ttl = ttl_seconds

    def start(self, request: CaptureStartRequest) -> CaptureSession:
        """Create a new capture session."""
        self._cleanup_expired()
        session_id = request.id or f"cap_{uuid4().hex[:10]}"
        if session_id in self._sessions:
            raise ValueError(f"Capture session '{session_id}' already exists")
        session = CaptureSession(
            session_id=session_id,
            start_time=datetime.now(UTC),
            hosts=request.hosts,
            exclude_hosts=request.exclude_hosts,
            simulator_udid=request.simulator_udid,
            client_ip=request.client_ip,
            detail=request.detail or "full",
        )
        self._sessions[session_id] = session
        logger.info("Capture session started: %s", session_id)
        return session

    async def stop(
        self, session_id: str, flow_store: FlowStore,
    ) -> CaptureStopResponse:
        """Stop a session, query its flows, and return results."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(f"Capture session '{session_id}' not found")

        duration = (datetime.now(UTC) - session.start_time).total_seconds()

        params = FlowQueryParams(
            since=session.start_time,
            hosts=session.hosts,
            exclude_hosts=session.exclude_hosts,
            simulator_udid=session.simulator_udid,
            client_ip=session.client_ip,
            device_id="",  # don't filter by device_id (default is "default")
            limit=1000,
        )
        flows, total = await flow_store.query(params)

        # Build by_host breakdown
        host_counts: dict[str, int] = {}
        for f in flows:
            host_counts[f.request.host] = host_counts.get(f.request.host, 0) + 1
        from server.models import HostSummary
        by_host = [
            HostSummary(host=h, total=c)
            for h, c in sorted(host_counts.items(), key=lambda x: -x[1])
        ]

        if session.detail == "summary":
            summaries = [
                FlowSummaryItem(
                    id=f.id,
                    timestamp=f.timestamp,
                    method=f.request.method,
                    url=f.request.url,
                    host=f.request.host,
                    path=f.request.path,
                    status_code=f.response.status_code if f.response else None,
                    error=f.error,
                    total_ms=f.timing.total_ms if f.timing else None,
                )
                for f in flows
            ]
            logger.info(
                "Capture session stopped: %s (%d flows, %.1fs)",
                session_id, total, duration,
            )
            return CaptureStopResponse(
                session_id=session_id,
                duration_seconds=round(duration, 2),
                total_flows=total,
                flow_summaries=summaries,
                by_host=by_host,
            )

        logger.info(
            "Capture session stopped: %s (%d flows, %.1fs)",
            session_id, total, duration,
        )
        return CaptureStopResponse(
            session_id=session_id,
            duration_seconds=round(duration, 2),
            total_flows=total,
            flows=flows,
            by_host=by_host,
        )

    def _cleanup_expired(self) -> None:
        """Remove sessions older than TTL."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._ttl)
        expired = [
            sid for sid, s in self._sessions.items()
            if s.start_time < cutoff
        ]
        for sid in expired:
            del self._sessions[sid]
            logger.info("Expired capture session: %s", sid)
