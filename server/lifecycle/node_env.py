"""Which `node` each part of the system will actually run (#214).

Setup used to ask one question -- is there a `node` on *this* process's PATH --
and accepted any version. But four different places choose a `node`, and they
routinely disagree:

- **this process**: whoever ran setup or update. It builds `mcp/dist` on a git
  install.
- **a login shell**: a new Terminal window, and CLI MCP clients started from
  one. Reads `.zprofile` and `.zshrc`.
- **a non-interactive shell**: agents' tools, scripts, anything a program
  spawns. zsh reads only `.zshenv`; bash reads only `$BASH_ENV`.
- **GUI apps**: the menu-bar app, and MCP clients launched from the Dock.
  launchd's PATH, and no shell startup files at all.

fnm and nvm usually live in `.zshrc`, so a machine can be fine in a terminal and
have no `node` for a GUI client -- measured on the maintainer's machine, where
Claude Desktop's `"command": "node"` would have found nothing.

Read-only. Every external lookup is injectable, because a test that ran the
developer's real shells would be both slow and a report about the developer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The MCP wrapper's floor. Mirrors `REQUIRED_MAJOR` in `mcp/src/launcher.cjs`
#: and `engines` in `mcp/package.json`; a test keeps the three in step.
MIN_NODE_MAJOR = 22

#: launchd's default PATH for a user's GUI apps.
GUI_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

#: What the menu-bar app puts in front of it (`QuernCLI.searchPath`).
MENUBAR_EXTRA_PATH = ("{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

PROBE_TIMEOUT = 10.0

# Printed by the shell probe after anything its startup files print, so their
# output cannot be mistaken for the answer.
_MARKER = "__QUERN_NODE__"
_SHELL_PROBE = (
    f'p=$(command -v node 2>/dev/null); '
    f'if [ -n "$p" ]; then v=$("$p" --version 2>/dev/null); fi; '
    f'printf "%s\\t%s\\t%s\\n" "{_MARKER}" "$p" "$v"'
)

OK = "ok"
TOO_OLD = "too_old"
MISSING = "missing"
UNKNOWN = "unknown"   # could not look -- not the same as "no node"


@dataclass(frozen=True)
class NodeSite:
    place: str        # short name, e.g. "GUI apps"
    used_by: str      # who runs node from here
    status: str       # OK | TOO_OLD | MISSING | UNKNOWN
    path: str | None = None
    version: str | None = None
    detail: str = ""  # why UNKNOWN, or anything else worth saying

    @property
    def ok(self) -> bool:
        return self.status == OK


Runner = Callable[..., subprocess.CompletedProcess]


def major_version(version: str | None) -> int | None:
    """22 from "v22.22.2"; None when there is nothing to parse."""
    if not version:
        return None
    m = re.match(r"v?(\d+)\.", version.strip())
    return int(m.group(1)) if m else None


def _classify(path: str | None, version: str | None) -> str:
    if not path:
        return MISSING
    major = major_version(version)
    if major is None:
        # Found but would not report a version: a broken binary is not a
        # working one, and calling it OK is the false all-clear.
        return TOO_OLD
    return OK if major >= MIN_NODE_MAJOR else TOO_OLD


def _version_of(path: str, run: Runner) -> str | None:
    try:
        result = run([path, "--version"], capture_output=True, text=True,
                     timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _on_path(place: str, used_by: str, path_dirs: list[str], run: Runner,
             which: Callable[..., str | None]) -> NodeSite:
    found = which("node", path=os.pathsep.join(path_dirs))
    version = _version_of(found, run) if found else None
    return NodeSite(place, used_by, _classify(found, version), found, version)


def _in_shell(place: str, used_by: str, argv: list[str], env: dict[str, str],
              run: Runner) -> NodeSite:
    try:
        result = run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                     env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    except subprocess.TimeoutExpired:
        return NodeSite(place, used_by, UNKNOWN,
                        detail=f"the shell did not answer within {PROBE_TIMEOUT:.0f}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return NodeSite(place, used_by, UNKNOWN, detail=f"could not start the shell: {exc}")

    for line in reversed(result.stdout.splitlines()):
        if line.startswith(_MARKER):
            _, path, version = (line.split("\t") + ["", ""])[:3]
            path, version = path.strip() or None, version.strip() or None
            return NodeSite(place, used_by, _classify(path, version), path, version)
    # No marker: the startup files failed or exited early. That is "could not
    # look", and reporting it as "no node" would send someone to install one.
    return NodeSite(place, used_by, UNKNOWN,
                    detail="the shell exited before answering "
                           f"(exit {result.returncode})")


def here(
    *,
    run: Runner = subprocess.run,
    which: Callable[..., str | None] = shutil.which,
    env: dict[str, str] | None = None,
) -> NodeSite:
    """Just this process's `node`: the one that builds the MCP wrapper."""
    return _here(run, which, dict(os.environ if env is None else env))


