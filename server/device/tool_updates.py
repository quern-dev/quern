"""Which install sites are behind, and what it would take to move them.

`tool_versions.collect_sites` answers what is installed. What to do about it
turned out to be a different question with a different shape, so it lives here.

Three findings set the design.

**The floor is not the driver.** Every floor quern declares is a `>=`, and on
the machine this was written on not one was load-bearing -- pymobiledevice3 sat
three majors above its own floor. A floor answers "is this broken", which is
almost never true, so a floor-driven updater sits silent while tools age
indefinitely. Latest is the driver here; a floor only escalates an offer into a
requirement.

**How a tool updates is a property of where it came from, not of its name.**
adb is left alone because it lives in Android Studio's SDK and node because fnm
hands it out -- not because either is named in a list. The same tool installed
from brew on another machine is offered normally. An earlier sketch hardcoded
the exceptions by name and would have been wrong on the next machine.

**Arriving as a dependency is a reason to leave a tool alone.** brew records
`installed_as_dependency`, and upgrading such a formula directly can be undone
by whatever pulled it in. Those are reported but not offered -- unless they are
below a floor, where staying put is not an option either.

Nothing here changes anything. `plan_updates` returns intent; running it is the
caller's decision, and `quern update` asks first.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from server.device.tool_versions import ToolSite, _run, upgrade_note

# Sources quern knows how to move. Anything else is somebody else's to update,
# and the reason matters more than the fact -- a reader who is told only "not
# managed" goes looking for a quern setting that does not exist.
UNMANAGED_REASONS = {
    "fnm": "fnm manages node; quern does not change node versions",
    "android-sdk": "Android Studio's SDK Manager owns this copy",
    "xcode": "ships inside Xcode; update Xcode instead",
    "system": "part of the OS image",
    "unknown": "quern cannot tell how this was installed",
}

# Floors for CLI sites. Deliberately empty, and that is the finding rather than
# an omission: no CLI version boundary has ever been tested against quern, and
# inventing one here would repeat the mistake pyproject.toml was just annotated
# to stop making. Add an entry only with a comment saying what breaks below it
# and at which versions that was verified.
#
#   ("pymobiledevice3", "cli"): "10.0",   # example shape -- do not add unverified
CLI_FLOORS: dict[tuple[str, str], str] = {}

# Actions, most urgent first. Ordering is a property of the data, so callers
# that sort by it cannot disagree about which is worse.
ACTION_ORDER = ("upgrade_required", "upgrade_available", "current", "unmanaged", "unknown")


@dataclass
class ToolUpdate:
    """What should happen to one install site, and why."""

    name: str
    role: str
    action: str
    """One of ACTION_ORDER."""

    current: str | None = None
    latest: str | None = None
    source: str = "unknown"
    """Where the site came from -- this is what decided the action."""

    floor: str | None = None
    command: list[str] = field(default_factory=list)
    """Exactly what would be run. Empty when there is nothing to run."""

    reason: str | None = None
    """Why this action, in a form a reader can act on."""

    note: str | None = None
    """Provenance context -- what else an upgrade would touch."""

    @property
    def actionable(self) -> bool:
        return self.action in ("upgrade_required", "upgrade_available")

    def describe(self) -> str:
        head = f"{self.name} ({self.role})"
        if self.action == "current":
            return f"{head} {self.current} — up to date"
        if self.action in ("unmanaged", "unknown"):
            return f"{head} {self.current or '?'} — {self.reason}"
        return f"{head} {self.current} → {self.latest}"


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    """A comparable form of a dotted version, or None if it is not one.

    Pre-release suffixes are dropped rather than ordered. The comparison here
    only ever decides "is there something newer", and getting 1.0.0-rc1 versus
    1.0.0 subtly wrong is not worth a dependency on `packaging`, which quern
    does not declare (it is present transitively, which is not the same thing).
    """
    if not version:
        return None
    head = version.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for chunk in head.split("."):
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return tuple(parts) or None


def is_behind(current: str | None, target: str | None) -> bool:
    """True only when both parse and current is genuinely lower.

    Unparseable versions read as "not behind" on purpose: offering an upgrade
    off a version nobody could compare is how a tool gets bumped for no reason.
    """
    a, b = version_tuple(current), version_tuple(target)
    if a is None or b is None:
        return False
    return a < b


async def pypi_latest(package: str) -> str | None:
    """The newest release on PyPI, or None if it cannot be determined."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"https://pypi.org/pypi/{package}/json")
            if response.status_code != 200:
                return None
            return (response.json().get("info") or {}).get("version")
    except Exception:
        # Offline is a normal state for this call. A version check must never
        # be the reason `quern update` fails.
        return None


