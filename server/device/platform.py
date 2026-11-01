"""Which platform a device request is for: iOS or Android (#582).

A bare OS version means either platform -- Android 16 and iOS 16 both exist --
so where a request does not say, the platform comes from the caller's project
(the MCP sends it, with where it came from) or, failing that, from what the
request matches. One rule for every surface that picks devices: the pool's
resolve and ensure, reservations, and the device list.

The order, first that applies:

1. **The request says.** ``platform`` with no source (or ``"request"``), a
   ``device_type``, or a prefixed version ("Android 16", "ios18.6"). These
   restrict, and conflicting ones are refused.
2. **The project says.** ``platform`` with a source such as
   ``"knowledge_base"`` or ``"project"``. A default, not a restriction: a
   request with criteria that matches nothing on that platform and exactly
   one device on the other gets that device -- naming a Pixel from an iOS
   project is asking for it.
3. **The criteria say.** Matches on one platform only are that platform's.
   Matches on both are refused as ambiguous, listing each side, so the
   caller can choose.
4. **Nothing says.** The surface's own default, as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from server.models import DeviceType

IOS = "ios"
ANDROID = "android"
PLATFORMS = (IOS, ANDROID)
OTHER = {IOS: ANDROID, ANDROID: IOS}

_TYPES = {
    IOS: (DeviceType.SIMULATOR, DeviceType.DEVICE),
    ANDROID: (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE),
}

#: The sources that make a platform a default rather than a restriction.
REQUEST = "request"

#: "16", "18.6", "iOS 18.6", "ios18", "Android 16", "android16", "v14".
_OS = re.compile(r"^\s*(?P<platform>ios|android)?\s*v?(?P<version>\d+(?:\.\d+)*)?\s*$",
                 re.IGNORECASE)


def platform_of(device_type: DeviceType | str | None) -> str | None:
    if device_type is None:
        return None
    value = getattr(device_type, "value", device_type)
    for platform, types in _TYPES.items():
        if value in {t.value for t in types}:
            return platform
    return None


def device_types(platform: str) -> tuple[DeviceType, ...]:
    return _TYPES[platform]


def parse_os(requested: str | None) -> tuple[str | None, str | None]:
    """``(platform, version)`` from a requested OS version, either part
    ``None`` when not given. Anything this does not recognise keeps its old
    meaning: the first number in it, on either platform."""
    if not requested:
        return None, None
    m = _OS.match(requested)
    if m:
        platform = m.group("platform")
        return (platform.lower() if platform else None), m.group("version")
    number = re.search(r"\d+(?:\.\d+)*", requested)
    return None, number.group() if number else None


def os_matches(device_os: str, device_type: DeviceType | str | None, requested: str) -> bool:
    """Whether a device's OS satisfies a requested version: the number as a
    prefix ("18" matches "iOS 18.2", not "iOS 180"), and a named platform
    only on that platform -- "Android 14" is not iOS 14."""
    platform, version = parse_os(requested)
    if platform and platform_of(device_type) != platform:
        return False
    if version is None:
        return platform is not None
    m = re.search(r"\d+(?:\.\d+)*", device_os or "")
    if not m:
        return False
    have = m.group()
    return have == version or have.startswith(version + ".")


class PlatformConflict(ValueError):
    """The request names two platforms: a device_type of one and a version
    or ``platform`` of the other."""


def requested_platform(*, platform: str | None, source: str | None,
                       device_type: DeviceType | str | None,
                       os_version: str | None) -> str | None:
    """The platform the request itself names, if any -- step 1."""
    named = {p for p in (
        platform if platform and (source in (None, "", REQUEST)) else None,
        platform_of(device_type),
        parse_os(os_version)[0],
    ) if p}
    if len(named) > 1:
        given = (("platform", platform),
                 ("device_type", getattr(device_type, "value", device_type)),
                 ("os_version", os_version))
        raise PlatformConflict("the request names both platforms: "
                               + ", ".join(f"{k}={v!r}" for k, v in given if v))
    return named.pop() if named else None


@dataclass
class PlatformChoice:
    devices: list
    #: The platform that governed the pick, or None where nothing did.
    platform: str | None
    #: Why: "request", the project's source ("knowledge_base", "project"),
    #: "only_match" (the only platform with a match), or None.
    source: str | None

    def report(self) -> dict:
        """The fields an answer carries, so a caller that wanted the other
        platform can see what happened and ask for it."""
        if self.platform is None:
            return {}
        return {"platform": self.platform, "platform_source": self.source}


def _brief(d) -> dict:
    kind = getattr(d, "device_type", None)
    return {"udid": d.udid, "name": getattr(d, "name", ""),
            "os_version": getattr(d, "os_version", ""),
            "device_type": getattr(kind, "value", kind),
            "state": getattr(getattr(d, "state", None), "value", None)}


class PlatformRefusal(Exception):
    """A pick that needs the caller to choose a platform. ``detail`` is the
    structured answer: what matched on each side and the calls that would
    choose."""

    LIMIT = 10

    def __init__(self, error: str, message: str, matches: dict[str, list]):
        super().__init__(message)
        self.detail = {
            "error": error,
            "message": message,
            "matches": {p: [_brief(d) for d in ds[:self.LIMIT]] for p, ds in matches.items()},
            "match_counts": {p: len(ds) for p, ds in matches.items()},
            "retry_with": [{"platform": p} for p, ds in matches.items() if ds],
        }


def label(platform: str) -> str:
    return "iOS" if platform == IOS else "Android"


def _count(n: int, platform: str) -> str:
    return f"{n} {label(platform)} device{'' if n == 1 else 's'}"


def choose(find: Callable[[str | None], list], *, requested: str | None,
           default: str | None, default_source: str | None,
           has_criteria: bool, refuse_ambiguous: bool = True) -> PlatformChoice:
    """Apply the order in the module docstring. ``find(platform)`` returns
    the candidates on that platform (``None``: either) under every other
    criterion of the request.

    ``refuse_ambiguous=False`` is for a listing, where showing both sides is
    an answer rather than a guess."""
    if requested:
        return PlatformChoice(find(requested), requested, REQUEST)
    if default:
        found = find(default)
        if found or not has_criteria:
            return PlatformChoice(found, default, default_source)
        other = OTHER[default]
        theirs = find(other)
        if len(theirs) == 1 or (theirs and not refuse_ambiguous):
            return PlatformChoice(theirs, other, "only_match")
        if theirs:
            raise PlatformRefusal(
                "other_platform",
                f"No {label(default)} device matches (platform {default!r}, from "
                f"{default_source}); {_count(len(theirs), other)} do. Pass "
                f"platform={other!r} to pick from them.",
                {other: theirs})
        return PlatformChoice([], default, default_source)
    if has_criteria:
        both = {p: find(p) for p in PLATFORMS}
        hit = [p for p in PLATFORMS if both[p]]
        if len(hit) == 1:
            return PlatformChoice(both[hit[0]], hit[0], "only_match")
        if len(hit) == 2 and refuse_ambiguous:
            raise PlatformRefusal(
                "ambiguous_platform",
                f"This matches {_count(len(both[IOS]), IOS)} and "
                f"{_count(len(both[ANDROID]), ANDROID)}. Pass platform='ios' or "
                "platform='android', or prefix the version ('iOS 16', 'Android 16').",
                both)
    return PlatformChoice(find(None), None, None)


def on_platform(devices: Iterable, platform: str | None) -> list:
    if platform is None:
        return list(devices)
    types = set(_TYPES[platform])
    return [d for d in devices if getattr(d, "device_type", None) in types]
