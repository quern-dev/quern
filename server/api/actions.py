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

import contextlib
import logging
import time

from fastapi import HTTPException

from server import logging_ext

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
def action(name: str, *, category: str = "device.action"):
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


