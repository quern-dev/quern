"""One log entry per completed quern action, for the API layer.

The spine of the combined trace. Every handler that does something on a
caller's behalf wraps it in `action(...)`, and exactly one entry is written
when the block ends -- however it ends, because a failure is the entry most
worth having.

What counts as an action is decided by *who invoked it*, not by which module
it lives in: a thing quern did for a caller is an action, while the same work
reached from a terminal by someone running `quern setup` is not -- they are
watching it happen.

See docs/proposals/logging-spec.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import HTTPException

from server import logging_ext
from server.logging_ext import (
    current_action,
    reset_current_action,
    set_current_action,
)

logger = logging.getLogger(__name__)

class ActionScope:
    """Collects what an action entry needs while the action is still running.

    The outcome and the resolved udid are not known until the work is done, so
    the handler fills them in and the context manager emits once, at the end.
    Exactly once: a START line plus a SUCCESS line makes a trace twice as long
    as the thing it describes, which is what `[PERF]` did.
    """

    __slots__ = ("name", "category", "udid", "outcome", "detail", "_start")

    def __init__(self, name: str, category: str) -> None:
        self.name = name
        self.category = category
        self.udid = ""
        self.outcome = "ok"
        self.detail = ""
        self._start = time.perf_counter()

    @property
    def duration_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)


@contextlib.contextmanager
def action(
    name: str, *, category: str = "device.action",
) -> Iterator[ActionScope]:
    """Emit one action entry when the block ends, however it ends.

    A failure is still an action that happened, and it is the one most worth
    having in a trace -- so the entry is emitted from `finally`, not from the
    success path.
    """
    scope = ActionScope(name, category)
    # The begin entry exists for one case the completion entry cannot cover:
    # an action that starts and never finishes. On a hang, a crash, or a
    # client that disconnects mid-sweep there is no completion entry at all,
    # and without this the trace simply shows nothing happened.
    #
    # It is DEBUG so the default trace stays one line per action -- turn the
    # level up (QUERN_LOG_LEVEL=debug, or `quern start -v`) and the pairs come
    # back, categorised, so `category=device.action` returns both halves.
    logging_ext.debug(
        logger, "%s started", name, category=category,
        extra_fields={"quern_action": name, "quern_outcome": "started"},
    )
    try:
        yield scope
    except HTTPException as exc:
        # A 404 from a find-style call is an answer, not a fault: the element
        # genuinely was not there. Anything else is a failure.
        scope.outcome = "not_found" if exc.status_code == 404 else "failed"
        raise
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so `except Exception` misses it
        # and the entry would be written as `ok` for work that was abandoned
        # part-way. That is not hypothetical here: `_run_until_client_leaves`
        # cancels deliberately when the caller disconnects, which is the
        # common case, and reporting it as success is the exact bug the
        # action log exists to expose (CodeRabbit, #253).
        scope.outcome = "cancelled"
        raise
    except Exception:
        scope.outcome = "failed"
        raise
    finally:
        logging_ext.action(
            logger,
            scope.name,
            category=scope.category,
            udid=scope.udid,
            outcome=scope.outcome,
            duration_ms=scope.duration_ms,
            detail=scope.detail,
        )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UI inspection & interaction
# ---------------------------------------------------------------------------




def logged_action(
    name: str, *, category: str = "device.action",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator form, for handlers too long to wrap in a `with` block.

    Deliberately a decorator and not middleware. It wraps the endpoint
    *function*, so it sees the real exception and the real return value, it is
    opt-in per route rather than guessing from a status code, and it never
    touches `send`/`receive` -- which is what streaming endpoints and the
    disconnect guard depend on.

    The handler names its device with `current_action().udid = resolved`.
    """
    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                with action(name, category=category) as scope:
                    token = set_current_action(scope)
                    try:
                        return await fn(*args, **kwargs)
                    finally:
                        reset_current_action(token)
            return wrapper

        # A sync handler is rarer here but FastAPI accepts them, and
        # `await`ing one raises. Every route decorated today is async, so this
        # branch is a guard against the next one rather than a fix for a
        # current bug -- a silent TypeError at request time is a bad way to
        # find out.
        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with action(name, category=category) as scope:
                token = set_current_action(scope)
                try:
                    return fn(*args, **kwargs)
                finally:
                    reset_current_action(token)
        return sync_wrapper
    return decorate
