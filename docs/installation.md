# Installing Quern

The one-line install in the [README](../README.md#install) covers most people. This page has the rest: what setup installs besides the server, how to install by hand or without a terminal, and how to remove it all again.

## What the installer does

This downloads the latest release to `~/.local/share/quern`, creates a virtualenv, installs dependencies, checks system tools, registers the MCP server with Claude Code, installs a Claude Code pre-commit checklist hook (see below), and adds `quern` to `~/.local/bin` on your PATH.

**After this, run `quern` from anywhere** — you do not need to be in the install directory, and you do not need to know where it is. Everything below is written that way. If your shell reports `command not found: quern` in the session you installed from, run `rehash` (zsh) or open a new terminal: the shell caches where commands live and does not notice a new one appearing.

### Pre-commit checklist hook

Setup also installs a Claude Code `PreToolUse` hook into `~/.claude/settings.json` that surfaces a short checklist whenever an agent runs `git commit` in a project that uses Quern (signaled by a `.quern/knowledge/` directory at the project root). The reminder covers KB drift, landmark verification, and other discipline that's easy to forget when committing app or KB changes. It stays silent in projects that don't use Quern.

To re-install or refresh the hook (for example after updating Quern), run `quern install-precommit-hook`. To disable it, remove the matching entry from `~/.claude/settings.json` under `hooks.PreToolUse`.

## Manual install

Download the latest release tarball from [GitHub Releases](https://github.com/quern-dev/quern/releases/latest), then:

```bash
mkdir -p ~/.local/share
tar -xzf quern-*.tar.gz -C ~/.local/share
mv ~/.local/share/quern-* ~/.local/share/quern
cd ~/.local/share/quern
./quern setup
./quern mcp-install
```

`./quern` here because you are standing in the extracted directory and there is
nothing on your PATH yet. Setup is what puts `quern` there; afterwards, run it
from anywhere as `quern`.

## Unattended setup

```bash
quern setup -y
quern setup --yes          # the same thing, spelled out
```

`-y` (or `--yes`) answers setup's prompts with their default instead of asking,
for a provisioning script, a Dockerfile, or anything else with no terminal.

Without it and with no terminal to ask on, setup still completes the install
itself — the virtualenv, the dependencies, the MCP wrapper and the `quern`
command — and declines the optional extras it would otherwise have asked about,
listing them at the end.

Five prompts are not covered by `-y`, and are listed at the end of the run:

| Prompt | Why `-y` leaves it |
|---|---|
| Install the capture CA into booted simulators | A certificate authority outlives the session that wanted it and has to be removed deliberately. Answer it once with `quern set-auto-install-cert`. |
| Install the tunneld LaunchDaemon | Runs as root at boot and survives reboots. Needs sudo. |
| `sudo pipx install --global pymobiledevice3` | Writes outside your home directory. Needs sudo. |
| Disable the macOS crash reporter dialog | A user-wide setting that `quern uninstall` does not put back. |
| Open the Xcode Command Line Tools installer | Hands off to a macOS dialog someone has to click. |

Three of those need a sudo password or a click that no flag can supply, so
answering yes on your behalf would leave the run waiting on something that
never comes. The other two outlive Quern itself.

## Uninstall

```bash
quern uninstall
```

Removes Homebrew packages, pipx packages, MCP registrations, the wrapper script, and the virtualenv. Only removes packages that were installed by `quern setup` — pre-existing tools (like Node.js) are left untouched. The source code directory is preserved; the command prints instructions to remove it if desired.