def _here(run: Runner, which: Callable[..., str | None], env: dict[str, str]) -> NodeSite:
    return _on_path("this command", "building the MCP wrapper (git installs)",
                    env.get("PATH", "").split(os.pathsep), run, which)


def user_shell(env: dict[str, str]) -> str | None:
    """The user's shell, if it is one whose startup files we understand."""
    shell = env.get("SHELL", "")
    return shell if Path(shell).name in ("zsh", "bash") else None


def probe(
    *,
    run: Runner = subprocess.run,
    which: Callable[..., str | None] = shutil.which,
    env: dict[str, str] | None = None,
    home: str | None = None,
) -> list[NodeSite]:
    """Where each of the four places finds `node`, and whether it is usable."""
    env = dict(os.environ if env is None else env)
    home = home or env.get("HOME") or str(Path.home())
    sites = [_here(run, which, env)]

    shell = user_shell(env)
    if shell is None:
        reason = f"unsupported shell {env.get('SHELL') or '(unset)'}; zsh and bash are checked"
        sites.append(NodeSite("login shell", "Terminal, and CLI MCP clients", UNKNOWN,
                              detail=reason))
        sites.append(NodeSite("non-interactive shell", "agents' tools and scripts",
                              UNKNOWN, detail=reason))
    else:
        base = {k: env[k] for k in ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR")
                if k in env}
        base["HOME"] = home
        minimal = {**base, "PATH": os.pathsep.join(GUI_PATH)}
        sites.append(_in_shell("login shell", "Terminal, and CLI MCP clients",
                               [shell, "-lic", _SHELL_PROBE], minimal, run))
        # bash reads $BASH_ENV in a non-interactive shell; carry it through,
        # since that is the one file such a shell would consult.
        non_interactive = dict(minimal)
        if Path(shell).name == "bash" and "BASH_ENV" in env:
            non_interactive["BASH_ENV"] = env["BASH_ENV"]
        sites.append(_in_shell("non-interactive shell", "agents' tools and scripts",
                               [shell, "-c", _SHELL_PROBE], non_interactive, run))

    gui_dirs = [d.format(home=home) for d in MENUBAR_EXTRA_PATH] + list(GUI_PATH)
    sites.append(_on_path("GUI apps", "the menu-bar app, and MCP clients opened from the Dock",
                          gui_dirs, run, which))
    return sites


#: Where each installer puts node, most specific first. Matched against the
#: path as found *and* with symlinks resolved: Intel Homebrew's
#: `/usr/local/bin/node` is only recognisable from where it points.
_MANAGER_PATHS = (
    ("/.local/share/mise/", "mise"),
    ("/.asdf/", "asdf"),
    ("/.nodenv/", "nodenv"),
    ("/.volta/", "volta"),
    ("/.nvm/", "nvm"),
    ("/fnm", "fnm"),                       # fnm_multishells and share/fnm
    ("/library/pnpm/", "pnpm"),
    ("/.local/share/pnpm/", "pnpm"),
    ("/nix/store/", "nix"),
    ("/.nix-profile/", "nix"),
    ("/run/current-system/", "nix"),
    ("/opt/local/", "macports"),
    ("/opt/homebrew/", "brew"),
    ("/usr/local/cellar/", "brew"),
    ("/usr/local/opt/", "brew"),
)

#: n installs node into /usr/local/bin and keeps its versions here.
N_PREFIX = "/usr/local/n"


