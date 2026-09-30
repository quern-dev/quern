"""Whether each MCP client quern is registered with can start its wrapper (#214).

`mcp-install` registers an absolute Node 22+, but a registration outlives the
node it names -- `nvm uninstall`, a Homebrew cleanup -- and older ones say plain
`node`, which an app opened from the Dock resolves on launchd's PATH and finds
nothing. The client then reports only `CONNECTION_CLOSED`, and nothing in
quern's own output ever mentioned it.

This module decides, per registration, whether it will fail, and writes the
failures to `~/.quern/mcp-clients.json` for the menu-bar app, which cannot run
the checks itself and is the only thing a GUI user looks at. `quern doctor`
reports the same assessment in full.

Only failures go in the file, and only ones this can know. Plain `node` is not
one: each client resolves it its own way -- Claude Desktop reads your shell's
PATH and adds every nvm version, measured from its own log -- so whether it
finds a good node cannot be judged from here, and calling it broken was a false
alarm on a machine where it worked. `quern doctor` warns about it instead. Nor
is a check that could not be made: a menu warning that is sometimes wrong is one
people learn to ignore.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from server.config import CONFIG_DIR, quern_cmd
from server.lifecycle import node_env

logger = logging.getLogger(__name__)

STATE_FILE = CONFIG_DIR / "mcp-clients.json"

#: Display names, for a person reading a menu rather than a config file.
NAMES = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "codex": "Codex",
}

OK = "ok"
PLAIN = "plain_node"        # `node`, resolved by each client its own way
LAUNCHER_GONE = "launcher_gone"  # the wrapper it starts is not there
TILDE = "tilde"             # a `~` path, which clients exec unexpanded
GONE = "gone"
TOO_OLD = "too_old"
UNUSABLE = "unusable"       # ran, but reported no Node version
NO_COMMAND = "no_command"
UNKNOWN = "unknown"         # the check could not be made
UNREADABLE = "unreadable"   # the config could not be read


@dataclass(frozen=True)
class Assessment:
    registration: object     # setup.McpRegistration
    status: str
    version: str | None = None
    fails: bool = False      # whether this client will not start the wrapper
    reason: str = ""         # a sentence for the menu, when it fails
    fix: str = ""            # what to do about it


def _name(reg) -> str:
    name = NAMES.get(reg.client, reg.client)
    return f"{name} ({reg.project})" if reg.project else name


def fix_for(reg) -> str:
    if reg.project:
        # `mcp-install` writes the user-wide entry, which this one overrides
        # inside its project -- so re-registering cannot fix it.
        return (f"edit the command of `quern-debug` under projects[\"{reg.project}\"]"
                f".mcpServers in {reg.config}, or remove that entry so the user-wide "
                f"one applies; `{quern_cmd()} mcp-install` does not write project entries")
    return (f"`{quern_cmd()} mcp-install {reg.client}` registers an absolute Node "
            f"{node_env.MIN_NODE_MAJOR}+")


def assess(registrations: list, *,
           check: Callable[..., tuple[str, str | None]] | None = None,
           exists: Callable[[str], bool] = os.path.exists) -> list[Assessment]:
    """One assessment per registration."""
    check = check or node_env.check_outside_a_shell

    out = []
    for reg in registrations:
        name, node = _name(reg), reg.node
        launcher = getattr(reg, "launcher", None)
        if reg.error:
            out.append(Assessment(reg, UNREADABLE))
        elif not node:
            out.append(Assessment(reg, NO_COMMAND, fails=True, fix=fix_for(reg),
                                  reason=f"{name}'s registration has no command"))
        elif node.startswith("~"):
            out.append(Assessment(reg, TILDE, fails=True, fix=fix_for(reg),
                                  reason=f"{name} is set to run {node}, and apps do not "
                                         f"expand `~`"))
        elif launcher and os.path.isabs(launcher) and not exists(launcher):
            # Quern moved, was reinstalled, or the clone it was registered from
            # is gone: the node is fine and the wrapper it is told to run is not.
            out.append(Assessment(reg, LAUNCHER_GONE, fails=True, fix=fix_for(reg),
                                  reason=f"{name} is set to start Quern from {launcher}, "
                                         f"which no longer exists"))
        elif not os.path.isabs(node):
            out.append(Assessment(reg, PLAIN, fix=fix_for(reg)))
        elif not exists(node):
            out.append(Assessment(reg, GONE, fails=True, fix=fix_for(reg),
                                  reason=f"{name} is set to run {node}, which no longer "
                                         f"exists"))
        else:
            status, version = check(node)
            if status == node_env.OK:
                out.append(Assessment(reg, OK, version))
            elif status == node_env.UNKNOWN:
                out.append(Assessment(reg, UNKNOWN))
            elif status == node_env.TOO_OLD:
                out.append(Assessment(
                    reg, TOO_OLD, version, fails=True, fix=fix_for(reg),
                    reason=f"{name} runs {node}, which is Node {version}; Quern needs "
                           f"{node_env.MIN_NODE_MAJOR} or later"))
            else:
                out.append(Assessment(
                    reg, UNUSABLE, fails=True, fix=fix_for(reg),
                    reason=f"{name} runs {node}, which did not report a Node version "
                           f"when run the way an app runs it"))
    return out


def state(assessments: list[Assessment], *, now: datetime | None = None) -> dict:
    """What the menu bar reads: the failures, and which clients `mcp-install`
    can fix (a project entry it cannot, so it is not listed)."""
    failing = [a for a in assessments if a.fails]
    clients = []
    for a in failing:
        if not a.registration.project and a.registration.client not in clients:
            clients.append(a.registration.client)
    return {
        "checked_at": (now or datetime.now(UTC)).isoformat(),
        # `fixable`: whether `mcp-install` fixes it, which a project entry it
        # does not -- so the menu bar knows which fixes to spell out.
        "problems": [{"client": _name(a.registration), "reason": a.reason, "fix": a.fix,
                      "fixable": not a.registration.project}
                     for a in failing],
        "fix_clients": clients,
    }


def write(data: dict, path: Path | None = None) -> None:
    """Atomically, so the menu bar never reads half a file."""
    path = path or STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per process: the server's own refresh and an `mcp-install` from Fix in
    # Terminal can run at once, and a shared name let one truncate the file
    # the other was about to rename -- a partial file reads as no problems.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def refresh(path: Path | None = None) -> list[Assessment] | None:
    """Assess every registration and write the result. None if it could not
    be done. The old file is left alone then: it may say "all fine" from
    before, which is what an empty file would say too -- but clearing a
    problem nobody fixed would say "fixed", which is worse."""
    from server.lifecycle import setup

    try:
        assessments = assess(setup.mcp_registrations())
        write(state(assessments), path)
    except Exception as exc:  # noqa: BLE001 -- a startup side job must not fail startup
        logger.warning("Could not check the MCP client registrations: %s", exc)
        return None
    return assessments
