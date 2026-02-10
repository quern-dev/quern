"""Template-based log summarizer.

Generates structured and prose summaries from a list of LogEntry objects.
No LLM calls — summaries are composed programmatically from templates.

Key features:
- Counts entries by level (weighted by repeat_count)
- Groups errors by normalized pattern
- Detects error→success resolution sequences
- Generates natural-language prose from templates
- Cursor support for delta summaries
"""

from __future__ import annotations

import base64
import struct
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from server.models import LogEntry, LogLevel, LogSummaryResponse, TopIssue
from server.processing.classifier import detect_resolution, extract_pattern

# Time windows accepted by the summary endpoint
WINDOW_DURATIONS: dict[str, timedelta] = {
    "30s": timedelta(seconds=30),
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
}


def make_cursor(ts: datetime) -> str:
    """Encode a timestamp into an opaque cursor string."""
    epoch_us = int(ts.timestamp() * 1_000_000)
    raw = struct.pack(">Q", epoch_us)
    return "c_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_cursor(cursor: str) -> datetime | None:
    """Decode an opaque cursor back into a timestamp. Returns None if invalid."""
    if not cursor.startswith("c_"):
        return None
    try:
        b64 = cursor[2:]
        # Re-pad base64
        b64 += "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64)
        epoch_us = struct.unpack(">Q", raw)[0]
        return datetime.fromtimestamp(epoch_us / 1_000_000, tz=timezone.utc)
    except Exception:
        return None


def generate_summary(
    entries: list[LogEntry],
    window: str = "5m",
    process: str | None = None,
) -> LogSummaryResponse:
    """Generate a structured summary from a list of log entries.

    Args:
        entries: Log entries to summarize (should already be filtered by time window).
        window: The window label (e.g., "5m") for the response.
        process: If set, only summarize entries from this process.
    """
    now = datetime.now(timezone.utc)

    # Filter by process if requested
    if process:
        entries = [e for e in entries if e.process == process]

    # Count by level (weighted by repeat_count)
    error_levels = set(LogLevel.at_least(LogLevel.ERROR))
    warning_levels = {LogLevel.WARNING}

    total_count = 0
    error_count = 0
    warning_count = 0

    for e in entries:
        total_count += e.repeat_count
        if e.level in error_levels:
            error_count += e.repeat_count
        elif e.level in warning_levels:
            warning_count += e.repeat_count

    # Group errors by pattern
    error_groups: dict[str, list[LogEntry]] = defaultdict(list)
    for e in entries:
        if e.level in error_levels:
            pattern = extract_pattern(e.message)
            error_groups[pattern].append(e)

    # Detect resolutions
    resolutions = detect_resolution(entries, process=process)
    resolved_patterns = {r["error_pattern"] for r in resolutions}

    # Build top issues
    top_issues: list[TopIssue] = []
    for pattern, group_entries in sorted(
        error_groups.items(), key=lambda kv: sum(e.repeat_count for e in kv[1]), reverse=True
    ):
        count = sum(e.repeat_count for e in group_entries)
        top_issues.append(TopIssue(
            pattern=pattern,
            count=count,
            first_seen=min(e.timestamp for e in group_entries),
            last_seen=max(e.timestamp for e in group_entries),
            resolved=pattern in resolved_patterns,
        ))

    # Group warnings by pattern for prose
    warning_groups: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.level in warning_levels:
            pattern = extract_pattern(e.message)
            warning_groups[pattern] += e.repeat_count

    # Generate prose summary
    summary = _build_prose(
        window=window,
        process=process,
        total_count=total_count,
        error_count=error_count,
        warning_count=warning_count,
        top_issues=top_issues,
        warning_groups=warning_groups,
    )

    # Cursor = timestamp of the latest entry (or now if empty)
    cursor_ts = max((e.timestamp for e in entries), default=now)

    return LogSummaryResponse(
        window=window,
        generated_at=now,
        cursor=make_cursor(cursor_ts),
        summary=summary,
        error_count=error_count,
        warning_count=warning_count,
        total_count=total_count,
        top_issues=top_issues,
    )


def _build_prose(
    window: str,
    process: str | None,
    total_count: int,
    error_count: int,
    warning_count: int,
    top_issues: list[TopIssue],
    warning_groups: dict[str, int],
) -> str:
    """Compose a natural-language summary from structured data."""
    if total_count == 0:
        subject = f"{process} had" if process else "There were"
        return f"{subject} no log entries in the last {window}."

    # Opening sentence
    subject = process or "The system"
    parts = [f"In the last {window}, {subject} logged {total_count} entries."]

    # Errors
    if error_count > 0:
        resolved = [i for i in top_issues if i.resolved]
        unresolved = [i for i in top_issues if not i.resolved]

        error_parts: list[str] = []
        if unresolved:
            descriptions = [f"{i.pattern} ({i.count}x)" for i in unresolved[:3]]
            error_parts.append(
                f"{error_count} error(s) occurred: {', '.join(descriptions)}"
            )
        if resolved:
            descriptions = [i.pattern for i in resolved[:3]]
            error_parts.append(
                f"{len(resolved)} issue(s) resolved: {', '.join(descriptions)}"
            )
        if error_parts:
            parts.append(" ".join(error_parts) + ".")
    else:
        parts.append("No errors detected.")

    # Warnings
    if warning_count > 0:
        top_warnings = sorted(warning_groups.items(), key=lambda kv: kv[1], reverse=True)[:3]
        descriptions = [f"{pat} ({cnt}x)" for pat, cnt in top_warnings]
        parts.append(f"{warning_count} warning(s): {', '.join(descriptions)}.")

    return " ".join(parts)