def manager_of(
    path: str | None,
    *,
    resolve: Callable[[str], str] = os.path.realpath,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> str | None:
    """Which tool installed this node, judged from where it lives."""
    if not path:
        return None
    try:
        real = resolve(path)
    except OSError:
        real = path
    for candidate in (path.lower(), real.lower()):
        # A versioned Homebrew formula is keg-only: upgrading `node` leaves it
        # where it is, so it gets its own advice.
        if "/opt/node@" in candidate or "/cellar/node@" in candidate:
            return "brew-keg"
        for needle, name in _MANAGER_PATHS:
            if needle in candidate:
                return name
    # A real file in /usr/local/bin is n's or the official installer's; they
    # put it in the same place, and only n keeps a versions directory.
    if real.startswith("/usr/local/bin/"):
        return "n" if is_dir(N_PREFIX) else "installer"
    return None


def upgrade_command(manager: str | None, major: int = MIN_NODE_MAJOR) -> str:
    """How to get Node `major` with the tool that installed the current one."""
    return {
        "fnm": f"fnm install {major} && fnm default {major}",
        "nvm": f"nvm install {major} && nvm alias default {major}",
        "volta": f"volta install node@{major}",
        "mise": f"mise use -g node@{major}",
        "asdf": (f"asdf install nodejs latest:{major}, then make it the default with "
                 f"`asdf set --home nodejs <version>` (`asdf global` before asdf 0.16)"),
        "nodenv": (f"nodenv install <{major}.x.y> && nodenv global <{major}.x.y> "
                   f"(`nodenv install -l | grep '^ *{major}'` lists them)"),
        "n": f"n {major} (with sudo if /usr/local is not writable)",
        "installer": f"install Node {major} from https://nodejs.org",
        "pnpm": f"pnpm env use --global {major}",
        "macports": f"sudo port install nodejs{major}",
        "nix": f"add nodejs_{major} to your Nix configuration",
        "brew": "brew upgrade node",
        "brew-keg": ("brew install node (a versioned node@ formula is not linked, "
                     "and upgrading `node` does not touch it)"),
    }.get(manager or "", "brew install node")


_NON_INTERACTIVE_FIX = {
    "fnm": 'Add `eval "$(fnm env)"` to ~/.zshenv, which every zsh reads.',
    "nvm": "Load nvm from ~/.zshenv rather than ~/.zshrc, which scripts never read.",
    "mise": ('Add `eval "$(mise activate zsh --shims)"` to ~/.zshenv. Plain '
             "`mise activate` only works in interactive shells."),
    "asdf": "Put asdf's shims directory on PATH in ~/.zshenv, not only in ~/.zshrc.",
    "volta": "Put ~/.volta/bin on PATH in ~/.zshenv, not only in ~/.zshrc.",
    "nodenv": 'Add `eval "$(nodenv init - zsh)"` to ~/.zshenv.',
}


def fix_for(site: NodeSite, sites: list[NodeSite]) -> str:
    """One actionable line for a site that is not OK."""
    if site.status == UNKNOWN:
        return site.detail
    # This node's own installer first: a too-old node is upgraded with the tool
    # that put it there, whatever the other places use.
    manager = manager_of(site.path) or next(
        (m for m in (manager_of(s.path) for s in sites) if m), None)
    upgrade = upgrade_command(manager)

    if site.status == TOO_OLD:
        return f"Node {site.version or '(unreadable)'} is below {MIN_NODE_MAJOR}. Run: {upgrade}"

    # MISSING: where the fix goes depends on the place.
    if site.place == "GUI apps":
        return ("GUI apps do not read your shell's startup files, so a version "
                "manager's node is invisible here. Install one where GUI apps look "
                "(`brew install node`, or the installer from https://nodejs.org; "
                f"not `node@{MIN_NODE_MAJOR}`, which Homebrew does not link onto "
                "PATH), or point your MCP client's \"command\" at an absolute "
                f"path to a Node {MIN_NODE_MAJOR}+ binary.")
    if site.place == "non-interactive shell":
        return _NON_INTERACTIVE_FIX.get(
            manager or "", "Put your Node setup in ~/.zshenv, which non-interactive zsh reads.")
    return f"No node found. Run: {upgrade}"