async def brew_outdated() -> dict[str, str] | None:  # noqa: D401
    """Formula name -> newest version for outdated formulae, or None if unasked.

    One call for every formula rather than one per tool: `brew outdated` is slow
    enough that per-tool invocations dominated the whole update step. A formula
    absent from the result is up to date, which is why this returns a mapping
    rather than answering per name.

    The None is load-bearing. An empty mapping means "brew checked and nothing
    is outdated"; None means "brew could not be asked". Collapsing the two --
    which the first version did -- reports every formula as current on a machine
    with no brew installed, which is a false all-clear rather than a missing
    answer.
    """
    code, stdout = await _run(["brew", "outdated", "--json=v2"], timeout=60)
    if code != 0:
        return None
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    # Casks as well as formulae: `adb` arrives from the android-platform-tools
    # cask, and reading only `formulae` meant an outdated cask was reported as
    # up to date. Their version key differs -- casks carry `current_version`
    # under the same name but are listed separately -- so both arrays are read.
    latest: dict[str, str] = {}
    for key in ("formulae", "casks"):
        for entry in payload.get(key) or []:
            name = entry.get("name")
            current = entry.get("current_version")
            if isinstance(name, list):          # casks report a list of tokens
                name = name[0] if name else None
            if name and current:
                latest[name] = current
    return latest


LatestFn = Callable[[str], Awaitable[str | None]]


async def plan_updates(
    sites: list[ToolSite],
    *,
    pypi: LatestFn | None = None,
    brew: Callable[[], Awaitable[dict[str, str] | None]] | None = None,
) -> list[ToolUpdate]:
    """What to do about every install site, without doing any of it.

    `pypi` and `brew` are injected so this is testable with no network and no
    homebrew -- the same shape `probe_container` uses for `describe_point`.
    """
    pypi = pypi or pypi_latest
    brew = brew or brew_outdated

    # Only pay for the brew query if something actually came from brew.
    brew_latest: dict[str, str] | None = None
    if any(site.source == "brew" and site.available for site in sites):
        brew_latest = await brew()

    updates: list[ToolUpdate] = []
    for site in sites:
        updates.append(await _plan_one(site, pypi, brew_latest))
    return updates


async def _plan_one(
    site: ToolSite, pypi: LatestFn, brew_latest: dict[str, str] | None,
) -> ToolUpdate:
    floor = CLI_FLOORS.get((site.name, site.role))
    base = ToolUpdate(
        name=site.name, role=site.role, action="unknown", source=site.source,
        current=site.version, floor=floor, note=upgrade_note(site),
    )

    if not site.available:
        base.action = "unknown"
        base.reason = "not installed; run `quern setup`"
        return base

    if site.source == "venv":
        # Already covered: `quern update` reinstalls the venv eagerly, so
        # offering a second route to the same packages would be two mechanisms
        # racing over one directory.
        base.action = "current"
        base.reason = "installed in quern's venv; `quern update` keeps it current"
        return base

    if site.source in UNMANAGED_REASONS:
        base.action = "unmanaged"
        base.reason = UNMANAGED_REASONS[site.source]
        return base

    # `checked` records whether the newest version could actually be looked up.
    # Without it a failed lookup is indistinguishable from a successful one that
    # found nothing newer, and the tool reports a clean bill of health for a
    # question it never got to ask.
    # The package manager's name for this tool, which is not always quern's:
    # `idb` ships from `fb-idb`, `adb` from the `android-platform-tools` cask.
    # Using `site.name` here queried the wrong PyPI project and emitted an
    # upgrade command for a package that does not exist.
    package = site.package
    if package is None:
        base.action = "unknown"
        base.reason = (
            "no package identity recorded, so the newest version cannot be "
            "looked up safely"
        )
        return base

    checked = True
    if site.source == "pipx":
        base.latest = await pypi(package)
        checked = base.latest is not None
        base.command = ["pipx", "upgrade", package]
    elif site.source == "brew":
        if brew_latest is None:
            checked = False
        else:
            # brew outdated is exhaustive: absent from it means up to date, so
            # the current version is the newest one.
            base.latest = brew_latest.get(package, site.version)
        base.command = ["brew", "upgrade"]
        if site.brew_cask:
            base.command.append("--cask")
        base.command.append(package)
    else:
        base.action = "unknown"
        base.reason = f"no upgrade route known for source {site.source!r}"
        return base

    if not checked:
        base.action = "unknown"
        base.reason = (
            "could not check for a newer version; leaving it alone rather than "
            "reporting it current"
        )
        base.command = []
        return base

    below_floor = floor is not None and is_behind(site.version, floor)
    behind_latest = is_behind(site.version, base.latest)

    if below_floor:
        base.action = "upgrade_required"
        base.reason = f"below the {floor} quern requires"
        # A floor overrides the dependency exemption below: a tool that is too
        # old to work is not made acceptable by something else having installed
        # it. The note still says what the upgrade would touch.
        return base

    if not behind_latest:
        base.action = "current"
        base.reason = f"up to date at {base.latest}"
        base.command = []
        return base

    if site.requested is False:
        # Arrived as a dependency and is above any floor quern cares about.
        # Upgrading it directly can be reverted by whatever pulled it in, so it
        # is reported rather than offered.
        base.action = "current"
        base.reason = (
            f"{base.latest} is available, but this arrived as a dependency "
            "and works; leaving it to whatever installed it"
        )
        base.command = []
        return base

    base.action = "upgrade_available"
    base.reason = f"newer release available ({base.latest})"
    return base


