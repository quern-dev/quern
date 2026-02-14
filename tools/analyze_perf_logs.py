#!/usr/bin/env python3
"""Performance log analyzer - parses [PERF] logs and builds timeline.

Usage:
    python tools/analyze_perf_logs.py <log_file>

Example:
    python tools/analyze_perf_logs.py perf_analysis.log

    # Or with live tail:
    tail -f server.log | grep PERF | python tools/analyze_perf_logs.py -
"""

import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class PerfEvent:
    """Parsed performance event from logs."""
    timestamp: str  # Original timestamp from log line
    component: str  # e.g., "tap_element", "get_ui_elements", "idb.describe_all"
    event_type: str  # "START", "COMPLETE", "CACHE HIT", etc.
    duration_ms: Optional[float]  # If it's a delta event (+Nms)
    details: str  # Rest of the message
    raw_line: str  # Full log line


def parse_perf_line(line: str) -> Optional[PerfEvent]:
    """Parse a [PERF] log line into a PerfEvent."""
    # Match pattern: timestamp [PERF] component: event_type details (+Nms)?
    # Example: 2024-02-13 10:23:45.123 [PERF] tap_element START (label=Profile, ...)
    # Example: 2024-02-13 10:23:45.456 [PERF] tap_element: fetching UI elements (+10.5ms)

    if '[PERF]' not in line:
        return None

    # Extract timestamp (first part before [PERF])
    parts = line.split('[PERF]', 1)
    if len(parts) != 2:
        return None

    timestamp = parts[0].strip()
    rest = parts[1].strip()

    # Try to parse: "component: event_type details" or "component event_type: details"
    # First, extract duration if present
    duration_ms = None
    duration_match = re.search(r'\+(\d+\.\d+)ms', rest)
    if duration_match:
        duration_ms = float(duration_match.group(1))

    # Split on first ": " or " START" or " COMPLETE"
    if ': ' in rest:
        component, message = rest.split(': ', 1)
    elif ' START' in rest:
        idx = rest.index(' START')
        component = rest[:idx]
        message = rest[idx+1:]
    elif ' COMPLETE' in rest:
        idx = rest.index(' COMPLETE')
        component = rest[:idx]
        message = rest[idx+1:]
    else:
        # Can't parse
        return None

    # Determine event type from message
    if message.startswith('START'):
        event_type = 'START'
    elif message.startswith('COMPLETE'):
        event_type = 'COMPLETE'
    elif 'CACHE HIT' in message:
        event_type = 'CACHE_HIT'
    elif 'EXPIRED' in message:
        event_type = 'CACHE_EXPIRED'
    elif 'subprocess' in message:
        if 'spawned' in message:
            event_type = 'SUBPROCESS_SPAWN'
        elif 'communicate' in message:
            event_type = 'SUBPROCESS_WAIT'
        elif 'returned' in message:
            event_type = 'SUBPROCESS_DONE'
        else:
            event_type = 'SUBPROCESS'
    elif 'idb returned' in message:
        event_type = 'IDB_DONE'
    elif 'JSON parsed' in message:
        event_type = 'JSON_PARSE'
    elif 'parsed' in message and 'elements' in message:
        event_type = 'PARSE_ELEMENTS'
    elif 'stability check' in message:
        event_type = 'STABILITY_CHECK'
    elif 'executing tap' in message:
        event_type = 'TAP'
    else:
        event_type = 'DETAIL'

    return PerfEvent(
        timestamp=timestamp,
        component=component.strip(),
        event_type=event_type,
        duration_ms=duration_ms,
        details=message,
        raw_line=line.strip(),
    )


