"""Template-based flow summary generator.

Mirrors the pattern from server/processing/summarizer.py but operates on
FlowRecord objects instead of LogEntry objects. Generates structured and
prose summaries from captured HTTP flows — no LLM calls needed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from server.models import (
    FlowErrorPattern,
    FlowRecord,
    FlowSummaryResponse,
    HostSummary,
    SlowRequest,
)
from server.processing.summarizer import make_cursor

# Flows slower than this threshold are flagged as slow
SLOW_THRESHOLD_MS = 1000.0


def generate_flow_summary(
    flows: list[FlowRecord],
    window: str = "5m",
    host: str | None = None,
) -> FlowSummaryResponse:
    """Generate a structured summary from a list of flow records.

    Args:
        flows: Flow records to summarize (should already be filtered by time window).
        window: The window label (e.g., "5m") for the response.
        host: If set, only summarize flows to/from this host.
    """
    now = datetime.now(timezone.utc)

    # Filter by host if requested
    if host:
        flows = [f for f in flows if f.request.host == host]

    # Group flows by host
    by_host: dict[str, list[FlowRecord]] = defaultdict(list)
    for f in flows:
        by_host[f.request.host].append(f)

    # Build per-host summaries
    host_summaries: list[HostSummary] = []
    for h, host_flows in sorted(by_host.items(), key=lambda kv: len(kv[1]), reverse=True):
        success = 0
        client_error = 0
        server_error = 0
        connection_errors = 0
        latencies: list[float] = []

        for f in host_flows:
            if f.error:
                connection_errors += 1
            elif f.response is None:
                connection_errors += 1
            elif f.response.status_code >= 500:
                server_error += 1
            elif f.response.status_code >= 400:
                client_error += 1
            else:
                success += 1

            if f.timing.total_ms is not None:
                latencies.append(f.timing.total_ms)

        avg_latency = sum(latencies) / len(latencies) if latencies else None

        host_summaries.append(HostSummary(
            host=h,
            total=len(host_flows),
            success=success,
            client_error=client_error,
            server_error=server_error,
            connection_errors=connection_errors,
            avg_latency_ms=round(avg_latency, 1) if avg_latency is not None else None,
        ))

    # Extract error patterns: "{METHOD} {path} -> {status}" for 4xx/5xx/errors
    error_groups: dict[str, list[FlowRecord]] = defaultdict(list)
    for f in flows:
        if f.error:
            pattern = f"{f.request.method} {f.request.path} -> error ({f.error})"
            error_groups[pattern].append(f)
        elif f.response and f.response.status_code >= 400:
            pattern = f"{f.request.method} {f.request.path} -> {f.response.status_code}"
            error_groups[pattern].append(f)

    error_patterns: list[FlowErrorPattern] = []
    for pattern, group in sorted(error_groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        error_patterns.append(FlowErrorPattern(
            pattern=pattern,
            count=len(group),
            first_seen=min(f.timestamp for f in group),
            last_seen=max(f.timestamp for f in group),
        ))

    # Identify slow requests (> SLOW_THRESHOLD_MS)
    slow_requests: list[SlowRequest] = []
    for f in flows:
        if f.timing.total_ms is not None and f.timing.total_ms > SLOW_THRESHOLD_MS:
            slow_requests.append(SlowRequest(
                method=f.request.method,
                url=f.request.url,
                total_ms=round(f.timing.total_ms, 1),
                status_code=f.response.status_code if f.response else None,
            ))
    slow_requests.sort(key=lambda s: s.total_ms, reverse=True)

    # Generate prose summary
    summary = _build_prose(
        window=window,
        host=host,
        total=len(flows),
        host_summaries=host_summaries,
        error_patterns=error_patterns,
        slow_requests=slow_requests,
    )

    # Cursor = timestamp of latest flow (or now if empty)
    cursor_ts = max((f.timestamp for f in flows), default=now)

    return FlowSummaryResponse(
        window=window,
        generated_at=now,
        cursor=make_cursor(cursor_ts),
        summary=summary,
        total_flows=len(flows),
        by_host=host_summaries,
        errors=error_patterns,
        slow_requests=slow_requests,
    )


def _build_prose(
    window: str,
    host: str | None,
    total: int,
    host_summaries: list[HostSummary],
    error_patterns: list[FlowErrorPattern],
    slow_requests: list[SlowRequest],
) -> str:
    """Compose a natural-language summary from structured data."""
    if total == 0:
        if host:
            return f"No HTTP traffic to {host} in the last {window}."
        return f"No HTTP traffic captured in the last {window}."

    num_hosts = len(host_summaries)
    parts = [f"{total} requests across {num_hosts} host(s) in the last {window}."]

    # Per-host error highlights (top 3)
    error_hosts = [h for h in host_summaries if h.client_error + h.server_error + h.connection_errors > 0]
    for h in error_hosts[:3]:
        err_total = h.client_error + h.server_error + h.connection_errors
        parts.append(f"{h.host}: {err_total} error(s)")
        # Find patterns for this host
        host_patterns = [
            p for p in error_patterns
            if h.host in p.pattern
        ]
        if host_patterns:
            descs = [f"{p.pattern} x{p.count}" for p in host_patterns[:2]]
            parts[-1] += f" ({', '.join(descs)})"
        parts[-1] += "."

    # No errors
    if not error_hosts:
        parts.append("No errors detected.")

    # Slow requests
    if slow_requests:
        count = len(slow_requests)
        top = slow_requests[0]
        parts.append(
            f"{count} slow request(s) (slowest: {top.method} {top.url}, {top.total_ms:.1f}ms)."
        )

    return " ".join(parts)
