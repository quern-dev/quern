"""Recover when an older release's updater imports this release's code.

Up to 0.18.3, `quern update` replaced the source tree inside the process that
was running the old code and then kept importing from it. Modules it had loaded
before the swap stayed old; modules it loaded afterwards were new. The first
time a new module asked an old one for a name added since, the update crashed
-- 0.18.3's `setup.py` importing `quern_cmd` from a 0.18.2 `server.config` did
exactly that, after the pull and before setup or the restart (#212).

From 0.18.4 the updater hands everything after the swap to a fresh process, so
this cannot happen to an update *from* 0.18.4. But a user on 0.18.3 or older
updates with their own updater, which will import this release's files the old
way. This module is how those files cope: it reloads, from disk, the modules
such a process loaded before the swap.

Deliberately free of `server` imports. It is loaded after the swap, so it is
always the new copy, and it must not itself depend on anything stale.
"""

from __future__ import annotations

import importlib
import sys

#: What a `quern update` process from 0.18.1-0.18.3 has imported before it
#: swaps the tree, measured by importing its updater from each release. Each
#: imports only the standard library apart from `server.config`, so reloading
#: them has no side effects beyond re-reading their own source. `server.config`
#: comes first because the others import from it.
PRELOADED_BY_OLD_UPDATERS = (
    "server.config",
    "server.lifecycle.invocation",
    "server.lifecycle.update_check",
)

#: Defined only by updaters that hand off to a fresh process. Its absence is how
#: an in-process updater from an older release is recognised.
HANDOFF_MARKER = "finish_update"


def running_under_old_updater() -> bool:
    """True inside a pre-0.18.4 `quern update` that has swapped the tree."""
    updater = sys.modules.get("server.lifecycle.updater")
    return updater is not None and not hasattr(updater, HANDOFF_MARKER)


def refresh_if_stale() -> list[str]:
    """Reload the modules an old updater loaded before the swap.

    Returns the names reloaded. A no-op in every other process, including the
    finishing process of a current update, whose modules were loaded after the
    swap and are already current.

    A module that fails to reload is left as it was: the import that follows
    then fails exactly as it would have without this, which is no worse, and
    the traceback names the real problem rather than this helper.
    """
    if not running_under_old_updater():
        return []
    reloaded = []
    for name in PRELOADED_BY_OLD_UPDATERS:
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            importlib.reload(module)
        except Exception:  # noqa: BLE001 -- see docstring
            continue
        reloaded.append(name)
    return reloaded
