"""Log entry classifier.

Provides two capabilities:
1. Noise detection — tags known iOS system chatter so it can be de-prioritized.
2. Pattern grouping — normalizes messages into templates by stripping variable
   parts (numbers, UUIDs, hex addresses) so errors can be grouped by pattern.
"""

from __future__ import annotations

import re

from server.models import LogEntry, LogLevel

# ---------------------------------------------------------------------------
# Noise patterns: common iOS system messages that are rarely actionable
# ---------------------------------------------------------------------------

NOISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"deny\(\d+\) mach-lookup", re.IGNORECASE),
    re.compile(r"deny\(\d+\) file-read-data", re.IGNORECASE),
    re.compile(r"Sandbox:.*deny", re.IGNORECASE),
    re.compile(r"AMFI:.*not valid", re.IGNORECASE),
    re.compile(r"nw_path_evaluator_evaluate", re.IGNORECASE),
    re.compile(r"TCP Conn .* event \d+", re.IGNORECASE),
    re.compile(r"TIC .* event \d+", re.IGNORECASE),
    re.compile(r"CoreData: annotation:.*", re.IGNORECASE),
    re.compile(r"Metal API Validation", re.IGNORECASE),
    re.compile(r"boringssl_", re.IGNORECASE),
    re.compile(r"SecTrust.*error", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Template normalization: strip variable parts to create grouping keys
# ---------------------------------------------------------------------------

# Order matters — UUIDs before plain hex to avoid partial matches
_TEMPLATE_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    # UUIDs: 8-4-4-4-12 hex
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),
    # Hex addresses: 0x1a2b3c
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    # IP:port
    (re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?"), "<IP>"),
    # Paths with hashes (e.g., /Application/ABC123-DEF456/MyApp)
    (re.compile(r"/[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}/"), "/<ID>/"),
    # Standalone numbers (integers and decimals), but not inside words
    (re.compile(r"(?<![a-zA-Z])\d+(\.\d+)?(?![a-zA-Z])"), "<N>"),
]


def extract_pattern(message: str) -> str:
    """Normalize a log message into a template for grouping.

    Strips numbers, UUIDs, hex addresses, and IPs so that messages differing
    only in variable parts produce the same template string.
    """
    result = message
    for pattern, replacement in _TEMPLATE_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    # Collapse repeated <N> separated by common delimiters
    result = re.sub(r"(<N>[,.\s]*)+", "<N> ", result)
    return result.strip()


def is_noise(entry: LogEntry) -> bool:
    """Check whether an entry matches a known iOS noise pattern."""
    for pattern in NOISE_PATTERNS:
        if pattern.search(entry.message):
            return True
    return False


def detect_resolution(entries: list[LogEntry], process: str | None = None) -> list[dict]:
    """Detect error→success resolution sequences in a list of entries.

    Looks for patterns where errors from a process are followed by a
    success-indicating message (containing keywords like "succeeded",
    "resolved", "connected", "refreshed", "recovered").

    Returns a list of resolution dicts with error_pattern, resolved_at, and
    resolution_message.
    """
    success_keywords = re.compile(
        r"\b(succeeded|successfully|success|resolved|connected|refreshed|recovered|completed)\b",
        re.IGNORECASE,
    )

    error_levels = set(LogLevel.at_least(LogLevel.ERROR))

    # Collect errors by process
    active_errors: dict[str, list[LogEntry]] = {}
    resolutions: list[dict] = []

    for entry in entries:
        if process and entry.process != process:
            continue

        if entry.level in error_levels:
            key = f"{entry.process}:{extract_pattern(entry.message)}"
            active_errors.setdefault(key, []).append(entry)
        elif success_keywords.search(entry.message) and active_errors:
            # Check if this success message is from a process with active errors
            resolved_keys = [
                k for k in active_errors if k.startswith(f"{entry.process}:")
            ]
            for key in resolved_keys:
                error_entries = active_errors.pop(key)
                resolutions.append({
                    "error_pattern": key.split(":", 1)[1],
                    "error_count": sum(e.repeat_count for e in error_entries),
                    "first_error": error_entries[0].timestamp.isoformat(),
                    "resolved_at": entry.timestamp.isoformat(),
                    "resolution_message": entry.message,
                })

    return resolutions
