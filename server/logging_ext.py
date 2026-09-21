"""Structured fields on quern's own log entries.

`LogEntry` already carried `category` and nothing on the server side populated
it, so every server entry arrived with `category=""` and "show me device
actions" was not a question anyone could ask. (The `/logs/query` endpoint could
not filter on it either -- the predicate existed only on the SSE stream. Both
halves were fixed together.)

This is the whole mechanism: standard `logging`, an `extra=` dict, and a
handler that reads it back out. No new transport, no parallel logger.

See docs/proposals/logging-spec.md for the category vocabulary and the level
policy. The short version: `category` is *what quern was doing*, while
`LogEntry.source` is *who produced the entry*. They collide by name -- there
is both a `proxy` category and a `LogSource.PROXY` -- and they mean different
things.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Final

#: The closed category vocabulary. A call site that does not fit one of these
#: is a reason to change this tuple in review, not to invent a string: a
#: category nobody else uses cannot be filtered on by anyone who does not
#: already know it exists.
CATEGORIES: Final[tuple[str, ...]] = (
    "device.action",      # any write to a device or an app on it
    "device.read",        # any read of device or app state
    "device.lifecycle",   # boot, shutdown, erase, resolve, claim, input repair
    "proxy",              # proxy control, certs, bypass, intercepts, flows
    "logs",               # log-stream control, filters, queries, plist watch
    "media",              # screenshot timeline, live preview, video
    "build",              # build orchestration and output parsing
    "knowledge",          # landmarks, screen identification, app knowledge
    "server.lifecycle",   # startup, shutdown, port reclaim, updates
)

#: `extra=` keys are copied onto the LogRecord, and `Logger.makeRecord` raises
#: KeyError for anything that collides with an existing attribute. `process`
#: collides, and `category` does not -- but prefixing everything keeps us clear
#: of that list as it grows, and makes the call sites greppable.
_PREFIX: Final[str] = "quern_"

#: What an action can have done. `not_found` and `ambiguous` are answers, not
#: failures -- an element that is not on screen is a true result for the
#: question asked, and logging it as an error trains the reader to ignore
#: errors.
#: `started` is the odd one out: it marks a *begin* entry, which carries no
#: duration because the action has not finished. It exists so that an action
#: that hangs still leaves a trace -- the completion entry never arrives, so
#: without this the trace shows nothing happened at all. Begin entries are
#: DEBUG, so the default trace stays one line per action.
#: `suspect` is the level policy's WARNING row as an outcome: quern did what
#: was asked and the result should not be trusted. Typing that reports success
#: into a field that is still empty is the case it was added for; a tap into a
#: device whose input services were taken is the same shape. It is distinct
#: from `failed`, which means the caller did not get what they asked for at
#: all, and logging it as an error would train the reader to ignore errors.
#: `cancelled` is distinct from `failed` on purpose. Quern cancels work when
#: the client disconnects -- that is what `_run_until_client_leaves` is for --
#: and an abandoned sweep is not a broken one. It may also be half-applied,
#: since input is not idempotent, so it is reported at WARNING rather than
#: INFO: something happened and the result is not to be trusted.
OUTCOMES: Final[tuple[str, ...]] = (
    "ok", "failed", "suspect", "cancelled", "not_found", "ambiguous", "started",
)


#: Which level an outcome is reported at. Anything absent is INFO.
_LEVEL_FOR_OUTCOME: Final[dict[str, int]] = {
    "failed": logging.ERROR,
    "suspect": logging.WARNING,
    "cancelled": logging.WARNING,
}


def log(
    logger: logging.Logger,
    level: int,
    msg: str,
    *args: object,
    category: str,
    udid: str | None = None,
    extra_fields: dict[str, object] | None = None,
    **kwargs: object,
) -> None:
    """Log with a category attached, so the entry can be filtered on later.

    `category` is keyword-only and required. A default would mean the
    uncategorised call is the convenient one, which is how the field ended up
    empty everywhere to begin with.
    """
    if category not in CATEGORIES:
        # Raising here would turn a logging mistake into an outage, and this
        # runs on paths that are already reporting a failure. Recording the
        # bad value keeps the entry and makes the mistake visible.
        logger.warning(
            "Unknown log category %r -- see server/logging_ext.CATEGORIES", category,
        )
    extra: dict[str, object] = {f"{_PREFIX}category": category}
    if udid:
        extra[f"{_PREFIX}udid"] = udid
    if extra_fields:
        extra.update(extra_fields)
    logger.log(level, msg, *args, extra=extra, **kwargs)  # type: ignore[arg-type]


def info(logger: logging.Logger, msg: str, *args: object, **kwargs: object) -> None:
    """One thing quern did that the user asked for."""
    log(logger, logging.INFO, msg, *args, **kwargs)  # type: ignore[arg-type]


def warning(logger: logging.Logger, msg: str, *args: object, **kwargs: object) -> None:
    """Quern did what was asked, and the result is probably not what the
    caller wanted -- a tap accepted by a device that discards it, say."""
    log(logger, logging.WARNING, msg, *args, **kwargs)  # type: ignore[arg-type]


def error(logger: logging.Logger, msg: str, *args: object, **kwargs: object) -> None:
    """The operation failed; the caller did not get what they asked for."""
    log(logger, logging.ERROR, msg, *args, **kwargs)  # type: ignore[arg-type]


def debug(logger: logging.Logger, msg: str, *args: object, **kwargs: object) -> None:
    """Only useful when reproducing a specific bug."""
    log(logger, logging.DEBUG, msg, *args, **kwargs)  # type: ignore[arg-type]


def action(
    logger: logging.Logger,
    action: str,
    *,
    category: str,
    udid: str,
    outcome: str,
    duration_ms: int,
    detail: str = "",
    started_monotonic: float | None = None,
) -> None:
    """One entry per completed action -- the spine of the combined trace.

    Emitted from the API handler rather than the controller: the handler is
    the boundary that knows the outcome, already measures the duration, and
    is one layer. Controller methods call each other, so emitting there would
    double-count a single user-visible operation.

    `udid` must be the **resolved** device, not what the caller asked for. A
    trace keyed on the empty string because the caller omitted a udid does
    not join to anything, and "which device did this actually go to" is a
    question we have had to answer by hand more than once.
    """
    if outcome not in OUTCOMES:
        logger.warning(
            "Unknown action outcome %r -- see server/logging_ext.OUTCOMES", outcome,
        )
    message = f"{action} {outcome} in {duration_ms}ms"
    if udid:
        message += f" on {udid[:8]}"
    if detail:
        message += f" -- {detail}"
    log(
        logger,
        # An action that failed is an ERROR; one whose result is not to be
        # trusted is a WARNING; one that simply found nothing is neither --
        # `not_found` is an answer to the question that was asked.
        _LEVEL_FOR_OUTCOME.get(outcome, logging.INFO),
        "%s",
        message,
        category=category,
        udid=udid,
        extra_fields={
            f"{_PREFIX}action": action,
            f"{_PREFIX}outcome": outcome,
            f"{_PREFIX}duration_ms": duration_ms,
            f"{_PREFIX}started_monotonic": started_monotonic,
        },
    )


def action_of(record: logging.LogRecord) -> str:
    """The action name a record carries, or ""."""
    return getattr(record, f"{_PREFIX}action", "") or ""


def outcome_of(record: logging.LogRecord) -> str:
    """The outcome a record carries, or ""."""
    return getattr(record, f"{_PREFIX}outcome", "") or ""


def duration_ms_of(record: logging.LogRecord) -> int | None:
    """The duration a record carries, or None."""
    return getattr(record, f"{_PREFIX}duration_ms", None)


def started_monotonic_of(record: logging.LogRecord) -> float | None:
    """When the action began, on time.monotonic(), or None."""
    return getattr(record, f"{_PREFIX}started_monotonic", None)


def category_of(record: logging.LogRecord) -> str:
    """The category a record carries, or "" -- the shape the handler needs."""
    return getattr(record, f"{_PREFIX}category", "") or ""


def udid_of(record: logging.LogRecord) -> str:
    """The resolved udid a record carries, or ""."""
    return getattr(record, f"{_PREFIX}udid", "") or ""


#: The action being recorded on this task, so the place that *decides* a
#: device can record it without every handler threading a parameter out.
#: A ContextVar rather than a global: requests interleave on one event loop,
#: and two concurrent boots would overwrite each other's device.
_CURRENT: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "quern_current_action", default=None,
)


class _NoAction:
    """Stand-in when nothing is recording, so call sites need no guard.

    Assigning a field on this is deliberately a no-op rather than an error:
    `resolve_udid` runs on paths with no action in progress -- from a test,
    from startup -- and must not care.
    """

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        return


_NO_ACTION = _NoAction()


def current_action():
    """The action being recorded, or a no-op stand-in."""
    return _CURRENT.get() or _NO_ACTION


def set_current_action(scope: object):
    """Record the action for this task. Returns the token to reset with."""
    return _CURRENT.set(scope)


def reset_current_action(token) -> None:
    _CURRENT.reset(token)