def analyze_timeline(events: list[PerfEvent]) -> None:
    """Analyze and print timeline with insights."""
    print("\n" + "=" * 80)
    print("PERFORMANCE TIMELINE ANALYSIS")
    print("=" * 80)

    # Group by operation (e.g., all events for a single tap_element call)
    # We'll use START events to identify operation boundaries
    operations = []
    current_op = []

    for event in events:
        if event.event_type == 'START':
            # Start of new operation
            if current_op:
                operations.append(current_op)
            current_op = [event]
        else:
            current_op.append(event)

    if current_op:
        operations.append(current_op)

    print(f"\nFound {len(operations)} major operation(s)\n")

    # Analyze each operation
    for i, op_events in enumerate(operations, 1):
        start_event = op_events[0]
        print(f"\n{'─' * 80}")
        print(f"Operation #{i}: {start_event.component}")
        print(f"Started: {start_event.timestamp}")
        print(f"Details: {start_event.details}")
        print(f"{'─' * 80}")

        # Find COMPLETE event for this operation
        complete_event = None
        for event in op_events:
            if event.event_type == 'COMPLETE' and event.component == start_event.component:
                complete_event = event
                break

        if complete_event:
            # Extract total time from COMPLETE event
            total_match = re.search(r'total=(\d+\.\d+)ms', complete_event.details)
            if total_match:
                total_ms = float(total_match.group(1))
                print(f"\n⏱️  TOTAL TIME: {total_ms:.1f}ms\n")

        # Print timeline with cumulative time
        cumulative = 0.0
        for event in op_events[1:]:  # Skip START event
            if event.duration_ms:
                cumulative += event.duration_ms

            indent = "  "
            if event.component != start_event.component:
                indent = "    "  # Nested operation

            marker = {
                'COMPLETE': '✓',
                'CACHE_HIT': '⚡',
                'CACHE_EXPIRED': '❌',
                'SUBPROCESS_SPAWN': '🔧',
                'SUBPROCESS_WAIT': '⏳',
                'IDB_DONE': '📱',
                'JSON_PARSE': '📄',
                'PARSE_ELEMENTS': '🔍',
                'STABILITY_CHECK': '🎯',
                'TAP': '👆',
            }.get(event.event_type, '•')

            duration_str = f"+{event.duration_ms:.1f}ms" if event.duration_ms else ""
            cumulative_str = f"[{cumulative:.1f}ms]" if event.duration_ms else ""

            print(f"{indent}{marker} {event.event_type:20s} {duration_str:>12s} {cumulative_str:>12s}  {event.details[:60]}")

    # Print summary statistics
    print("\n" + "=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)

    # Count event types
    event_counts = defaultdict(int)
    event_times = defaultdict(list)

    for event in events:
        event_counts[event.event_type] += 1
        if event.duration_ms:
            event_times[event.event_type].append(event.duration_ms)

    print("\nEvent Type Counts:")
    for event_type, count in sorted(event_counts.items(), key=lambda x: -x[1]):
        avg_time = ""
        if event_times[event_type]:
            avg = sum(event_times[event_type]) / len(event_times[event_type])
            total = sum(event_times[event_type])
            avg_time = f"  (avg: {avg:.1f}ms, total: {total:.1f}ms)"
        print(f"  {event_type:25s}: {count:3d}{avg_time}")

    # Identify bottlenecks (slowest operations)
    print("\nSlowest Operations (top 10):")
    slow_ops = []
    for event in events:
        if event.duration_ms and event.duration_ms > 10:  # Only operations > 10ms
            slow_ops.append((event.duration_ms, event.component, event.event_type, event.details))

    slow_ops.sort(reverse=True)
    for duration, component, event_type, details in slow_ops[:10]:
        print(f"  {duration:8.1f}ms  {component:25s}  {event_type:20s}  {details[:40]}")

    # Cache effectiveness
    cache_hits = event_counts.get('CACHE_HIT', 0)
    cache_misses = sum(1 for e in events if 'cache' in e.details.lower() and 'miss' in e.details.lower())
    if cache_hits or cache_misses:
        total = cache_hits + cache_misses
        hit_rate = (cache_hits / total * 100) if total > 0 else 0
        print(f"\nCache Effectiveness:")
        print(f"  Hits: {cache_hits}, Misses: {cache_misses}, Hit Rate: {hit_rate:.1f}%")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python analyze_perf_logs.py <log_file>")
        print("       python analyze_perf_logs.py -  (read from stdin)")
        sys.exit(1)

    log_file = sys.argv[1]

    # Read log lines
    if log_file == '-':
        lines = sys.stdin.readlines()
    else:
        try:
            with open(log_file) as f:
                lines = f.readlines()
        except FileNotFoundError:
            print(f"Error: File not found: {log_file}")
            sys.exit(1)

    # Parse performance events
    events = []
    for line in lines:
        event = parse_perf_line(line)
        if event:
            events.append(event)

    if not events:
        print("No [PERF] events found in log file.")
        sys.exit(1)

    print(f"Parsed {len(events)} performance events from {len(lines)} log lines")

    # Analyze timeline
    analyze_timeline(events)


if __name__ == '__main__':
    main()
