"""Who asked, as distinct from what is possible.

Two questions get tangled whenever a command needs a terminal, and keeping them
apart is the whole point of this module:

* *Can I prompt?* is a capability. It is answered by looking for a terminal,
  cannot be forgotten by a caller, and is right for callers nobody enumerated
  -- CI, cron, a shell pipeline, an editor's task runner.
* *Who called?* is an identity. Only the caller knows, so the caller says so.

Identity must never decide capability. A caller that forgets to identify itself
still cannot prompt, and `quern update --tools | tee log` has no tty while the
user sits in front of one. Identity is for *wording*: "there is no terminal to
ask on" is true and useless to someone who clicked a menu item, and the right
answer for a CI runner is different again.

Environment variable rather than a flag, following the convention already here
-- `server/api/system.py` sets `QUERN_UPDATE_TRIGGERED_BY=api` when an update
comes in over HTTP.
"""

from __future__ import annotations

import os

INVOKED_BY = "QUERN_INVOKED_BY"

#: Known callers. Anything else is treated as unknown, which is the safe
#: reading: advice aimed at the wrong audience is worse than none.
MENUBAR = "menubar"


def invoked_by() -> str | None:
    """The caller's own name for itself, or None when it did not say."""
    value = os.environ.get(INVOKED_BY, "").strip()
    return value or None


def run_it_yourself(command: list[str]) -> list[str]:
    """How to tell this caller's user to run `command` themselves.

    Returned as lines rather than printed, so a caller can put them in an alert
    as easily as on a terminal.
    """
    rendered = " ".join(command)
    if invoked_by() == MENUBAR:
        # Someone who clicked a menu item needs to be told where to go, not
        # that the place they are is unsuitable.
        return ["Open a terminal and run:", f"    {rendered}"]
    if invoked_by() is None:
        # An unidentified non-interactive caller is most likely a script or CI.
        # Bare command, no advice about terminals it may not have.
        return [rendered]
    return ["Run this yourself:", f"    {rendered}"]
