"""Environment discovery: what can this machine actually test?

Nothing in this suite names a device. The run starts by asking the server what
tools it has and what is plugged in, maps the answer onto a set of roles, and
every test declares the role it needs. A machine with no Android hardware skips
the Android tier and says so; a machine with two iPhones picks one and reports
which. That is what makes the same suite meaningful on a different desk.

The distinction that matters here is **absent vs broken**. A missing `adb` is a
machine that was never going to run those tests. An `adb` that is present and
hangs is a bug -- possibly Quern's -- and reporting it as "skipped: no Android"
would bury the finding. Probes therefore record *why* a capability is
unavailable, and the environment report prints it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from tests.conformance.client import QuernClient

#: `/tools` probes every device CLI, and a wedged one blocks the response.
#: Short on purpose: if tool discovery cannot answer in this long, the run needs
#: to know that as a *result*, not wait on it.
TOOLS_TIMEOUT = 25.0

#: Enumerating devices shells out to simctl/adb/devicectl in turn.
DEVICE_LIST_TIMEOUT = 90.0


class Role(str, Enum):
    """A kind of target a test can ask for, independent of any specific unit."""

    IOS_SIMULATOR = "ios_simulator"
    IOS_DEVICE = "ios_device"
    ANDROID_EMULATOR = "android_emulator"
    ANDROID_DEVICE = "android_device"

    @property
    def is_physical(self) -> bool:
        return self in (Role.IOS_DEVICE, Role.ANDROID_DEVICE)

    @property
    def platform(self) -> str:
        return "ios" if self in (Role.IOS_SIMULATOR, Role.IOS_DEVICE) else "android"


#: Role -> the `device_type` the API reports for it. One place, so a rename in
#: the server surfaces as one failing mapping test rather than scattered misses.
ROLE_DEVICE_TYPE: dict[Role, str] = {
    Role.IOS_SIMULATOR: "simulator",
    Role.IOS_DEVICE: "device",
    Role.ANDROID_EMULATOR: "android_emulator",
    Role.ANDROID_DEVICE: "android_device",
}

#: Role -> tools that could serve it. Any one present is enough; Quern falls
#: back between backends, so requiring all of them would under-report.
ROLE_TOOLS: dict[Role, tuple[str, ...]] = {
    Role.IOS_SIMULATOR: ("simctl", "idb", "sim_bridge"),
    Role.IOS_DEVICE: ("devicectl", "pymobiledevice3"),
    Role.ANDROID_EMULATOR: ("adb",),
    Role.ANDROID_DEVICE: ("adb",),
}


@dataclass(frozen=True)
class Device:
    """One discovered target, as the server describes it."""

    udid: str
    name: str
    state: str
    device_type: str
    os_version: str = ""
    device_family: str = ""
    connection_type: str = ""
    is_connected: bool = True
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def booted(self) -> bool:
        return self.state == "booted"

    @property
    def usable(self) -> bool:
        """Connected, and not sitting at an unaccepted debugging prompt.

        `unauthorized` is the state an Android device reports before the USB
        debugging dialog is accepted. It is plugged in and listed, and every
        operation against it fails -- so treating it as available produces a
        tier of failures that are really one unaccepted prompt.
        """
        return self.is_connected and self.state != "unauthorized"

    def describe(self) -> str:
        bits = [self.name, self.os_version, self.state]
        return f"{' / '.join(b for b in bits if b)} [{self.udid[:8]}…]"


@dataclass
class RoleAvailability:
    """Whether a role can be tested, and if not, why not."""

    role: Role
    devices: list[Device]
    tools_present: tuple[str, ...]
    #: Empty when the role is available.
    blocked_reason: str = ""
    #: True when the reason is "this machine has no such hardware", as opposed
    #: to "the hardware is here and something is wrong". Only the latter is a
    #: finding.
    absent: bool = True

    @property
    def available(self) -> bool:
        return not self.blocked_reason and bool(self.devices)

    def pick(self, *, prefer_booted: bool = True) -> Device | None:
        """Choose a device for this role.

        Prefers an already-booted one so a run does not pay boot cost it does
        not need, and otherwise takes the first listed. Deterministic within a
        run: sorted by udid so two tests asking for the same role get the same
        unit, and a failure names a device that can be looked at afterwards.
        """
        usable = sorted((d for d in self.devices if d.usable), key=lambda d: d.udid)
        if not usable:
            return None
        if prefer_booted:
            for device in usable:
                if device.booted:
                    return device
        return usable[0]


@dataclass
class Environment:
    """Everything the run discovered before the first real test."""

    server_url: str
    server_version: str | None
    server_source: str
    authenticated: bool
    tools: dict[str, bool] = field(default_factory=dict)
    tools_error: str = ""
    devices: list[Device] = field(default_factory=list)
    devices_error: str = ""
    roles: dict[Role, RoleAvailability] = field(default_factory=dict)
    #: Seconds each discovery probe took; slow discovery is itself a signal.
    timings: dict[str, float] = field(default_factory=dict)

    def role(self, role: Role) -> RoleAvailability:
        return self.roles[role]

    def has(self, role: Role) -> bool:
        return self.roles[role].available

    @property
    def any_ios(self) -> bool:
        return self.has(Role.IOS_SIMULATOR) or self.has(Role.IOS_DEVICE)

    @property
    def any_android(self) -> bool:
        return self.has(Role.ANDROID_EMULATOR) or self.has(Role.ANDROID_DEVICE)

    def summary(self) -> str:
        """The block printed at the head of every run.

        Written to be pasted into a bug report: it says which server, which
        version, which tools, and which devices -- the four things that make a
        conformance failure on someone else's machine reproducible.
        """
        lines = [
            "Quern conformance environment",
            f"  server    {self.server_url}"
            f"  (v{self.server_version or '?'}, via {self.server_source})",
            f"  auth      {'ok' if self.authenticated else 'NOT AUTHENTICATED'}",
        ]
        if self.tools_error:
            lines.append(f"  tools     UNAVAILABLE — {self.tools_error}")
        else:
            present = sorted(n for n, ok in self.tools.items() if ok)
            missing = sorted(n for n, ok in self.tools.items() if not ok)
            lines.append(f"  tools     {', '.join(present) or 'none'}")
            if missing:
                lines.append(f"  missing   {', '.join(missing)}")
        if self.devices_error:
            lines.append(f"  devices   UNAVAILABLE — {self.devices_error}")

        for role in Role:
            avail = self.roles.get(role)
            if avail is None:
                continue
            if avail.available:
                chosen = avail.pick()
                extra = f" (using {chosen.describe()})" if chosen else ""
                lines.append(
                    f"  {role.value:<17} {len(avail.devices)} available{extra}"
                )
            else:
                mark = "—" if avail.absent else "!!"
                lines.append(f"  {role.value:<17} {mark} {avail.blocked_reason}")

        if self.timings:
            timing = ", ".join(f"{k} {v:.1f}s" for k, v in self.timings.items())
            lines.append(f"  discovery {timing}")
        return "\n".join(lines)


def discover(client: QuernClient, *, authenticated: bool) -> Environment:
    """Probe the server and build the capability map.

    Never raises for a missing capability -- a machine without Android is a
    normal machine. Raises only if the server itself cannot be talked to, which
    the caller has already checked.
    """
    env = Environment(
        server_url=client.target.url,
        server_version=None,
        server_source=client.target.source,
        authenticated=authenticated,
    )

    health = _timed(env, "health", lambda: client.get("/health", authenticated=False, timeout=10.0))
    if health is not None and health.is_success and health.content:
        env.server_version = health.json().get("version")

    env.tools, env.tools_error = _probe_tools(env, client)
    env.devices, env.devices_error = _probe_devices(env, client, authenticated=authenticated)
    env.roles = _map_roles(env)
    return env


def _timed(env: Environment, label: str, call) -> Any:
    started = time.perf_counter()
    try:
        return call()
    except httpx.HTTPError:
        return None
    finally:
        env.timings[label] = time.perf_counter() - started


def _probe_tools(env: Environment, client: QuernClient) -> tuple[dict[str, bool], str]:
    """Read tool availability from the public `/tools` endpoint.

    A timeout here is reported rather than raised, and deliberately phrased as a
    server problem: `/tools` is what `quern doctor` runs, so a `/tools` that
    cannot answer is a broken doctor, not merely a detail this suite lacks.
    """
    started = time.perf_counter()
    try:
        resp = client.get("/tools", authenticated=False, timeout=TOOLS_TIMEOUT)
    except httpx.TimeoutException:
        return {}, (
            f"/tools did not respond within {TOOLS_TIMEOUT:.0f}s — a device CLI "
            "is wedged (this is what `quern doctor` calls)"
        )
    except httpx.HTTPError as exc:
        return {}, f"/tools failed: {exc!r}"
    finally:
        env.timings["tools"] = time.perf_counter() - started

    if not resp.is_success:
        return {}, f"/tools returned {resp.status_code}"
    body = resp.json() if resp.content else {}
    tools = body.get("tools") or {}
    if not isinstance(tools, dict):
        return {}, f"/tools returned an unexpected shape: {body!r}"
    return {str(k): bool(v) for k, v in tools.items()}, ""


def _probe_devices(
    env: Environment, client: QuernClient, *, authenticated: bool
) -> tuple[list[Device], str]:
    """Enumerate every device the server can see.

    `include_disconnected` is on so that a paired-but-unreachable iPhone is
    discovered and reported as blocked, rather than vanishing into "no device".
    Filtering to what is usable happens in the role mapping, where the reason
    can be stated.
    """
    if not authenticated:
        return [], "device list needs an API key"

    started = time.perf_counter()
    try:
        resp = client.get(
            "/api/v1/device/list",
            params={"include_disconnected": True},
            timeout=DEVICE_LIST_TIMEOUT,
        )
    except httpx.TimeoutException:
        return [], (
            f"/device/list did not respond within {DEVICE_LIST_TIMEOUT:.0f}s — "
            "device enumeration is hung"
        )
    except httpx.HTTPError as exc:
        return [], f"/device/list failed: {exc!r}"
    finally:
        env.timings["device_list"] = time.perf_counter() - started

    if not resp.is_success:
        return [], f"/device/list returned {resp.status_code}: {resp.text[:400]}"

    body = resp.json() if resp.content else {}
    devices = []
    for raw in body.get("devices") or []:
        devices.append(
            Device(
                udid=raw.get("udid", ""),
                name=raw.get("name", ""),
                state=raw.get("state", ""),
                device_type=raw.get("device_type", ""),
                os_version=raw.get("os_version", ""),
                device_family=raw.get("device_family", ""),
                connection_type=raw.get("connection_type", ""),
                is_connected=bool(raw.get("is_connected", True)),
                raw=raw,
            )
        )
    # `/device/list` also reports tools. Prefer it when the public probe timed
    # out but this one answered -- same source, and a filled-in map beats none.
    if env.tools_error and isinstance(body.get("tools"), dict):
        env.tools = {str(k): bool(v) for k, v in body["tools"].items()}
        env.tools_error += " (recovered from /device/list)"
    return devices, ""


def _map_roles(env: Environment) -> dict[Role, RoleAvailability]:
    """Turn tools + devices into per-role availability with stated reasons."""
    roles: dict[Role, RoleAvailability] = {}
    for role in Role:
        wanted_type = ROLE_DEVICE_TYPE[role]
        matching = [d for d in env.devices if d.device_type == wanted_type]
        usable = [d for d in matching if d.usable]
        present = tuple(t for t in ROLE_TOOLS[role] if env.tools.get(t))

        reason, absent = "", True
        if env.devices_error:
            # Discovery itself failed. Not "no hardware" -- unknown hardware,
            # and the run should not claim the tier was cleanly skipped.
            reason, absent = f"device discovery failed: {env.devices_error}", False
        elif not present and not env.tools_error:
            reason = (
                f"no backend for {role.value}: none of "
                f"{', '.join(ROLE_TOOLS[role])} is available"
            )
        elif env.tools_error and not matching:
            reason, absent = f"tool discovery failed: {env.tools_error}", False
        elif not matching:
            reason = f"no {wanted_type} present"
        elif not usable:
            blocked = ", ".join(
                f"{d.name or d.udid[:8]} ({d.state or 'disconnected'})" for d in matching
            )
            reason, absent = (
                f"{len(matching)} found but none usable: {blocked}",
                False,
            )

        roles[role] = RoleAvailability(
            role=role,
            devices=usable,
            tools_present=present,
            blocked_reason=reason,
            absent=absent,
        )
    return roles