def actionable(updates: list[ToolUpdate]) -> list[ToolUpdate]:
    """Just the ones with something to run, most urgent first."""
    return sorted(
        (u for u in updates if u.actionable),
        key=lambda u: ACTION_ORDER.index(u.action),
    )


def format_offer(updates: list[ToolUpdate]) -> str:
    """The block `quern update` prints. Empty string when there is nothing.

    Every line carries the command, because the alternative is a reader who
    knows a tool is stale and has to go and find out how it was installed --
    which is the work this module exists to have already done.
    """
    todo = actionable(updates)
    if not todo:
        return ""

    lines = ["", "External tools with updates available:"]
    for update in todo:
        marker = "!" if update.action == "upgrade_required" else "-"
        lines.append(f"  {marker} {update.describe()}")
        if update.action == "upgrade_required":
            lines.append(f"      required: {update.reason}")
        if update.note:
            lines.append(f"      note: {update.note}")
        lines.append(f"      {' '.join(update.command)}")
    return "\n".join(lines)


# How each action reads at a glance. `quern update` only ever prints the first
# two; doctor prints all of them, which is the difference between "what should I
# do" and "what is actually here".
ACTION_MARKERS = {
    "upgrade_required": "!",
    "upgrade_available": "\u2191",
    "current": "\u2713",
    "unmanaged": "\u2013",
    "unknown": "?",
}


def format_report(updates: list[ToolUpdate]) -> str:
    """The full picture, for `quern doctor`.

    Deliberately wider than `format_offer`. The offer answers "what should I
    run" and so shows only what is actionable; a diagnostic that hid everything
    working would be useless for the question doctor actually gets asked, which
    is "why is this machine behaving differently from that one".

    In particular the brew dependents are shown for tools that need no action at
    all. `libimobiledevice` being current is not the interesting part -- that
    `ideviceinstaller` and `ios-webkit-debug-proxy` also depend on it is, because
    that is what turns a later upgrade into a decision rather than a command.
    """
    if not updates:
        return "External tools:\n  (none detected)"

    def _label(update: ToolUpdate) -> str:
        return f"{update.name} ({update.role})"

    def _version(update: ToolUpdate) -> str:
        if update.actionable and update.latest:
            return f"{update.current} \u2192 {update.latest}"
        return update.current or "not found"

    label_width = max(len(_label(u)) for u in updates)
    version_width = max(len(_version(u)) for u in updates)

    lines = ["External tools:"]
    for update in sorted(updates, key=lambda u: (ACTION_ORDER.index(u.action), u.name)):
        marker = ACTION_MARKERS.get(update.action, "?")
        label = _label(update).ljust(label_width)
        version = _version(update).ljust(version_width)
        lines.append(f"  {marker} {label}  {version}  ({update.source})")
        if update.reason:
            lines.append(f"      {update.reason}")
        if update.note:
            lines.append(f"      {update.note}")
        if update.command:
            lines.append(f"      run: {' '.join(update.command)}")
    return "\n".join(lines)
